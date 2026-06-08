"""Tree-sitter grammar loading.

Maps Skyhook's language strings (the values in ``scanner.SOURCE_EXTENSIONS``,
e.g. ``"Python"``, ``"Swift"``) to their tree-sitter grammar wheels, and builds
cached ``Language``/``Parser`` objects plus a small query-execution helper.

Design notes:
- A missing or incompatible grammar **degrades to skip-with-warning** (returns
  ``None``); it must never crash a scan.
- Targets the tree-sitter >= 0.25 Python API (``Language(capsule)``,
  ``Parser(language)``, ``Query`` + ``QueryCursor``). The minor API differences
  of older releases are isolated here so the rest of the package never sees them.
- TypeScript ships two grammars (``language_typescript`` / ``language_tsx``);
  the right one is selected from the file extension.
"""

from __future__ import annotations

import importlib
import warnings
from typing import Callable, Dict, List, Optional, Tuple

try:  # tree-sitter is a core dependency (>= 0.25); guard so import errors are friendly.
    import tree_sitter as _ts

    _Language = _ts.Language
    _Parser = _ts.Parser
    _Query = _ts.Query
    _QueryCursor = getattr(_ts, "QueryCursor", None)
    _TS_IMPORT_ERROR: Optional[BaseException] = None
except Exception as exc:  # pragma: no cover - exercised only without the dep
    _ts = None  # type: ignore
    _Language = _Parser = _Query = _QueryCursor = None  # type: ignore
    _TS_IMPORT_ERROR = exc


# Skyhook language string -> (pip module, capsule attribute).
# The capsule attribute is a function returning the grammar's C capsule.
# A tuple of attributes (TypeScript) means: pick by extension via EXT_VARIANT.
GRAMMARS: Dict[str, object] = {
    "Python": ("tree_sitter_python", "language"),
    "Swift": ("tree_sitter_swift", "language"),
    "Kotlin": ("tree_sitter_kotlin", "language"),
    "Java": ("tree_sitter_java", "language"),
    "JavaScript": ("tree_sitter_javascript", "language"),
    "TypeScript": ("tree_sitter_typescript", ("language_typescript", "language_tsx")),
    "Go": ("tree_sitter_go", "language"),
    "Elixir": ("tree_sitter_elixir", "language"),
}

# Extension -> which TypeScript variant attribute to use.
_EXT_VARIANT: Dict[str, str] = {".tsx": "language_tsx", ".jsx": "language_typescript"}

_LANGUAGE_CACHE: Dict[str, object] = {}
_WARNED: set = set()


def supported_languages() -> List[str]:
    """Language strings Skyhook knows a grammar wheel for (not necessarily installed)."""
    return sorted(GRAMMARS)


def tree_sitter_available() -> bool:
    return _ts is not None and _QueryCursor is not None


def _warn_once(key: str, message: str) -> None:
    if key not in _WARNED:
        _WARNED.add(key)
        warnings.warn(message, RuntimeWarning, stacklevel=2)


def _capsule_attr(language: str, ext: str) -> Optional[Tuple[str, str]]:
    spec = GRAMMARS.get(language)
    if spec is None:
        return None
    module_name, attr = spec  # type: ignore[misc]
    if isinstance(attr, tuple):
        # TypeScript: pick variant by extension, default to the first (typescript).
        chosen = _EXT_VARIANT.get(ext, attr[0])
        if chosen not in attr:
            chosen = attr[0]
        return module_name, chosen
    return module_name, attr


def get_language(language: str, ext: str = "") -> Optional[object]:
    """Return a cached tree-sitter ``Language`` for a Skyhook language string.

    Returns ``None`` (warning once) when tree-sitter is missing, the grammar
    wheel is not installed, or the grammar's ABI is incompatible with the
    installed tree-sitter core.
    """
    if not tree_sitter_available():
        _warn_once(
            "core",
            "tree-sitter is not available (%r); code graph disabled. "
            "Install with `pip install tree-sitter>=0.25`." % (_TS_IMPORT_ERROR,),
        )
        return None

    resolved = _capsule_attr(language, ext)
    if resolved is None:
        return None
    module_name, attr = resolved
    cache_key = f"{module_name}:{attr}"
    if cache_key in _LANGUAGE_CACHE:
        return _LANGUAGE_CACHE[cache_key]

    try:
        module = importlib.import_module(module_name)
        capsule = getattr(module, attr)()
        lang = _Language(capsule)
    except ImportError:
        _warn_once(
            module_name,
            f"grammar for {language} not installed ({module_name}); "
            f"skipping {language} files. Install with `pip install {module_name.replace('_', '-')}`.",
        )
        _LANGUAGE_CACHE[cache_key] = None
        return None
    except Exception as exc:  # ABI mismatch, capsule errors, etc.
        _warn_once(
            cache_key,
            f"could not load grammar for {language} ({module_name}): {exc}; "
            f"skipping {language} files.",
        )
        _LANGUAGE_CACHE[cache_key] = None
        return None

    _LANGUAGE_CACHE[cache_key] = lang
    return lang


def get_parser(language: str, ext: str = "") -> Optional[object]:
    """Return a fresh ``Parser`` bound to the language, or ``None`` if unavailable.

    Parsers are cheap and not thread-safe to share, so we build one per call
    rather than caching (the Language is cached).
    """
    lang = get_language(language, ext)
    if lang is None:
        return None
    try:
        return _Parser(lang)
    except TypeError:
        # Older API: Parser() then assign language.
        parser = _Parser()
        try:
            parser.language = lang  # >= 0.22
        except Exception:
            parser.set_language(lang)  # <= 0.21
        return parser


def compile_query(language: str, source: str, ext: str = "") -> Optional[object]:
    lang = get_language(language, ext)
    if lang is None:
        return None
    try:
        return _Query(lang, source)
    except Exception as exc:
        _warn_once(
            f"query:{language}:{hash(source)}",
            f"invalid tree-sitter query for {language}: {exc}",
        )
        return None


def run_matches(query: object, node: object):
    """Execute a compiled query over a node, yielding ``(pattern_index, captures)``.

    ``captures`` is a ``{capture_name: [nodes]}`` dict per match (the 0.25
    ``QueryCursor.matches`` shape). Returns ``[]`` when query execution is
    unavailable.
    """
    if query is None or _QueryCursor is None:
        return []
    cursor = _QueryCursor(query)
    return cursor.matches(node)
