from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, Iterable, List, Mapping, Sequence


STOP_WORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "how",
    "i",
    "in",
    "into",
    "is",
    "it",
    "of",
    "on",
    "or",
    "that",
    "the",
    "this",
    "to",
    "we",
    "with",
}


# Shared, agent-facing navigation protocol appended to the profile guidance below.
# The whole point of Skyhook is a smaller, righter context, and that only pays off if
# the agent asks the graph before it grep-explores. These bullets encode that, and
# keep it honest: trust the precise edges, fall back to grep for the rest.
_GRAPH_FIRST = [
    "Ask the graph before grep-exploring: query callers, callees, and blast-radius "
    "(`skyhook graph query ...`, or the Skyhook MCP tools) for the symbols and files below.",
    "Trust precise edges (same_file/qualified/imported/same_package); grep only for edges "
    "marked global/approximate, or when the graph returns nothing. An empty result means "
    "verify by hand, not that nothing is there.",
]
_GRAPH_SCOPING = [
    "Use the graph (callers, blast-radius) to scope impact before estimating, but prefer open "
    "questions and boundaries over edit targets.",
]


ROUTE_PROFILES: Dict[str, Dict[str, Any]] = {
    "product_planning": {
        "label": "Product Planning",
        "guidance": [
            "Start with product-facing docs, existing user flows, domain language, and public contracts.",
            "Prefer questions, scope boundaries, and user outcomes over edit targets.",
            *_GRAPH_SCOPING,
        ],
        "docKinds": ["readme", "design", "api", "architecture"],
        "includeEdits": False,
        "includeTests": False,
        "extraSearchTerms": ["user", "workflow", "persona", "story", "value", "problem"],
    },
    "requirements_planning": {
        "label": "Requirements Planning",
        "guidance": [
            "Find existing requirements, domain contracts, acceptance criteria, and testing guidance.",
            "Capture open questions before proposing implementation details.",
            *_GRAPH_SCOPING,
        ],
        "docKinds": ["readme", "design", "api", "test", "architecture"],
        "includeEdits": False,
        "includeTests": True,
        "extraSearchTerms": ["requirement", "acceptance", "criteria", "contract", "constraint"],
    },
    "technical_breakdown": {
        "label": "Technical Breakdown",
        "guidance": [
            "Identify affected code areas, architecture boundaries, integration points, tests, and likely issue slices.",
            "Prefer dependency order and verification strategy before implementation details.",
            *_GRAPH_FIRST,
        ],
        "docKinds": ["architecture", "adr", "design", "runbook", "test"],
        "includeEdits": True,
        "includeTests": True,
        "extraSearchTerms": ["boundary", "dependency", "integration", "migration", "test"],
    },
    "implementation": {
        "label": "Implementation",
        "guidance": [
            "Read the route pack, then inspect likely edit targets and relevant tests before changing files.",
            "Preserve architecture constraints and run the narrowest useful verification commands.",
            *_GRAPH_FIRST,
        ],
        "docKinds": ["architecture", "adr", "design", "test", "runbook"],
        "includeEdits": True,
        "includeTests": True,
        "extraSearchTerms": ["service", "repository", "controller", "viewmodel", "test"],
    },
    "code_review": {
        "label": "Code Review",
        "guidance": [
            "Prioritize changed or named files, nearby tests, public contracts, and architecture rules.",
            "Look for behavioral regressions, missing verification, and boundary violations.",
            *_GRAPH_FIRST,
        ],
        "docKinds": ["architecture", "adr", "test", "runbook"],
        "includeEdits": True,
        "includeTests": True,
        "extraSearchTerms": ["regression", "risk", "contract", "test", "boundary"],
    },
    "bug_hunt": {
        "label": "Bug Hunt",
        "guidance": [
            "Prioritize symptoms, failing tests, logs, public contracts, and the smallest reproducible path.",
            "Use route evidence as a starting point, then verify against runtime or test failure data.",
            *_GRAPH_FIRST,
        ],
        "docKinds": ["runbook", "test", "architecture", "api"],
        "includeEdits": True,
        "includeTests": True,
        "extraSearchTerms": ["error", "failure", "bug", "exception", "regression", "log"],
    },
}


DEFAULT_PROFILE = "implementation"


def profile_names() -> List[str]:
    return sorted(ROUTE_PROFILES)


def normalize_profile(profile: str | None) -> str:
    if not profile:
        return DEFAULT_PROFILE
    normalized = profile.strip().lower().replace("-", "_")
    aliases = {
        "bug_fix": "bug_hunt",
        "bugfix": "bug_hunt",
        "breakdown": "technical_breakdown",
        "implement": "implementation",
        "planning": "product_planning",
        "review": "code_review",
        "requirements": "requirements_planning",
        "story": "product_planning",
        "technical": "technical_breakdown",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in ROUTE_PROFILES:
        raise ValueError(f"unsupported route profile: {profile}. Supported profiles: {', '.join(profile_names())}")
    return normalized


def build_route(
    data: Mapping[str, Any], task: str, profile: str | None = None, graph: Any = None
) -> Dict[str, Any]:
    cleaned_task = task.strip()
    if not cleaned_task:
        raise ValueError("skyhook route requires non-empty task text")
    route_profile = normalize_profile(profile)
    profile_cfg = ROUTE_PROFILES[route_profile]
    tokens = task_tokens(cleaned_task)
    areas = _rank_items(data.get("codeAreas", []) or [], tokens, cleaned_task, _area_text)
    docs = _rank_items(data.get("docs", []) or [], tokens, cleaned_task, lambda item: _doc_text(item, profile_cfg))
    arch = _rank_items(data.get("architecture", []) or [], tokens, cleaned_task, _arch_text)
    symbols = _rank_items(data.get("symbols", []) or [], tokens, cleaned_task, _symbol_text)
    tests = _rank_items(data.get("tests", []) or [], tokens, cleaned_task, _test_text)

    top_areas = [item for score, item in areas if score > 0][:3]
    if not top_areas and data.get("codeAreas"):
        top_areas = list(data.get("codeAreas", []) or [])[:1]
    top_area_ids = {str(area.get("id") or area.get("name") or "") for area in top_areas}

    read_first = _read_first(data, top_areas, docs, arch, route_profile)
    likely_edits = _likely_edit_targets(top_areas, symbols, top_area_ids) if profile_cfg["includeEdits"] else []
    relevant_tests = _relevant_tests(top_areas, tests, top_area_ids) if profile_cfg["includeTests"] else []
    architecture = _architecture_paths(top_areas, arch)
    constraints = _constraints(data, top_areas)
    evidence = _evidence(top_areas, docs, arch, symbols, tests)
    confidence = _confidence(areas, docs, symbols, tests)
    search_terms = _search_terms(tokens, symbols, likely_edits, profile_cfg.get("extraSearchTerms", []))

    route = {
        "schemaVersion": 1,
        "id": _route_id(cleaned_task, route_profile),
        "profile": route_profile,
        "profileLabel": profile_cfg["label"],
        "profileGuidance": profile_cfg["guidance"],
        "task": cleaned_task,
        "confidence": confidence,
        "readFirst": read_first[:12],
        "likelyEditTargets": likely_edits[:20],
        "relevantTests": relevant_tests[:20],
        "architecture": architecture[:12],
        "constraints": constraints[:16],
        "searchTerms": search_terms[:16],
        "evidence": evidence[:30],
    }
    if graph is not None:
        try:
            st = graph.stats()
            resolved = st.get("resolved_calls") or 0
            precise = st.get("precise_calls") or 0
            route["graphCoverage"] = {
                "precisionOfResolvedPct": round(100 * precise / resolved, 1) if resolved else None,
                "note": "Call edges carry confidence grades. Precise edges are reliable; the graph "
                        "does not bind every in-repo call, so treat thin or missing callers as a "
                        "prompt to verify, not proof there are none.",
            }
        except Exception:
            pass
        enrichment = _graph_enrichment(graph, symbols, likely_edits)
        if enrichment.get("callChains"):
            route["callChains"] = enrichment["callChains"]
        if enrichment.get("blastRadius"):
            route["blastRadius"] = enrichment["blastRadius"]
    return route


def _graph_enrichment(graph: Any, symbols, likely_edits) -> Dict[str, Any]:
    """Use the AST graph to embed precise call chains + blast radius in the route,
    so the agent gets structural context without grep-exploring. Best-effort."""
    seeds: List[str] = []
    for score, symbol in symbols:
        if score <= 0:
            continue
        name = symbol.get("name")
        if name and name not in seeds:
            seeds.append(name)
        if len(seeds) >= 5:
            break

    call_chains = []
    for name in seeds:
        try:
            callers = graph.callers_of(name)
            callees = graph.callees_of(name)
        except Exception:
            continue
        if callers or callees:
            edges = callers[:8] + callees[:8]
            call_chains.append(
                {
                    "symbol": name,
                    "callers": [
                        {"name": c["name"], "path": c.get("path"), "resolution": c.get("resolution")}
                        for c in callers[:8]
                    ],
                    "callees": [
                        {"name": c["name"], "path": c.get("path"), "resolution": c.get("resolution")}
                        for c in callees[:8]
                    ],
                    "approximate": any(c.get("approximate", True) for c in edges),
                }
            )

    blast = None
    edit_paths = [e for e in likely_edits if isinstance(e, str) and e]
    if edit_paths:
        try:
            br = graph.blast_radius(edit_paths[0], depth=2)
            if br.get("impactedFiles"):
                blast = {
                    "target": br["target"],
                    "impactedFiles": br["impactedFiles"][:20],
                    "approximate": br.get("approximate", True),
                    "resolutionSummary": br.get("resolutionSummary"),
                }
        except Exception:
            blast = None

    return {"callChains": call_chains, "blastRadius": blast}


def task_tokens(task: str) -> List[str]:
    raw_tokens = re.findall(r"[A-Za-z0-9_./:-]+", task.lower())
    tokens: List[str] = []
    for token in raw_tokens:
        pieces = [token]
        if "/" in token or "." in token or ":" in token or "-" in token or "_" in token:
            pieces.extend(re.split(r"[/.:_-]+", token))
        for piece in pieces:
            piece = piece.strip()
            if len(piece) < 2 or piece in STOP_WORDS:
                continue
            tokens.append(piece)
    return _unique(tokens)


def _rank_items(
    items: Sequence[Mapping[str, Any]],
    tokens: Sequence[str],
    task: str,
    text_fn,
) -> List[tuple[int, Mapping[str, Any]]]:
    ranked = []
    for item in items:
        text = text_fn(item).lower()
        score = _score(text, tokens, task)
        ranked.append((score, item))
    return sorted(ranked, key=lambda pair: (-pair[0], _stable_label(pair[1])))


def _score(text: str, tokens: Sequence[str], task: str) -> int:
    score = 0
    normalized_text = _normalize(text)
    normalized_task = _normalize(task)
    for token in tokens:
        if token in text:
            score += 3
        if _normalize(token) and _normalize(token) in normalized_text:
            score += 1
    for fragment in _path_fragments(task):
        if fragment and fragment.lower() in text:
            score += 12
    if normalized_task and normalized_task in normalized_text:
        score += 20
    return score


def _read_first(
    data: Mapping[str, Any],
    top_areas: List[Mapping[str, Any]],
    docs: List[tuple[int, Mapping[str, Any]]],
    arch: List[tuple[int, Mapping[str, Any]]],
    profile: str,
) -> List[str]:
    values: List[str] = []
    values.extend(doc.get("path", "") for score, doc in docs if score > 0)
    for _score_value, item in arch:
        values.extend(item.get("paths", []) or [])
    for area in top_areas:
        values.extend(area.get("relatedDocs", []) or [])
        if profile not in {"product_planning", "requirements_planning"}:
            values.extend(area.get("entrypoints", []) or [])
    values.extend((data.get("orientation") or {}).get("agentStartHere", []) or [])
    return _unique(values)


def _likely_edit_targets(
    top_areas: List[Mapping[str, Any]],
    symbols: List[tuple[int, Mapping[str, Any]]],
    top_area_ids: set[str],
) -> List[str]:
    values: List[str] = []
    for score, symbol in symbols:
        if score <= 0 and symbol.get("areaId") not in top_area_ids:
            continue
        if symbol.get("kind") == "test":
            continue
        values.append(str(symbol.get("path", "")))
    for area in top_areas:
        values.extend(area.get("publicContracts", []) or [])
        values.extend(area.get("entrypoints", []) or [])
    return _unique(values)


def _relevant_tests(
    top_areas: List[Mapping[str, Any]],
    tests: List[tuple[int, Mapping[str, Any]]],
    top_area_ids: set[str],
) -> List[str]:
    values: List[str] = []
    for area in top_areas:
        values.extend(area.get("relevantTests", []) or [])
    for score, test in tests:
        if score > 0:
            values.append(str(test.get("path", "")))
    return _unique(values)


def _architecture_paths(top_areas: List[Mapping[str, Any]], arch: List[tuple[int, Mapping[str, Any]]]) -> List[str]:
    values: List[str] = []
    for score, item in arch:
        if score > 0:
            values.extend(item.get("paths", []) or [])
    for area in top_areas:
        for evidence in area.get("evidence", []) or []:
            if evidence.get("kind") == "architecture":
                values.append(str(evidence.get("path", "")))
    return _unique(values)


def _constraints(data: Mapping[str, Any], top_areas: List[Mapping[str, Any]]) -> List[str]:
    values: List[str] = []
    for area in top_areas:
        values.extend(area.get("changeRules", []) or [])
        values.extend(f"Danger zone: {path}" for path in area.get("dangerZones", []) or [])
    values.extend((data.get("orientation") or {}).get("knownGotchas", []) or [])
    return _unique(values)


def _evidence(
    top_areas: List[Mapping[str, Any]],
    docs: List[tuple[int, Mapping[str, Any]]],
    arch: List[tuple[int, Mapping[str, Any]]],
    symbols: List[tuple[int, Mapping[str, Any]]],
    tests: List[tuple[int, Mapping[str, Any]]],
) -> List[Dict[str, str]]:
    evidence: List[Dict[str, str]] = []
    for area in top_areas:
        for item in area.get("evidence", []) or []:
            evidence.append({"kind": str(item.get("kind", "area")), "path": str(item.get("path", "")), "reason": str(item.get("reason", ""))})
    for score, doc in docs[:8]:
        if score > 0:
            evidence.append({"kind": "doc", "path": str(doc.get("path", "")), "reason": f"Matched task with score {score}"})
    for score, item in arch[:8]:
        if score > 0:
            for path in item.get("paths", []) or []:
                evidence.append({"kind": "architecture", "path": str(path), "reason": f"Matched task with score {score}"})
    for score, symbol in symbols[:12]:
        if score > 0:
            evidence.append({"kind": "symbol", "path": str(symbol.get("path", "")), "reason": f"Matched `{symbol.get('name', '')}`"})
    for score, test in tests[:8]:
        if score > 0:
            evidence.append({"kind": "test", "path": str(test.get("path", "")), "reason": f"Matched task with score {score}"})
    return _unique_evidence(evidence)


def _confidence(
    areas: List[tuple[int, Mapping[str, Any]]],
    docs: List[tuple[int, Mapping[str, Any]]],
    symbols: List[tuple[int, Mapping[str, Any]]],
    tests: List[tuple[int, Mapping[str, Any]]],
) -> str:
    top_area = areas[0][0] if areas else 0
    corroboration = sum(1 for ranked in [docs, symbols, tests] if ranked and ranked[0][0] > 0)
    if top_area >= 12 and corroboration >= 2:
        return "high"
    if top_area >= 6 or corroboration >= 2:
        return "medium"
    return "low"


def _search_terms(
    tokens: Sequence[str],
    symbols: List[tuple[int, Mapping[str, Any]]],
    edits: Sequence[str],
    extra_terms: Sequence[str],
) -> List[str]:
    values = list(tokens)
    for score, symbol in symbols:
        if score > 0:
            values.append(str(symbol.get("name", "")))
    for path in edits:
        name = path.rsplit("/", 1)[-1].split(".", 1)[0]
        values.append(name)
    values.extend(extra_terms)
    return _unique(value for value in values if len(value) >= 3)


def _area_text(area: Mapping[str, Any]) -> str:
    return " ".join(
        [
            str(area.get("id", "")),
            str(area.get("name", "")),
            str(area.get("purpose", "")),
            " ".join(str(value) for value in area.get("paths", []) or []),
            " ".join(str(value) for value in area.get("entrypoints", []) or []),
            " ".join(str(value) for value in area.get("relatedDocs", []) or []),
            " ".join(str(value) for value in area.get("responsibilities", []) or []),
            " ".join(str(value) for value in area.get("publicContracts", []) or []),
        ]
    )


def _doc_text(doc: Mapping[str, Any], profile_cfg: Mapping[str, Any]) -> str:
    parts = [str(doc.get(key, "")) for key in ["path", "kind", "title", "summary", "whenToRead"]]
    if doc.get("kind") in (profile_cfg.get("docKinds") or []):
        parts.extend([str(doc.get("kind", ""))] * 6)
    return " ".join(parts)


def _arch_text(item: Mapping[str, Any]) -> str:
    return " ".join(
        [
            str(item.get("name", "")),
            str(item.get("kind", "")),
            str(item.get("summary", "")),
            " ".join(str(path) for path in item.get("paths", []) or []),
        ]
    )


def _symbol_text(symbol: Mapping[str, Any]) -> str:
    return " ".join(str(symbol.get(key, "")) for key in ["name", "kind", "path", "areaId"])


def _test_text(test: Mapping[str, Any]) -> str:
    return " ".join(
        [
            str(test.get("path", "")),
            str(test.get("framework", "")),
            " ".join(str(value) for value in test.get("targetHints", []) or []),
            " ".join(str(value) for value in test.get("symbols", []) or []),
        ]
    )


def _route_id(task: str, profile: str) -> str:
    digest = hashlib.sha256(f"{profile}\n{task}".encode("utf-8")).hexdigest()[:12]
    slug = "-".join(task_tokens(task)[:6])
    profile_slug = profile.replace("_", "-")
    return f"{profile_slug}-{digest}-{slug}" if slug else f"{profile_slug}-{digest}"


def _path_fragments(task: str) -> List[str]:
    return [fragment for fragment in re.findall(r"[A-Za-z0-9_./:-]+\.[A-Za-z0-9_]+|[A-Za-z0-9_-]+/[A-Za-z0-9_./:-]+", task)]


def _stable_label(item: Mapping[str, Any]) -> str:
    return str(item.get("path") or item.get("id") or item.get("name") or "")


def _normalize(value: str) -> str:
    return "".join(ch.lower() for ch in value if ch.isalnum())


def _unique(values: Iterable[str]) -> List[str]:
    seen = set()
    result: List[str] = []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _unique_evidence(values: Iterable[Mapping[str, str]]) -> List[Dict[str, str]]:
    seen = set()
    result: List[Dict[str, str]] = []
    for value in values:
        key = (value.get("kind"), value.get("path"), value.get("reason"))
        if key in seen:
            continue
        seen.add(key)
        result.append(dict(value))
    return result
