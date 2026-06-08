"""AST extraction via tree-sitter.

Parses a source file once and runs three queries (``tags``, ``calls``,
``imports``) to produce a :class:`FileAST`: symbol definitions (with kind,
line range, enclosing scope, signature), call sites (callee name + line +
enclosing symbol), and imports. This replaces Skyhook's former regex symbol
extraction.

Queries live in ``skyhook/queries/<lang_dir>/{tags,calls,imports}.scm`` and are
loaded from package data. A language with no query directory or no installed
grammar yields an empty :class:`FileAST` (graceful skip).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib import resources
from typing import Dict, List, Optional

from . import grammars

# Skyhook language string -> query subdirectory name.
_LANG_DIR: Dict[str, str] = {
    "Python": "python",
    "Swift": "swift",
    "Kotlin": "kotlin",
    "Java": "java",
    "JavaScript": "javascript",
    "TypeScript": "typescript",
    "Go": "go",
    "Elixir": "elixir",
}

# structural kind (from the @definition.<x> capture) -> fallback kind handed to
# scanner._symbol_kind, preserving today's human vocabulary ("model"/"other"/...).
_KIND_FALLBACK: Dict[str, str] = {
    "class": "model",
    "struct": "model",
    "protocol": "model",
    "interface": "model",
    "enum": "model",
    "object": "model",
    "record": "model",
    "module": "module",
    "function": "other",
    "method": "other",
    "constant": "other",
}

_DEFS_CAP = 50
_IMPORTS_CAP = 80
_CALLS_CAP = 2000


@dataclass
class SymbolDef:
    name: str
    structural_kind: str  # function | class | method | ...
    kind_fallback: str  # fed to scanner._symbol_kind
    start_line: int  # 1-indexed
    end_line: int
    scope: Optional[str] = None  # enclosing symbol name, or None at top level
    signature: str = ""
    start_byte: int = 0
    end_byte: int = 0


@dataclass
class CallSite:
    callee_name: str
    line: int
    enclosing: Optional[str] = None  # name of the symbol containing the call


@dataclass
class ImportRef:
    target: str
    line: int


@dataclass
class FileAST:
    language: str = ""
    defs: List[SymbolDef] = field(default_factory=list)
    calls: List[CallSite] = field(default_factory=list)
    imports: List[ImportRef] = field(default_factory=list)
    had_error: bool = False
    parsed: bool = False  # True when tree-sitter actually ran

    def empty(self) -> bool:
        return not (self.defs or self.calls or self.imports)


_QUERY_CACHE: Dict[str, Optional[str]] = {}


def _load_query(lang_dir: str, name: str) -> Optional[str]:
    key = f"{lang_dir}/{name}"
    if key in _QUERY_CACHE:
        return _QUERY_CACHE[key]
    try:
        text = (
            resources.files("skyhook").joinpath("queries", lang_dir, f"{name}.scm").read_text()
        )
    except (FileNotFoundError, ModuleNotFoundError, OSError):
        text = None
    _QUERY_CACHE[key] = text
    return text


def _structural_kind(capture_name: str) -> str:
    # "definition.function" -> "function"; "definition.class" -> "class".
    return capture_name.split(".", 1)[1] if "." in capture_name else capture_name


def _signature(node, source: bytes) -> str:
    text = source[node.start_byte : node.end_byte].decode("utf-8", "replace")
    first = text.splitlines()[0].strip() if text else ""
    return first[:160]


def _enclosing(byte_pos: int, defs: List[SymbolDef], exclude: Optional[SymbolDef] = None) -> Optional[SymbolDef]:
    """Smallest def whose byte range strictly contains ``byte_pos`` (not ``exclude``)."""
    best: Optional[SymbolDef] = None
    for d in defs:
        if d is exclude:
            continue
        if d.start_byte <= byte_pos < d.end_byte:
            if best is None or (d.end_byte - d.start_byte) < (best.end_byte - best.start_byte):
                best = d
    return best


def extract_file(path: str, language: str, source: bytes) -> FileAST:
    """Parse ``source`` and return a :class:`FileAST`. Never raises."""
    result = FileAST(language=language)
    lang_dir = _LANG_DIR.get(language)
    if lang_dir is None:
        return result

    ext = "." + path.rsplit(".", 1)[-1] if "." in path else ""
    parser = grammars.get_parser(language, ext)
    if parser is None:
        return result

    try:
        tree = parser.parse(source)
    except Exception:
        return result
    result.parsed = True
    root = tree.root_node
    result.had_error = bool(getattr(root, "has_error", False))

    # --- definitions (tags) ---
    tags_src = _load_query(lang_dir, "tags")
    if tags_src:
        query = grammars.compile_query(language, tags_src, ext)
        for _pat, caps in grammars.run_matches(query, root):
            name_nodes = caps.get("name") or []
            if not name_nodes:
                continue
            def_node = None
            structural = "function"
            for cap_name, nodes in caps.items():
                if cap_name.startswith("definition") and nodes:
                    def_node = nodes[0]
                    structural = _structural_kind(cap_name)
                    break
            name_node = name_nodes[0]
            anchor = def_node or name_node
            result.defs.append(
                SymbolDef(
                    name=name_node.text.decode("utf-8", "replace"),
                    structural_kind=structural,
                    kind_fallback=_KIND_FALLBACK.get(structural, "other"),
                    start_line=anchor.start_point[0] + 1,
                    end_line=anchor.end_point[0] + 1,
                    signature=_signature(anchor, source),
                    start_byte=anchor.start_byte,
                    end_byte=anchor.end_byte,
                )
            )
            if len(result.defs) >= _DEFS_CAP:
                break

    # Second pass: scope + method-vs-function (needs the full def set).
    for d in result.defs:
        parent = _enclosing(d.start_byte, result.defs, exclude=d)
        if parent is not None:
            d.scope = parent.name
            if d.structural_kind == "function" and parent.structural_kind in (
                "class",
                "struct",
                "protocol",
                "interface",
                "enum",
                "object",
            ):
                d.structural_kind = "method"

    # --- imports ---
    imports_src = _load_query(lang_dir, "imports")
    if imports_src:
        query = grammars.compile_query(language, imports_src, ext)
        seen = set()
        for _pat, caps in grammars.run_matches(query, root):
            for node in caps.get("name") or []:
                target = node.text.decode("utf-8", "replace").strip()
                if target and target not in seen:
                    seen.add(target)
                    result.imports.append(ImportRef(target=target, line=node.start_point[0] + 1))
                    if len(result.imports) >= _IMPORTS_CAP:
                        break
            if len(result.imports) >= _IMPORTS_CAP:
                break

    # --- calls ---
    calls_src = _load_query(lang_dir, "calls")
    if calls_src:
        query = grammars.compile_query(language, calls_src, ext)
        for _pat, caps in grammars.run_matches(query, root):
            name_nodes = caps.get("name") or []
            if not name_nodes:
                continue
            node = name_nodes[0]
            enc = _enclosing(node.start_byte, result.defs)
            result.calls.append(
                CallSite(
                    callee_name=node.text.decode("utf-8", "replace"),
                    line=node.start_point[0] + 1,
                    enclosing=enc.name if enc else None,
                )
            )
            if len(result.calls) >= _CALLS_CAP:
                break

    return result
