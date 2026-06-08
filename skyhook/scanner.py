from __future__ import annotations

import hashlib
import os
import re
import subprocess
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

from . import astextract
from .config import SkyhookConfig
from .schema import digest_records


SOURCE_EXTENSIONS = {
    ".c": "C",
    ".cc": "C++",
    ".cpp": "C++",
    ".cs": "C#",
    ".css": "CSS",
    ".dart": "Dart",
    ".ex": "Elixir",
    ".exs": "Elixir",
    ".go": "Go",
    ".groovy": "Groovy",
    ".h": "C/C++",
    ".hpp": "C++",
    ".html": "HTML",
    ".java": "Java",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".kt": "Kotlin",
    ".kts": "Kotlin",
    ".m": "Objective-C",
    ".mm": "Objective-C++",
    ".php": "PHP",
    ".py": "Python",
    ".rb": "Ruby",
    ".rs": "Rust",
    ".scala": "Scala",
    ".sh": "Shell",
    ".sql": "SQL",
    ".swift": "Swift",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
}

DOC_EXTENSIONS = {".md", ".mdx", ".rst", ".adoc", ".txt"}
MANIFEST_NAMES = {
    "AGENTS.md",
    "CLAUDE.md",
    "CODEOWNERS",
    "Dockerfile",
    "Gemfile",
    "Makefile",
    "Package.swift",
    "README.md",
    "build.gradle",
    "build.gradle.kts",
    "composer.json",
    "go.mod",
    "mix.exs",
    "package.json",
    "pom.xml",
    "pyproject.toml",
    "requirements.txt",
    "settings.gradle",
    "settings.gradle.kts",
}


@dataclass
class FileRecord:
    path: str
    kind: str
    language: str = ""
    size: int = 0
    title: str = ""
    doc_kind: str = ""
    snippet: str = ""
    symbols: List[Dict[str, str]] = field(default_factory=list)
    imports: List[str] = field(default_factory=list)
    is_test: bool = False
    file_ast: Optional[object] = None  # astextract.FileAST (runtime only; not in digest)
    content_hash: str = ""  # sha1 of source bytes; for incremental graph rebuilds

    def digest_view(self) -> Dict[str, object]:
        return {
            "path": self.path,
            "kind": self.kind,
            "language": self.language,
            "size": self.size,
            "title": self.title,
            "docKind": self.doc_kind,
            "symbols": self.symbols,
            "imports": self.imports,
            "isTest": self.is_test,
        }


@dataclass
class RepoScan:
    root: Path
    repo_name: str
    files: List[FileRecord] = field(default_factory=list)
    language_counts: Dict[str, int] = field(default_factory=dict)
    frameworks: List[str] = field(default_factory=list)
    top_dirs: Dict[str, int] = field(default_factory=dict)
    digest: str = ""

    @property
    def docs(self) -> List[FileRecord]:
        return [f for f in self.files if f.kind == "doc"]

    @property
    def sources(self) -> List[FileRecord]:
        return [f for f in self.files if f.kind == "source"]

    @property
    def manifests(self) -> List[FileRecord]:
        return [f for f in self.files if f.kind == "manifest"]


def scan_repo(repo_root: Path, config: SkyhookConfig) -> RepoScan:
    root = repo_root.resolve()
    paths = _discover_paths(root, config)
    records: List[FileRecord] = []
    language_counts: Dict[str, int] = {}
    top_dirs: Dict[str, int] = {}

    for rel in paths[: config.scan.max_files]:
        abs_path = root / rel
        if not abs_path.is_file():
            continue
        try:
            size = abs_path.stat().st_size
        except OSError:
            continue
        kind = classify_path(rel)
        ext = Path(rel).suffix.lower()
        language = SOURCE_EXTENSIONS.get(ext, "")
        record = FileRecord(path=rel, kind=kind, language=language, size=size)
        record.is_test = is_test_path(rel)
        if kind == "source" and language:
            language_counts[language] = language_counts.get(language, 0) + 1
            _enrich_source(abs_path, record)
        if kind == "doc":
            _enrich_doc(abs_path, record, config.scan.max_doc_bytes)
        if "/" in rel:
            top = rel.split("/", 1)[0]
        else:
            top = "."
        top_dirs[top] = top_dirs.get(top, 0) + 1
        records.append(record)

    frameworks = detect_frameworks({r.path for r in records})
    scan = RepoScan(
        root=root,
        repo_name=root.name,
        files=records,
        language_counts=dict(sorted(language_counts.items(), key=lambda kv: (-kv[1], kv[0]))),
        frameworks=frameworks,
        top_dirs=dict(sorted(top_dirs.items(), key=lambda kv: (-kv[1], kv[0]))),
    )
    scan.digest = digest_records(r.digest_view() for r in records)
    return scan


def classify_path(path: str) -> str:
    name = Path(path).name
    ext = Path(path).suffix.lower()
    if ext in DOC_EXTENSIONS:
        return "doc"
    if name in MANIFEST_NAMES or path.endswith(".xcodeproj/project.pbxproj"):
        return "manifest"
    if ext in SOURCE_EXTENSIONS:
        return "source"
    if ext in {".json", ".yaml", ".yml", ".toml", ".xml"}:
        return "config"
    return "other"


def classify_doc(path: str, text: str) -> str:
    lowered = path.lower()
    body = text[:2000].lower()
    if Path(path).name.lower() == "code_map.md":
        return "architecture"
    if Path(path).name.lower().startswith("readme"):
        return "readme"
    if Path(path).name.lower() in {"claude.md", "agents.md"}:
        return "readme"
    if "adr" in lowered or "architecture decision" in body:
        return "adr"
    if "c4" in lowered or "context diagram" in body or "container diagram" in body:
        return "c4"
    if "architecture" in lowered or "architecture" in body:
        return "architecture"
    if "design" in lowered or "design" in body:
        return "design"
    if (
        "runbook" in lowered
        or "setup" in lowered
        or "pre_submit" in lowered
        or "pre-submit" in lowered
        or "checklist" in lowered
        or "operational" in body
        or "build commands" in body
    ):
        return "runbook"
    if "api" in lowered:
        return "api"
    if "test" in lowered or "testing" in body:
        return "test"
    return "unknown"


def is_test_path(path: str) -> bool:
    lowered = path.lower()
    name = Path(lowered).name
    test_parts = {
        "__tests__",
        "androidtest",
        "commontest",
        "integrationtest",
        "ios_test",
        "iostest",
        "jvmtest",
        "spec",
        "test",
        "tests",
    }
    if any(part in test_parts for part in lowered.replace("-", "_").split("/")):
        return True
    return (
        name.startswith("test_")
        or name.endswith("_test.go")
        or name.endswith("_test.py")
        or name.endswith(".spec.js")
        or name.endswith(".spec.jsx")
        or name.endswith(".spec.ts")
        or name.endswith(".spec.tsx")
        or name.endswith(".test.js")
        or name.endswith(".test.jsx")
        or name.endswith(".test.ts")
        or name.endswith(".test.tsx")
        or name.endswith("test.kt")
        or name.endswith("tests.kt")
        or name.endswith("test.java")
        or name.endswith("tests.java")
        or name.endswith("test.swift")
        or name.endswith("tests.swift")
    )


def detect_frameworks(paths: Iterable[str]) -> List[str]:
    path_set = set(paths)
    frameworks: List[str] = []
    if "package.json" in path_set:
        frameworks.append("Node.js")
    if any(p.startswith("src/app/") for p in path_set) or any("next.config" in p for p in path_set):
        frameworks.append("Next.js")
    if "pyproject.toml" in path_set or "requirements.txt" in path_set:
        frameworks.append("Python")
    if "go.mod" in path_set:
        frameworks.append("Go")
    if "mix.exs" in path_set:
        frameworks.append("Elixir")
    if "Package.swift" in path_set or any(p.endswith(".xcodeproj/project.pbxproj") for p in path_set):
        frameworks.append("Swift/Xcode")
    if "build.gradle" in path_set or "build.gradle.kts" in path_set or "settings.gradle" in path_set:
        frameworks.append("Gradle")
    if any(p.endswith(".kt") for p in path_set):
        frameworks.append("Kotlin")
    if any(p.endswith(".swift") for p in path_set):
        frameworks.append("Swift")
    if "pom.xml" in path_set:
        frameworks.append("Maven")
    if any("spring" in p.lower() for p in path_set) or any(p.endswith("Application.kt") for p in path_set):
        frameworks.append("Spring")
    return sorted(set(frameworks))


def _discover_paths(root: Path, config: SkyhookConfig) -> List[str]:
    git_paths = _git_paths(root)
    if git_paths is not None:
        paths = git_paths
    else:
        paths = _walk_paths(root, config.scan.exclude)
    filtered = [
        p
        for p in paths
        if _is_included(p, config.scan.include) and not _is_excluded(p, config.scan.exclude)
    ]
    return sorted(set(filtered))


def _git_paths(root: Path) -> Optional[List[str]]:
    try:
        result = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            cwd=str(root),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _walk_paths(root: Path, excludes: Sequence[str]) -> List[str]:
    paths: List[str] = []
    for current, dirs, files in os.walk(root):
        rel_dir = Path(current).relative_to(root).as_posix()
        dirs[:] = [d for d in dirs if not _is_excluded(d if rel_dir == "." else f"{rel_dir}/{d}", excludes)]
        for file_name in files:
            rel = file_name if rel_dir == "." else f"{rel_dir}/{file_name}"
            paths.append(rel)
    return paths


def _is_excluded(path: str, excludes: Sequence[str]) -> bool:
    parts = path.split("/")
    for excluded in excludes:
        excluded = excluded.strip("/")
        if not excluded:
            continue
        if excluded in parts or path == excluded or path.startswith(excluded + "/"):
            return True
    return False


def _is_included(path: str, includes: Sequence[str]) -> bool:
    if not includes or "." in includes:
        return True
    for included in includes:
        included = included.strip("/")
        if not included:
            continue
        if path == included or path.startswith(included + "/") or fnmatch(path, included):
            return True
    return False


def _enrich_doc(abs_path: Path, record: FileRecord, max_bytes: int) -> None:
    try:
        raw = abs_path.read_bytes()[:max_bytes]
        text = raw.decode("utf-8", errors="replace")
    except OSError:
        text = ""
    record.title = _extract_title(text) or Path(record.path).name
    record.doc_kind = classify_doc(record.path, text)
    record.snippet = _snippet(text)


# Canonical extension per Skyhook language string — lets the AST extractor pick
# the right grammar (and TS-vs-TSX variant) when only a language is known.
_CANONICAL_EXT = {
    "Python": ".py",
    "Swift": ".swift",
    "Kotlin": ".kt",
    "Java": ".java",
    "JavaScript": ".js",
    "TypeScript": ".ts",
    "Go": ".go",
    "Elixir": ".ex",
}


def _enrich_source(abs_path: Path, record: FileRecord) -> None:
    try:
        raw = abs_path.read_bytes()[:50000]
    except OSError:
        raw = b""
    record.content_hash = hashlib.sha1(raw).hexdigest()
    file_ast = astextract.extract_file(record.path, record.language, raw)
    record.file_ast = file_ast

    symbols: List[Dict[str, object]] = []
    for d in file_ast.defs[:50]:
        symbols.append(
            {
                "name": d.name,
                "kind": _symbol_kind(record.path, d.name, d.kind_fallback),
                "structuralKind": d.structural_kind,
                "line": d.start_line,
                "endLine": d.end_line,
                "scope": d.scope or "",
                "signature": d.signature,
            }
        )
    record.symbols = symbols  # type: ignore[assignment]

    seen: set = set()
    imports: List[str] = []
    for ref in file_ast.imports:
        if ref.target and ref.target not in seen:
            seen.add(ref.target)
            imports.append(ref.target)
            if len(imports) >= 80:
                break
    record.imports = imports


def extract_symbols(path: str, language: str, text: str) -> List[Dict[str, str]]:
    """Backward-compatible shim: AST-derived ``[{name, kind}]`` for a source string."""
    file_ast = astextract.extract_file(path, language, text.encode("utf-8", "replace"))
    out: List[Dict[str, str]] = []
    for d in file_ast.defs[:50]:
        out.append({"name": d.name, "kind": _symbol_kind(path, d.name, d.kind_fallback)})
    return out


def extract_imports(language: str, text: str) -> List[str]:
    """Backward-compatible shim: AST-derived import targets for a source string."""
    synthetic = "_source" + _CANONICAL_EXT.get(language, "")
    file_ast = astextract.extract_file(synthetic, language, text.encode("utf-8", "replace"))
    seen: set = set()
    out: List[str] = []
    for ref in file_ast.imports:
        if ref.target and ref.target not in seen:
            seen.add(ref.target)
            out.append(ref.target)
            if len(out) >= 80:
                break
    return out


def _symbol_kind(path: str, name: str, fallback: str) -> str:
    lowered = f"{path}/{name}".lower()
    if is_test_path(path) or "test" in lowered or "spec" in lowered:
        return "test"
    if "route" in lowered or "controller" in lowered or "endpoint" in lowered:
        return "route"
    if "service" in lowered or "client" in lowered:
        return "service"
    if "repository" in lowered or "repo" in lowered or "dao" in lowered:
        return "repository"
    if "viewmodel" in lowered or "model" in lowered or "entity" in lowered or "dto" in lowered:
        return "model"
    if "component" in lowered or name[:1].isupper() and path.endswith((".tsx", ".jsx")):
        return "component"
    return fallback


def _extract_title(text: str) -> str:
    for line in text.splitlines()[:80]:
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()
        if stripped:
            return stripped[:100]
    return ""


def _snippet(text: str) -> str:
    lines = []
    for line in text.splitlines():
        stripped = re.sub(r"\s+", " ", line.strip())
        if not stripped or stripped.startswith("#"):
            continue
        lines.append(stripped)
        if len(" ".join(lines)) > 500:
            break
    return " ".join(lines)[:700]
