from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


DEFAULT_EXCLUDES = [
    ".skyhook",
    ".git",
    ".gradle",
    ".idea",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".sim-worktrees",
    ".tox",
    ".venv",
    ".worktrees",
    "__pycache__",
    "build",
    "coverage",
    "DerivedData",
    "dist",
    "node_modules",
    "target",
    "vendor",
]

DEFAULT_DOC_GLOBS = [
    "README*",
    "AGENTS.md",
    "CLAUDE.md",
    "CODE_MAP.md",
    "docs/**/*.md",
    "doc/**/*.md",
    "adr/**/*.md",
    "adrs/**/*.md",
    "architecture/**/*.md",
    "design/**/*.md",
    "**/*ADR*.md",
    "**/*C4*.md",
    "**/*architecture*.md",
    "**/*design*.md",
    "**/*runbook*.md",
]


@dataclass
class ModelConfig:
    provider: str = "auto"
    model: str = "auto"
    base_url: Optional[str] = None


@dataclass
class ScanConfig:
    include: List[str] = field(default_factory=lambda: ["."])
    exclude: List[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDES))
    max_files: int = 5000
    max_doc_bytes: int = 12000
    max_source_samples: int = 300


@dataclass
class DocsConfig:
    extra_globs: List[str] = field(default_factory=lambda: list(DEFAULT_DOC_GLOBS))


@dataclass
class SkyhookConfig:
    version: int = 1
    output_dir: str = ".skyhook"
    model: ModelConfig = field(default_factory=ModelConfig)
    scan: ScanConfig = field(default_factory=ScanConfig)
    docs: DocsConfig = field(default_factory=DocsConfig)


def default_config() -> SkyhookConfig:
    return SkyhookConfig()


def load_config(repo_root: Path, config_path: Optional[str] = None) -> SkyhookConfig:
    path = Path(config_path) if config_path else repo_root / ".skyhook" / "config.yaml"
    if not path.exists():
        return default_config()
    raw = path.read_text(encoding="utf-8")
    data = _parse_minimal_yaml(raw)
    cfg = default_config()
    if "version" in data:
        cfg.version = int(data["version"])
    if "outputDir" in data:
        cfg.output_dir = str(data["outputDir"])
    if "output_dir" in data:
        cfg.output_dir = str(data["output_dir"])
    model = data.get("model", {})
    if isinstance(model, dict):
        cfg.model.provider = str(model.get("provider", cfg.model.provider))
        cfg.model.model = str(model.get("model", cfg.model.model))
        if model.get("baseUrl") or model.get("base_url"):
            cfg.model.base_url = str(model.get("baseUrl") or model.get("base_url"))
    scan = data.get("scan", {})
    if isinstance(scan, dict):
        cfg.scan.include = _string_list(scan.get("include"), cfg.scan.include)
        cfg.scan.exclude = _string_list(scan.get("exclude"), cfg.scan.exclude)
        cfg.scan.max_files = int(scan.get("maxFiles", scan.get("max_files", cfg.scan.max_files)))
        cfg.scan.max_doc_bytes = int(scan.get("maxDocBytes", scan.get("max_doc_bytes", cfg.scan.max_doc_bytes)))
        cfg.scan.max_source_samples = int(
            scan.get("maxSourceSamples", scan.get("max_source_samples", cfg.scan.max_source_samples))
        )
    docs = data.get("docs", {})
    if isinstance(docs, dict):
        cfg.docs.extra_globs = _string_list(docs.get("extraGlobs", docs.get("extra_globs")), cfg.docs.extra_globs)
    return cfg


def _string_list(value: Any, fallback: List[str]) -> List[str]:
    if value is None:
        return fallback
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str):
        return [value]
    return fallback


def _parse_minimal_yaml(raw: str) -> Dict[str, Any]:
    """Parse the small YAML subset used by .skyhook/config.yaml.

    This intentionally avoids a runtime PyYAML dependency. It supports nested maps
    and dash lists with two-space indentation, which is enough for Skyhook config.
    """
    root: Dict[str, Any] = {}
    stack: List[tuple[int, Dict[str, Any]]] = [(-1, root)]
    last_key_at_indent: Dict[int, str] = {}

    for original in raw.splitlines():
        line = original.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        text = line.strip()

        while stack and indent <= stack[-1][0]:
            stack.pop()
        current = stack[-1][1]

        if text.startswith("- "):
            key = last_key_at_indent.get(indent - 2)
            if key is None:
                continue
            parent = stack[-2][1] if len(stack) >= 2 and stack[-1][0] == indent - 2 else stack[-1][1]
            if not isinstance(parent.get(key), list):
                parent[key] = []
            parent[key].append(_parse_scalar(text[2:].strip()))
            continue

        if ":" not in text:
            continue
        key, value = text.split(":", 1)
        key = key.strip()
        value = value.strip()
        last_key_at_indent[indent] = key
        if value == "":
            child: Dict[str, Any] = {}
            current[key] = child
            stack.append((indent, child))
        elif value == "[]":
            current[key] = []
        else:
            current[key] = _parse_scalar(value)
    return root


def _parse_scalar(value: str) -> Any:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    lower = value.lower()
    if lower == "true":
        return True
    if lower == "false":
        return False
    if lower == "null":
        return None
    try:
        return int(value)
    except ValueError:
        return value
