from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

from .scanner import FileRecord, RepoScan


def enrich_map(data: Mapping[str, Any], scan: RepoScan) -> Dict[str, Any]:
    enriched: Dict[str, Any] = json.loads(json.dumps(data))
    tests = build_test_map(scan)
    symbols = build_symbol_map(scan, enriched.get("codeAreas", []) or [])
    enriched["tests"] = tests
    enriched["symbols"] = _unique_symbols(list(enriched.get("symbols", []) or []) + symbols)
    enriched["codeAreas"] = [
        enrich_area(area, scan, tests, enriched.get("docs", []) or [], enriched.get("architecture", []) or [])
        for area in enriched.get("codeAreas", []) or []
    ]
    return enriched


def build_test_map(scan: RepoScan) -> List[Dict[str, Any]]:
    tests: List[Dict[str, Any]] = []
    for record in scan.files:
        if not record.is_test:
            continue
        tests.append(
            {
                "path": record.path,
                "language": record.language,
                "framework": _test_framework(record),
                "targetHints": _target_hints(record.path),
                "symbols": [symbol["name"] for symbol in record.symbols[:8]],
            }
        )
    return tests[:500]


# Optional AST-derived keys carried from a record symbol into the map symbol.
_SYMBOL_EXTRA_KEYS = ("structuralKind", "line", "endLine", "scope", "signature")


def project_symbol(symbol: Mapping[str, Any], path: str, area_id: str) -> Dict[str, Any]:
    """Map a scanned record symbol into a map.json symbol, carrying AST extras."""
    out: Dict[str, Any] = {
        "name": symbol.get("name"),
        "kind": symbol.get("kind"),
        "path": path,
        "areaId": area_id,
    }
    for key in _SYMBOL_EXTRA_KEYS:
        value = symbol.get(key)
        if value not in (None, ""):
            out[key] = value
    return out


def build_symbol_map(scan: RepoScan, areas: Iterable[Mapping[str, Any]]) -> List[Dict[str, str]]:
    symbols: List[Dict[str, str]] = []
    for record in scan.sources:
        area_id = area_for_path(record.path, areas)
        for symbol in record.symbols:
            symbols.append(project_symbol(symbol, record.path, area_id))
    return symbols[:1000]


def enrich_area(
    area: Mapping[str, Any],
    scan: RepoScan,
    tests: List[Mapping[str, Any]],
    docs: List[Mapping[str, Any]],
    architecture: List[Mapping[str, Any]],
) -> Dict[str, Any]:
    result = dict(area)
    area_paths = [str(path) for path in result.get("paths", []) or []]
    area_files = [record for record in scan.files if _path_in_area(record.path, area_paths)]
    source_files = [record for record in area_files if record.kind == "source"]
    area_id = str(result.get("id") or result.get("name") or "")

    result.setdefault("responsibilities", _responsibilities(result, source_files))
    result.setdefault("publicContracts", _public_contracts(source_files))
    result.setdefault("dependencies", _dependencies(source_files))
    result.setdefault("relevantTests", _relevant_tests(area_id, area_paths, tests))
    result.setdefault("verificationCommands", verification_commands(scan, area_paths))
    result.setdefault("changeRules", _change_rules(result, docs, architecture))
    result.setdefault("dangerZones", _danger_zones(area_files))
    result.setdefault("commonTasks", _common_tasks(result, source_files))
    result.setdefault("evidence", _evidence(result, area_files, docs, architecture))
    return result


def area_for_path(path: str, areas: Iterable[Mapping[str, Any]]) -> str:
    best: Optional[tuple[int, str]] = None
    for area in areas:
        area_id = str(area.get("id") or area.get("name") or "")
        for prefix in area.get("paths", []) or []:
            prefix = str(prefix).strip("/")
            if not prefix:
                continue
            if path == prefix or path.startswith(prefix + "/"):
                score = len(prefix)
                if best is None or score > best[0]:
                    best = (score, area_id)
    return best[1] if best else ""


def verification_commands(scan: RepoScan, area_paths: Iterable[str]) -> List[str]:
    paths = {record.path for record in scan.files}
    commands: List[str] = []
    has_gradlew = "gradlew" in paths
    gradle = "./gradlew" if has_gradlew else "gradle"
    if any(path.endswith(("build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts")) for path in paths):
        for area_path in area_paths:
            if area_path and any(p in {f"{area_path}/build.gradle", f"{area_path}/build.gradle.kts"} for p in paths):
                commands.append(f"{gradle} :{area_path.replace('/', ':')}:test")
        commands.append(f"{gradle} test")
    if "pyproject.toml" in paths or "requirements.txt" in paths:
        if any(record.is_test and record.language == "Python" for record in scan.files):
            commands.append("python3 -m unittest discover -s tests -v")
    if "package.json" in paths:
        commands.append("npm test")
    if "Package.swift" in paths:
        commands.append("swift test")
    return _unique(commands)[:6]


def _responsibilities(area: Mapping[str, Any], source_files: List[FileRecord]) -> List[str]:
    purpose = str(area.get("purpose") or "").strip()
    values = [purpose] if purpose else []
    languages = Counter(record.language for record in source_files if record.language)
    if languages:
        values.append("Owns " + ", ".join(language for language, _ in languages.most_common(3)) + " source files in this area.")
    return values[:4]


def _public_contracts(source_files: List[FileRecord]) -> List[str]:
    contracts: List[str] = []
    for record in source_files:
        lower = record.path.lower()
        if any(marker in lower for marker in ["api", "route", "controller", "client", "service", "repository", "dao", "interface", "protocol"]):
            contracts.append(record.path)
    return _unique(contracts)[:12]


def _dependencies(source_files: List[FileRecord]) -> List[str]:
    counts: Counter[str] = Counter()
    for record in source_files:
        for dependency in record.imports:
            if not dependency.startswith((".", "/")):
                counts[dependency.split(",", 1)[0].strip()] += 1
    return [dependency for dependency, _ in counts.most_common(20)]


def _relevant_tests(area_id: str, area_paths: List[str], tests: List[Mapping[str, Any]]) -> List[str]:
    values: List[str] = []
    area_terms = {_normalize(area_id), *(_normalize(path.split("/")[-1]) for path in area_paths)}
    for test in tests:
        path = str(test.get("path", ""))
        normalized = _normalize(path)
        if any(_path_in_area(path, [area_path]) for area_path in area_paths):
            values.append(path)
            continue
        hints = {_normalize(hint) for hint in test.get("targetHints", []) or []}
        if (area_terms & hints) or any(term and term in normalized for term in area_terms):
            values.append(path)
    return _unique(values)[:20]


def _change_rules(area: Mapping[str, Any], docs: List[Mapping[str, Any]], architecture: List[Mapping[str, Any]]) -> List[str]:
    rules = []
    if area.get("relatedDocs"):
        rules.append("Read related docs before changing this area.")
    if architecture:
        rules.append("Check architecture and ADR entries before changing module boundaries or data flow.")
    if any(doc.get("kind") == "test" for doc in docs):
        rules.append("Read testing documentation before adding or changing tests.")
    return rules[:5]


def _danger_zones(area_files: List[FileRecord]) -> List[str]:
    danger = []
    for record in area_files:
        lowered = record.path.lower()
        if any(term in lowered for term in ["legacy", "deprecated", "migration", "generated", "pbxproj", "lock"]):
            danger.append(record.path)
    return _unique(danger)[:12]


def _common_tasks(area: Mapping[str, Any], source_files: List[FileRecord]) -> List[Dict[str, Any]]:
    source_paths = [record.path for record in source_files if not record.is_test][:6]
    if not source_paths:
        return []
    return [
        {
            "taskPattern": f"Change {area.get('name') or area.get('id')}",
            "read": source_paths[:4],
            "editCandidates": source_paths[:6],
            "tests": [record.path for record in source_files if record.is_test][:6],
        }
    ]


def _evidence(
    area: Mapping[str, Any],
    area_files: List[FileRecord],
    docs: List[Mapping[str, Any]],
    architecture: List[Mapping[str, Any]],
) -> List[Dict[str, str]]:
    evidence = [{"kind": "path", "path": path, "reason": "Area path"} for path in area.get("paths", []) or []]
    for path in area.get("entrypoints", []) or []:
        evidence.append({"kind": "entrypoint", "path": path, "reason": "Entrypoint selected for area"})
    for path in area.get("relatedDocs", []) or []:
        evidence.append({"kind": "doc", "path": path, "reason": "Related documentation"})
    for item in architecture[:5]:
        for path in item.get("paths", []) or []:
            evidence.append({"kind": "architecture", "path": path, "reason": "Architecture or design reference"})
    if not evidence and area_files:
        evidence.append({"kind": "path", "path": area_files[0].path, "reason": "Representative scanned file"})
    return evidence[:20]


def _test_framework(record: FileRecord) -> str:
    lower = record.path.lower()
    if record.language == "Python":
        return "pytest/unittest" if "test" in lower else "python"
    if record.language in {"Kotlin", "Java"}:
        if "androidtest" in lower:
            return "Android instrumentation"
        return "JUnit"
    if record.language == "Swift":
        return "XCTest"
    if record.language in {"JavaScript", "TypeScript"}:
        return "Jest/Vitest"
    if record.language == "Go":
        return "go test"
    if record.language == "Elixir":
        return "ExUnit"
    return "unknown"


def _target_hints(path: str) -> List[str]:
    name = Path(path).name
    stem = Path(name).stem
    for suffix in ["Tests", "Test", "Spec", "_test", ".test", ".spec"]:
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
    parts = [part for part in Path(path).parts if part.lower() not in {"test", "tests", "androidtest", "__tests__"}]
    return _unique([stem, *parts[-5:]])


def _path_in_area(path: str, area_paths: Iterable[str]) -> bool:
    for area_path in area_paths:
        prefix = str(area_path).strip("/")
        if not prefix:
            continue
        if path == prefix or path.startswith(prefix + "/"):
            return True
    return False


def _unique_symbols(symbols: List[Mapping[str, Any]]) -> List[Dict[str, str]]:
    seen = set()
    result: List[Dict[str, str]] = []
    for symbol in symbols:
        key = (
            symbol.get("name"),
            symbol.get("path"),
            symbol.get("kind"),
            symbol.get("scope"),
            symbol.get("line"),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append({str(k): str(v) for k, v in symbol.items() if v is not None})
    return result[:1000]


def _unique(values: Iterable[str]) -> List[str]:
    seen = set()
    result = []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _normalize(value: str) -> str:
    return "".join(ch.lower() for ch in value if ch.isalnum())
