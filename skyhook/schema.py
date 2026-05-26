from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping

SCHEMA_VERSION = 1


def empty_map(repo_name: str, root: str) -> Dict[str, Any]:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "repo": {
            "name": repo_name,
            "root": root,
            "primaryLanguages": [],
            "detectedFrameworks": [],
        },
        "scan": {
            "digest": "",
            "fileCount": 0,
            "generatedBy": "skyhook",
        },
        "orientation": {
            "summary": "",
            "agentStartHere": [],
            "knownGotchas": [],
        },
        "codeAreas": [],
        "docs": [],
        "architecture": [],
        "symbols": [],
        "tests": [],
    }


def validate_map(data: Mapping[str, Any]) -> List[str]:
    errors: List[str] = []
    if data.get("schemaVersion") != SCHEMA_VERSION:
        errors.append(f"schemaVersion must be {SCHEMA_VERSION}")
    for key in ["repo", "scan", "orientation", "codeAreas", "docs", "architecture", "symbols"]:
        if key not in data:
            errors.append(f"missing top-level key: {key}")
    if not isinstance(data.get("codeAreas", []), list):
        errors.append("codeAreas must be a list")
    if not isinstance(data.get("docs", []), list):
        errors.append("docs must be a list")
    if not isinstance(data.get("symbols", []), list):
        errors.append("symbols must be a list")
    if "tests" in data and not isinstance(data.get("tests", []), list):
        errors.append("tests must be a list")
    return errors


def canonical_json(data: Mapping[str, Any]) -> str:
    return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def digest_records(records: Iterable[Mapping[str, Any]]) -> str:
    h = hashlib.sha256()
    for record in records:
        h.update(json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


def write_json(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(canonical_json(data), encoding="utf-8")


def read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))
