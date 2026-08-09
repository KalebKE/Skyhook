from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from .analysis import project_symbol
from .config import SkyhookConfig
from .scanner import RepoScan
from .schema import empty_map, validate_map


DEFAULT_OPENAI_MODEL = "gpt-4.1-mini"

_ORIENTATION_SYSTEM_PROMPT = (
    "You generate compact repository orientation maps for coding agents. "
    "Return only valid JSON matching the requested schema. "
    "Do not make a comprehensive index; create fast wayfinding context."
)

_PARTIAL_SYSTEM_PROMPT = (
    "You extract compact repository-orientation evidence from one bounded segment "
    "of a repository scan. Return only valid JSON matching the requested schema."
)


class ModelError(RuntimeError):
    pass


class ModelHTTPError(ModelError):
    """HTTP verdict from an OpenAI-compatible endpoint."""

    def __init__(self, status: int, detail: str, code: str = ""):
        super().__init__(f"model request failed: HTTP {status}: {detail}")
        self.status = status
        self.detail = detail
        self.code = code

    @property
    def is_context_length_error(self) -> bool:
        lowered = f"{self.code} {self.detail}".lower()
        return "context_length_exceeded" in lowered or "context length" in lowered


class Orienter:
    def orient(self, scan: RepoScan, previous: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        raise NotImplementedError


class StaticOrienter(Orienter):
    """Deterministic fallback used when no model credentials are configured."""

    def orient(self, scan: RepoScan, previous: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        data = empty_map(scan.repo_name, str(scan.root))
        data["repo"]["primaryLanguages"] = list(scan.language_counts.keys())[:5]
        data["repo"]["detectedFrameworks"] = scan.frameworks
        data["scan"]["digest"] = scan.digest
        data["scan"]["fileCount"] = len(scan.files)
        data["orientation"] = {
            "summary": _static_summary(scan),
            "agentStartHere": _start_here(scan),
            "knownGotchas": _static_gotchas(scan),
        }
        data["codeAreas"] = _static_code_areas(scan)
        data["docs"] = _static_docs(scan)
        data["architecture"] = _static_architecture(scan)
        data["symbols"] = _static_symbols(scan)
        return data


class OpenAICompatibleOrienter(Orienter):
    def __init__(self, config: SkyhookConfig):
        self.config = config
        self.api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("SKYHOOK_API_KEY")
        self.base_url = (
            config.model.base_url
            or os.environ.get("OPENAI_BASE_URL")
            or os.environ.get("SKYHOOK_BASE_URL")
            or "https://api.openai.com/v1"
        ).rstrip("/")
        requested_model = os.environ.get("SKYHOOK_MODEL") or config.model.model
        self.model = DEFAULT_OPENAI_MODEL if requested_model in {"", "auto"} else requested_model
        self.timeout_seconds = _positive_int_setting(
            "SKYHOOK_TIMEOUT_SECONDS", config.model.timeout_seconds
        )
        self.max_prompt_bytes = _positive_int_setting(
            "SKYHOOK_MAX_PROMPT_BYTES", config.model.max_prompt_bytes
        )
        if not self.api_key:
            raise ModelError("OPENAI_API_KEY or SKYHOOK_API_KEY is required for provider=openai")

    def orient(self, scan: RepoScan, previous: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        facts = scan_facts(scan, max_source_samples=self.config.scan.max_source_samples)
        messages = _orientation_messages(facts, previous)
        if _messages_size(messages) <= self.max_prompt_bytes:
            try:
                data = self._request_json(messages, validate_map, _schema_instruction(), "map")
            except ModelHTTPError as exc:
                if not exc.is_context_length_error:
                    raise
                data = self._orient_chunked(facts, previous)
        else:
            data = self._orient_chunked(facts, previous)
        data["scan"]["digest"] = scan.digest
        data["scan"]["fileCount"] = len(scan.files)
        data["scan"]["generatedBy"] = "skyhook"
        return data

    def _orient_chunked(
        self,
        facts: Mapping[str, Any],
        previous: Optional[Mapping[str, Any]],
    ) -> Dict[str, Any]:
        partials: List[Dict[str, Any]] = []
        for chunk in _fact_chunks(facts, self.max_prompt_bytes):
            partials.extend(self._map_fact_chunk(chunk))
        return self._synthesize_map(facts.get("repo", {}), partials, previous)

    def _map_fact_chunk(self, facts: Mapping[str, Any]) -> List[Dict[str, Any]]:
        messages = _partial_messages(facts)
        try:
            return [
                self._request_json(
                    messages, _validate_partial, _partial_schema_instruction(), "partial map"
                )
            ]
        except ModelHTTPError as exc:
            if not exc.is_context_length_error:
                raise
            halves = _split_fact_chunk(facts)
            if halves is None:
                raise ModelError(
                    "one scan fact exceeds the model context window; lower scan.maxDocBytes "
                    "or use a model with a larger context window"
                ) from exc
            return self._map_fact_chunk(halves[0]) + self._map_fact_chunk(halves[1])

    def _synthesize_map(
        self,
        repo: Mapping[str, Any],
        partials: List[Mapping[str, Any]],
        previous: Optional[Mapping[str, Any]],
    ) -> Dict[str, Any]:
        partials = self._reduce_to_fit(repo, partials, previous)
        messages = _final_messages(repo, partials, previous)
        try:
            return self._request_json(messages, validate_map, _schema_instruction(), "map")
        except ModelHTTPError as exc:
            if not exc.is_context_length_error or len(partials) <= 1:
                raise
            compacted = self._merge_partials(repo, partials)
            return self._synthesize_map(repo, [compacted], previous)

    def _reduce_to_fit(
        self,
        repo: Mapping[str, Any],
        partials: List[Mapping[str, Any]],
        previous: Optional[Mapping[str, Any]],
    ) -> List[Mapping[str, Any]]:
        current = list(partials)
        while _messages_size(_final_messages(repo, current, previous)) > self.max_prompt_bytes:
            groups = _partial_groups(repo, current, self.max_prompt_bytes)
            reduced: List[Mapping[str, Any]] = []
            for group in groups:
                if len(group) == 1:
                    reduced.append(group[0])
                else:
                    reduced.append(self._merge_partials(repo, group))
            if len(reduced) >= len(current):
                raise ModelError(
                    "partial map output exceeds model.maxPromptBytes; increase the budget "
                    "or use a model with a larger context window"
                )
            current = reduced
        return current

    def _merge_partials(
        self,
        repo: Mapping[str, Any],
        partials: Sequence[Mapping[str, Any]],
    ) -> Dict[str, Any]:
        messages = _merge_messages(repo, partials)
        if _messages_size(messages) > self.max_prompt_bytes:
            if len(partials) <= 1:
                raise ModelError("one partial map exceeds model.maxPromptBytes")
            midpoint = len(partials) // 2
            left = self._merge_partials(repo, partials[:midpoint])
            right = self._merge_partials(repo, partials[midpoint:])
            return self._merge_partials(repo, [left, right])
        try:
            return self._request_json(
                messages, _validate_partial, _partial_schema_instruction(), "partial map"
            )
        except ModelHTTPError as exc:
            if not exc.is_context_length_error or len(partials) <= 1:
                raise
            midpoint = len(partials) // 2
            left = self._merge_partials(repo, partials[:midpoint])
            right = self._merge_partials(repo, partials[midpoint:])
            return self._merge_partials(repo, [left, right])

    def _request_json(
        self,
        messages: List[Mapping[str, str]],
        validator: Callable[[Mapping[str, Any]], List[str]],
        schema: str,
        label: str,
    ) -> Dict[str, Any]:
        raw = self._chat(messages)
        data, errors = _parse_and_validate(raw, validator)
        if not errors:
            return data
        repair_messages = [
            {
                "role": "system",
                "content": "Repair JSON. Return only one valid JSON object matching the requested schema.",
            },
            {
                "role": "user",
                "content": (
                    "Schema:\n" + schema + "\n\nErrors:\n" + "; ".join(errors) + "\n\nJSON to repair:\n" + raw
                ),
            },
        ]
        raw = self._chat(repair_messages)
        data, errors = _parse_and_validate(raw, validator)
        if errors:
            raise ModelError(f"model returned invalid {label} JSON: " + "; ".join(errors))
        return data

    def _chat(self, messages: List[Mapping[str, str]]) -> str:
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
        }
        request = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise ModelHTTPError(exc.code, detail, _http_error_code(detail)) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise ModelError(f"model request failed: {exc}") from exc
        try:
            return body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelError("model response did not contain choices[0].message.content") from exc


def resolve_provider(config: SkyhookConfig, provider_override: Optional[str] = None) -> str:
    provider = provider_override or config.model.provider
    if provider == "auto":
        provider = "openai" if (os.environ.get("OPENAI_API_KEY") or os.environ.get("SKYHOOK_API_KEY")) else "static"
    return provider


def choose_orienter(config: SkyhookConfig, provider_override: Optional[str] = None) -> Orienter:
    provider = resolve_provider(config, provider_override)
    if provider in {"static", "none", "no-model"}:
        return StaticOrienter()
    if provider == "openai":
        return OpenAICompatibleOrienter(config)
    raise ModelError(f"unsupported model provider: {provider}")


def chat_json(
    config: SkyhookConfig,
    messages: List[Mapping[str, str]],
    provider_override: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Public JSON-mode completion helper for tools built on Skyhook.

    Returns None when the resolved provider is static/none, so callers can
    fall back to deterministic behavior. Raises ModelError on request or
    parse failure.
    """
    provider = resolve_provider(config, provider_override)
    if provider in {"static", "none", "no-model"}:
        return None
    if provider != "openai":
        raise ModelError(f"unsupported model provider: {provider}")
    orienter = OpenAICompatibleOrienter(config)
    return parse_json_object(orienter._chat(list(messages)))


def scan_facts(scan: RepoScan, max_source_samples: int = 300) -> Dict[str, Any]:
    docs = [
        {
            "path": doc.path,
            "kind": doc.doc_kind,
            "title": doc.title,
            "snippet": doc.snippet,
        }
        for doc in scan.docs[:200]
    ]
    manifests = [{"path": item.path, "kind": item.kind} for item in scan.manifests[:100]]
    sources = [
        {
            "path": source.path,
            "language": source.language,
            "isTest": source.is_test,
            "symbols": source.symbols[:12],
            "imports": source.imports[:20],
        }
        for source in sorted(scan.sources, key=lambda item: item.path)[:max_source_samples]
    ]
    return {
        "repo": {
            "name": scan.repo_name,
            "root": str(scan.root),
            "languageCounts": scan.language_counts,
            "frameworks": scan.frameworks,
            "topDirs": scan.top_dirs,
            "fileCount": len(scan.files),
        },
        "docs": docs,
        "manifests": manifests,
        "sourceSamples": sources,
    }


def build_prompt(facts: Mapping[str, Any], previous: Optional[Mapping[str, Any]] = None) -> str:
    previous_note = ""
    if previous:
        previous_note = "\nExisting map JSON is provided for continuity:\n" + json.dumps(previous, ensure_ascii=False)[:20000]
    return (
        "Create a Skyhook orientation JSON for this repository.\n"
        "Goal: help coding agents find relevant code, docs, architecture, ADRs, and design context quickly.\n"
        "Keep it lightweight. Prefer 5-12 codeAreas, 5-30 docs, and compact symbols.\n"
        "Use only facts from the scan. If confidence is weak, mark confidence=low.\n"
        "Return only JSON.\n\n"
        "Required JSON shape:\n"
        + _schema_instruction()
        + "\n\nScan facts:\n"
        + json.dumps(facts, ensure_ascii=False, separators=(",", ":"))
        + previous_note
    )


def _orientation_messages(
    facts: Mapping[str, Any], previous: Optional[Mapping[str, Any]]
) -> List[Mapping[str, str]]:
    return [
        {"role": "system", "content": _ORIENTATION_SYSTEM_PROMPT},
        {"role": "user", "content": build_prompt(facts, previous)},
    ]


def _partial_messages(facts: Mapping[str, Any]) -> List[Mapping[str, str]]:
    prompt = (
        "Extract orientation evidence from this bounded scan segment. "
        "Describe only areas and documents supported by these facts. Keep every list compact.\n\n"
        "Required JSON shape:\n"
        + _partial_schema_instruction()
        + "\n\nScan segment:\n"
        + json.dumps(facts, ensure_ascii=False, separators=(",", ":"))
    )
    return [
        {"role": "system", "content": _PARTIAL_SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]


def _merge_messages(
    repo: Mapping[str, Any], partials: Sequence[Mapping[str, Any]]
) -> List[Mapping[str, str]]:
    prompt = (
        "Merge these partial repository-orientation results. Deduplicate overlapping areas and paths, "
        "retain distinct evidence, and keep the result compact.\n\nRequired JSON shape:\n"
        + _partial_schema_instruction()
        + "\n\nRepository facts:\n"
        + json.dumps(repo, ensure_ascii=False, separators=(",", ":"))
        + "\n\nPartial results:\n"
        + json.dumps(list(partials), ensure_ascii=False, separators=(",", ":"))
    )
    return [
        {"role": "system", "content": _PARTIAL_SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]


def _final_messages(
    repo: Mapping[str, Any],
    partials: Sequence[Mapping[str, Any]],
    previous: Optional[Mapping[str, Any]],
) -> List[Mapping[str, str]]:
    previous_note = ""
    if previous:
        previous_note = (
            "\n\nExisting map JSON for continuity:\n"
            + json.dumps(previous, ensure_ascii=False, separators=(",", ":"))[:20000]
        )
    prompt = (
        "Synthesize one Skyhook orientation JSON from the bounded partial analyses below. "
        "Deduplicate overlapping areas and paths. Prefer 5-12 codeAreas, 5-30 docs, and compact symbols. "
        "Use only supplied evidence and mark weak confidence as low. Return only JSON.\n\n"
        "Required JSON shape:\n"
        + _schema_instruction()
        + "\n\nRepository facts:\n"
        + json.dumps(repo, ensure_ascii=False, separators=(",", ":"))
        + "\n\nPartial analyses:\n"
        + json.dumps(list(partials), ensure_ascii=False, separators=(",", ":"))
        + previous_note
    )
    return [
        {"role": "system", "content": _ORIENTATION_SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]


def _fact_chunks(facts: Mapping[str, Any], max_prompt_bytes: int) -> List[Dict[str, Any]]:
    repo = dict(facts.get("repo", {}) or {})
    items: List[tuple[str, Mapping[str, Any]]] = []
    for key in ("docs", "manifests", "sourceSamples"):
        for item in facts.get(key, []) or []:
            items.append((key, item))
    items.sort(key=lambda pair: (str(pair[1].get("path", "")), pair[0]))

    def empty_chunk() -> Dict[str, Any]:
        return {"repo": repo, "docs": [], "manifests": [], "sourceSamples": []}

    chunks: List[Dict[str, Any]] = []
    current = empty_chunk()
    for key, item in items:
        candidate = {
            name: list(value) if isinstance(value, list) else value
            for name, value in current.items()
        }
        candidate[key].append(item)
        if _messages_size(_partial_messages(candidate)) <= max_prompt_bytes:
            current = candidate
            continue
        if any(current[name] for name in ("docs", "manifests", "sourceSamples")):
            chunks.append(current)
            current = empty_chunk()
            current[key].append(item)
        else:
            current = candidate
        if _messages_size(_partial_messages(current)) > max_prompt_bytes:
            path = item.get("path", "unknown")
            raise ModelError(
                f"scan fact {path} exceeds model.maxPromptBytes={max_prompt_bytes}"
            )
    if any(current[name] for name in ("docs", "manifests", "sourceSamples")) or not chunks:
        chunks.append(current)
    return chunks


def _split_fact_chunk(
    facts: Mapping[str, Any],
) -> Optional[tuple[Dict[str, Any], Dict[str, Any]]]:
    items: List[tuple[str, Mapping[str, Any]]] = []
    for key in ("docs", "manifests", "sourceSamples"):
        items.extend((key, item) for item in facts.get(key, []) or [])
    if len(items) <= 1:
        return None
    midpoint = len(items) // 2

    def build(part: Sequence[tuple[str, Mapping[str, Any]]]) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "repo": dict(facts.get("repo", {}) or {}),
            "docs": [],
            "manifests": [],
            "sourceSamples": [],
        }
        for key, item in part:
            result[key].append(item)
        return result

    return build(items[:midpoint]), build(items[midpoint:])


def _partial_groups(
    repo: Mapping[str, Any],
    partials: Sequence[Mapping[str, Any]],
    max_prompt_bytes: int,
) -> List[List[Mapping[str, Any]]]:
    groups: List[List[Mapping[str, Any]]] = []
    current: List[Mapping[str, Any]] = []
    for partial in partials:
        candidate = current + [partial]
        if _messages_size(_merge_messages(repo, candidate)) <= max_prompt_bytes:
            current = candidate
            continue
        if current:
            groups.append(current)
        current = [partial]
    if current:
        groups.append(current)
    return groups


def _messages_size(messages: Sequence[Mapping[str, str]]) -> int:
    return sum(len(str(message.get("content", "")).encode("utf-8")) for message in messages)


def _partial_schema_instruction() -> str:
    return json.dumps(
        {
            "orientation": {
                "summaryHints": ["string"],
                "agentStartHere": ["path"],
                "knownGotchas": ["string"],
            },
            "codeAreas": [
                {
                    "id": "string",
                    "name": "string",
                    "purpose": "string",
                    "paths": ["string"],
                    "entrypoints": ["string"],
                    "relatedDocs": ["string"],
                    "confidence": "high|medium|low",
                }
            ],
            "docs": [
                {
                    "path": "string",
                    "kind": "readme|adr|architecture|design|runbook|api|test|unknown",
                    "title": "string",
                    "summary": "string",
                    "whenToRead": "string",
                }
            ],
            "architecture": [
                {
                    "name": "string",
                    "kind": "adr|c4|module-map|dependency-map|domain-model|api-map|other",
                    "paths": ["string"],
                    "summary": "string",
                }
            ],
        },
        separators=(",", ":"),
    )


def _validate_partial(data: Mapping[str, Any]) -> List[str]:
    errors: List[str] = []
    if not isinstance(data.get("orientation"), Mapping):
        errors.append("orientation must be an object")
    for key in ("codeAreas", "docs", "architecture"):
        if not isinstance(data.get(key), list):
            errors.append(f"{key} must be a list")
    return errors


def _parse_and_validate(
    raw: str, validator: Callable[[Mapping[str, Any]], List[str]]
) -> tuple[Dict[str, Any], List[str]]:
    try:
        data = parse_json_object(raw)
    except (json.JSONDecodeError, ModelError) as exc:
        return {}, [str(exc)]
    if not isinstance(data, dict):
        return {}, ["response must be a JSON object"]
    return data, validator(data)


def _http_error_code(detail: str) -> str:
    try:
        body = json.loads(detail)
    except (json.JSONDecodeError, TypeError):
        return ""
    error = body.get("error", {}) if isinstance(body, dict) else {}
    if not isinstance(error, dict):
        return ""
    return str(error.get("code") or error.get("type") or "")


def _positive_int_setting(env_name: str, configured: int) -> int:
    raw: Any = os.environ.get(env_name, configured)
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ModelError(f"{env_name} must be a positive integer") from exc
    if value <= 0:
        raise ModelError(f"{env_name} must be a positive integer")
    return value


def parse_json_object(raw: str) -> Dict[str, Any]:
    raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            raise ModelError("model did not return a JSON object")
        return json.loads(match.group(0))


def _schema_instruction() -> str:
    return json.dumps(
        {
            "schemaVersion": 1,
            "repo": {
                "name": "string",
                "root": "string",
                "primaryLanguages": ["string"],
                "detectedFrameworks": ["string"],
            },
            "scan": {"digest": "string", "fileCount": 0, "generatedBy": "skyhook"},
            "orientation": {
                "summary": "string",
                "agentStartHere": ["string"],
                "knownGotchas": ["string"],
            },
            "codeAreas": [
                {
                    "id": "string",
                    "name": "string",
                    "purpose": "string",
                    "paths": ["string"],
                    "entrypoints": ["string"],
                    "relatedDocs": ["string"],
                    "confidence": "high|medium|low",
                }
            ],
            "docs": [
                {
                    "path": "string",
                    "kind": "readme|adr|architecture|design|runbook|api|test|unknown",
                    "title": "string",
                    "summary": "string",
                    "whenToRead": "string",
                }
            ],
            "architecture": [
                {
                    "name": "string",
                    "kind": "adr|c4|module-map|dependency-map|domain-model|api-map|other",
                    "paths": ["string"],
                    "summary": "string",
                }
            ],
            "symbols": [
                {
                    "name": "string",
                    "kind": "module|route|service|repository|model|component|test|task|other",
                    "path": "string",
                    "areaId": "string",
                }
            ],
            "tests": [
                {
                    "path": "string",
                    "language": "string",
                    "framework": "string",
                    "targetHints": ["string"],
                    "symbols": ["string"],
                }
            ],
        },
        indent=2,
    )


def _static_summary(scan: RepoScan) -> str:
    languages = ", ".join(list(scan.language_counts.keys())[:3]) or "unknown languages"
    frameworks = ", ".join(scan.frameworks[:5]) or "no specific framework detected"
    return f"{scan.repo_name} contains {len(scan.files)} scanned files. Primary languages: {languages}. Detected frameworks: {frameworks}."


def _start_here(scan: RepoScan) -> List[str]:
    paths: List[str] = []
    for preferred in [
        "AGENTS.md",
        "CLAUDE.md",
        "README.md",
        ".claude/ROUTER.md",
        ".claude/CODE_MAP.md",
        "docs/ARCHITECTURE.md",
        ".claude/context/architecture.md",
        ".claude/context/decisions.md",
        ".claude/patterns/INDEX.md",
        "docs/README.md",
    ]:
        if any(f.path == preferred for f in scan.files):
            paths.append(preferred)
    paths.extend(
        doc.path
        for doc in _ranked_docs(scan)
        if doc.path not in paths and doc.doc_kind in {"readme", "architecture", "adr"}
        and not _is_low_signal_workflow_doc(doc.path)
    )
    if not paths:
        paths = [doc.path for doc in _ranked_docs(scan)[:3]]
    return paths[:8]


def _static_gotchas(scan: RepoScan) -> List[str]:
    gotchas = [
        "This static map is generated without a model; treat summaries as wayfinding hints, not source-of-truth architecture.",
    ]
    if any(".sim-worktrees/" in f.path for f in scan.files):
        gotchas.append("Generated or temporary worktree directories were detected; keep them excluded from orientation scans.")
    if not scan.docs:
        gotchas.append("No documentation files were detected; agents may need to rely on source manifests and directory names.")
    return gotchas


def _static_code_areas(scan: RepoScan) -> List[Dict[str, Any]]:
    areas: List[Dict[str, Any]] = []
    area_candidates = _area_candidates(scan)
    for index, (top, count) in enumerate(area_candidates[:12], start=1):
        if top in {".", ".skyhook"}:
            continue
        related_docs = [doc.path for doc in _ranked_docs(scan) if doc.path.startswith(top + "/")][:5]
        entrypoints = [
            f.path
            for f in sorted(scan.files, key=lambda item: _entrypoint_sort_key(item.path))
            if f.path.startswith(top + "/") and f.kind in {"manifest", "source"}
        ][:8]
        areas.append(
            {
                "id": _area_id(top, index),
                "name": top,
                "purpose": f"Top-level area with {count} scanned files.",
                "paths": [top],
                "entrypoints": entrypoints,
                "relatedDocs": related_docs,
                "confidence": "medium",
            }
        )
    return areas


def _static_docs(scan: RepoScan) -> List[Dict[str, Any]]:
    return [
        {
            "path": doc.path,
            "kind": doc.doc_kind or "unknown",
            "title": doc.title or doc.path,
            "summary": doc.snippet[:220] if doc.snippet else "Documentation file discovered by Skyhook.",
            "whenToRead": _when_to_read(doc.doc_kind),
        }
        for doc in _ranked_docs(scan)[:40]
    ]


def _static_architecture(scan: RepoScan) -> List[Dict[str, Any]]:
    arch_docs = [
        doc
        for doc in _ranked_docs(scan)
        if doc.doc_kind in {"adr", "architecture", "design", "c4"}
        and not _is_low_signal_workflow_doc(doc.path)
    ]
    return [
        {
            "name": doc.title or doc.path,
            "kind": "module-map" if doc.path.endswith("CODE_MAP.md") else doc.doc_kind,
            "paths": [doc.path],
            "summary": doc.snippet[:240] if doc.snippet else "Architecture or design document.",
        }
        for doc in arch_docs[:25]
    ]


def _static_symbols(scan: RepoScan) -> List[Dict[str, str]]:
    symbols: List[Dict[str, str]] = []
    for area in _static_code_areas(scan):
        area_paths = area.get("paths", []) or []
        for record in scan.sources:
            if not any(record.path == path or record.path.startswith(path + "/") for path in area_paths):
                continue
            if record.symbols:
                for symbol in record.symbols[:6]:
                    symbols.append(project_symbol(symbol, record.path, area["id"]))
            elif record.path in area["entrypoints"][:5]:
                symbols.append(
                    {
                        "name": record.path.split("/")[-1],
                        "kind": _symbol_kind(record.path),
                        "path": record.path,
                        "areaId": area["id"],
                    }
                )
    return symbols[:120]


def _when_to_read(kind: str) -> str:
    if kind == "readme":
        return "Read first for setup, commands, and project overview."
    if kind == "adr":
        return "Read before changing architecture or revisiting design decisions."
    if kind in {"architecture", "c4", "design"}:
        return "Read before changing module boundaries, data flow, or large features."
    if kind == "runbook":
        return "Read when operating, deploying, or debugging production behavior."
    if kind == "test":
        return "Read before adding or changing tests."
    return "Read when its path or title matches the task."


def _symbol_kind(path: str) -> str:
    lower = path.lower()
    if "test" in lower:
        return "test"
    if "route" in lower or "controller" in lower:
        return "route"
    if "service" in lower:
        return "service"
    if "repository" in lower or "repo" in lower:
        return "repository"
    if "model" in lower or "entity" in lower or "dto" in lower:
        return "model"
    if path.endswith(("package.json", "pyproject.toml", "go.mod", "build.gradle", "build.gradle.kts")):
        return "module"
    return "other"


def _area_id(name: str, index: int) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or f"area-{index}"


def _ranked_docs(scan: RepoScan) -> List[Any]:
    return sorted(scan.docs, key=lambda doc: (_doc_priority(doc.path, doc.doc_kind), doc.path))


def _doc_priority(path: str, kind: str) -> int:
    preferred = {
        "CLAUDE.md": 0,
        "AGENTS.md": 1,
        "README.md": 2,
        ".claude/ROUTER.md": 3,
        ".claude/CODE_MAP.md": 4,
        "docs/ARCHITECTURE.md": 5,
        ".claude/context/architecture.md": 6,
        ".claude/context/decisions.md": 7,
        ".claude/context/stack.md": 8,
        ".claude/context/setup.md": 9,
        ".claude/patterns/INDEX.md": 10,
        ".claude/LESSONS.md": 11,
    }
    if path in preferred:
        return preferred[path]
    if path.startswith("docs/") and kind in {"architecture", "design", "adr", "c4"}:
        return 20
    if path.startswith(".claude/docs/"):
        return 30
    if path.startswith(".claude/patterns/"):
        return 35
    if path.endswith("/README.md") or kind == "readme":
        return 40
    if kind in {"architecture", "adr", "design", "c4"}:
        return 50
    if kind in {"runbook", "test", "api"}:
        return 60
    if _is_low_signal_workflow_doc(path):
        return 90
    return 70


def _is_low_signal_workflow_doc(path: str) -> bool:
    return path.startswith((".claude/agents/", ".claude/teams/", ".claude/skills/", ".claude/tasks/"))


def _is_code_area_top(top: str, scan: RepoScan) -> bool:
    if top in {".", ".skyhook", ".claude", ".github", "docs", "tasks"}:
        return False
    prefix = top + "/"
    return any(record.path.startswith(prefix) and record.kind in {"source", "manifest"} for record in scan.files)


def _area_candidates(scan: RepoScan) -> List[tuple[str, int]]:
    counts: dict[str, int] = {}
    for top, count in scan.top_dirs.items():
        if _is_code_area_top(top, scan):
            counts[top] = max(counts.get(top, 0), count)

    paths = {record.path for record in scan.files}
    manifest_names = {
        "build.gradle",
        "build.gradle.kts",
        "Package.swift",
        "go.mod",
        "mix.exs",
        "package.json",
        "pom.xml",
        "pyproject.toml",
    }
    for record in scan.files:
        name = record.path.rsplit("/", 1)[-1]
        if name not in manifest_names or "/" not in record.path:
            continue
        root = record.path.rsplit("/", 1)[0]
        if root in {".claude", ".github", "docs"}:
            continue
        counts[root] = max(counts.get(root, 0), sum(1 for path in paths if path.startswith(root + "/")))

    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))


def _entrypoint_sort_key(path: str) -> tuple[int, str]:
    name = path.rsplit("/", 1)[-1]
    if name in {"build.gradle.kts", "build.gradle", "settings.gradle.kts", "settings.gradle", "Package.swift", "go.mod", "pyproject.toml"}:
        return (0, path)
    if "/src/main/" in path:
        if name.endswith(("Application.kt", "Application.java", "Activity.kt", "Activity.java", "NavGraph.kt", "Routes.kt", "Module.kt")):
            return (1, path)
        if name.endswith(("ViewModel.kt", "Repository.kt", "Dao.kt", "Database.kt", "Service.kt")):
            return (2, path)
        return (3, path)
    if "/src/test/" in path or "/src/androidTest/" in path:
        return (6, path)
    return (4, path)
