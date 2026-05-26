from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Any, Dict, List, Mapping, Optional

from .config import SkyhookConfig
from .scanner import RepoScan
from .schema import empty_map, validate_map


DEFAULT_OPENAI_MODEL = "gpt-4.1-mini"


class ModelError(RuntimeError):
    pass


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
        if not self.api_key:
            raise ModelError("OPENAI_API_KEY or SKYHOOK_API_KEY is required for provider=openai")

    def orient(self, scan: RepoScan, previous: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        facts = scan_facts(scan)
        messages = [
            {
                "role": "system",
                "content": (
                    "You generate compact repository orientation maps for coding agents. "
                    "Return only valid JSON matching the requested schema. "
                    "Do not make a comprehensive index; create fast wayfinding context."
                ),
            },
            {"role": "user", "content": build_prompt(facts, previous)},
        ]
        raw = self._chat(messages)
        data = parse_json_object(raw)
        errors = validate_map(data)
        if errors:
            repair_messages = messages + [
                {"role": "assistant", "content": raw},
                {
                    "role": "user",
                    "content": "Repair the JSON so it satisfies the schema. Errors: " + "; ".join(errors),
                },
            ]
            raw = self._chat(repair_messages)
            data = parse_json_object(raw)
            errors = validate_map(data)
            if errors:
                raise ModelError("model returned invalid map JSON: " + "; ".join(errors))
        data["scan"]["digest"] = scan.digest
        data["scan"]["fileCount"] = len(scan.files)
        data["scan"]["generatedBy"] = "skyhook"
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
            with urllib.request.urlopen(request, timeout=90) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise ModelError(f"model request failed: HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise ModelError(f"model request failed: {exc}") from exc
        try:
            return body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelError("model response did not contain choices[0].message.content") from exc


def choose_orienter(config: SkyhookConfig, provider_override: Optional[str] = None) -> Orienter:
    provider = provider_override or config.model.provider
    if provider == "auto":
        provider = "openai" if (os.environ.get("OPENAI_API_KEY") or os.environ.get("SKYHOOK_API_KEY")) else "static"
    if provider in {"static", "none", "no-model"}:
        return StaticOrienter()
    if provider == "openai":
        return OpenAICompatibleOrienter(config)
    raise ModelError(f"unsupported model provider: {provider}")


def scan_facts(scan: RepoScan) -> Dict[str, Any]:
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
        for source in sorted(scan.sources, key=lambda item: item.path)[:300]
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
        "schema": _schema_instruction(),
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
        + json.dumps(facts, indent=2, ensure_ascii=False)
        + previous_note
    )


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
                    symbols.append(
                        {
                            "name": symbol["name"],
                            "kind": symbol["kind"],
                            "path": record.path,
                            "areaId": area["id"],
                        }
                    )
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
