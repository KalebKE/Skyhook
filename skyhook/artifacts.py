from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from .render import markdown_outputs, render_route_markdown, write_markdown_outputs
from .schema import canonical_json, read_json, validate_map, write_json


def output_dir(repo_root: Path, configured: str, override: Optional[str] = None) -> Path:
    raw = override or configured
    path = Path(raw)
    if not path.is_absolute():
        path = repo_root / path
    return path


def write_outputs(out_dir: Path, data: Mapping[str, Any]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    write_json(out_dir / "map.json", data)
    write_markdown_outputs(out_dir, data)


def write_route(out_dir: Path, route: Mapping[str, Any]) -> list[Path]:
    route_id = str(route.get("id") or "route")
    route_dir = out_dir / "routes"
    route_dir.mkdir(parents=True, exist_ok=True)
    json_path = route_dir / f"{route_id}.json"
    md_path = route_dir / f"{route_id}.md"
    json_path.write_text(canonical_json(route), encoding="utf-8")
    md_path.write_text(render_route_markdown(route), encoding="utf-8")
    return [md_path, json_path]


def load_existing_map(out_dir: Path) -> Optional[Dict[str, Any]]:
    path = out_dir / "map.json"
    if not path.exists():
        return None
    return read_json(path)


def check_outputs(out_dir: Path, current_digest: str) -> list[str]:
    errors: list[str] = []
    json_path = out_dir / "map.json"
    base_paths = [
        out_dir / "map.json",
        out_dir / "map.md",
        out_dir / "INDEX.md",
        out_dir / "docs.md",
        out_dir / "architecture.md",
        out_dir / "tests.md",
    ]
    for path in base_paths:
        if not path.exists():
            errors.append(f"missing artifact: {path}")
    if not json_path.exists():
        return errors
    try:
        data = read_json(json_path)
    except json.JSONDecodeError as exc:
        errors.append(f"invalid JSON in {json_path}: {exc}")
        return errors
    errors.extend(validate_map(data))
    for rel_path in markdown_outputs(data):
        path = out_dir / rel_path
        if not path.exists():
            errors.append(f"missing artifact: {path}")
    stored_digest = ((data.get("scan") or {}).get("digest") or "")
    if stored_digest != current_digest:
        errors.append("map is stale: repository scan digest differs from .skyhook/map.json")
    return errors


def outputs_would_change(out_dir: Path, data: Mapping[str, Any]) -> bool:
    contents = {"map.json": canonical_json(data), **markdown_outputs(data)}
    return any(_path_would_change(out_dir / rel_path, content) for rel_path, content in contents.items())


def _path_would_change(path: Path, content: str) -> bool:
    if not path.exists():
        return True
    return path.read_text(encoding="utf-8") != content
