"""Skyhook MCP server.

Exposes the AST code graph (``.skyhook/graph.db``) as read-only MCP tools so any
MCP client (Claude Code, Cursor, Copilot, a pipeline) can query structure
instead of grep-exploring. The ``route`` tool wraps ``skyhook route`` (task ->
orientation pack); the rest mirror ``skyhook graph query`` 1:1.

Protocol for agents: for a coding task, call ``route`` FIRST to get where to start,
likely edit targets, tests, and blast radius, instead of grepping to find them.
Then reach for the graph tools before further grep-exploring. Trust precise edges
(same_file/qualified/imported/same_package); a ``global`` or candidate
``resolution`` is a heuristic guess, and an empty result means verify by hand,
not that nothing is there.

Run via ``skyhook mcp --repo <path>``. Requires the optional ``mcp`` extra
(``pip install 'skyhook[mcp]'``, Python >= 3.10); without it, query the graph
with ``skyhook graph query`` instead.

Register with any client:
    {"mcpServers": {"skyhook": {"command": "skyhook", "args": ["mcp", "--repo", "/path"]}}}
"""

from __future__ import annotations

from pathlib import Path
from typing import List


class McpUnavailable(RuntimeError):
    pass


def build_server(store, map_data=None):
    """Register the read-only graph tools on a FastMCP server. Returns the server.

    ``map_data`` is the loaded ``.skyhook/map.json``; when present it enables the
    ``route`` tool (task -> orientation pack). When absent, ``route`` is still
    registered but returns a clear "run skyhook init" error, so the tool stays
    discoverable.
    """
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise McpUnavailable(
            "the MCP server needs the 'mcp' extra: pip install 'skyhook[mcp]' "
            "(requires Python >= 3.10). Until then, use `skyhook graph query`."
        ) from exc

    server = FastMCP("skyhook")

    @server.tool()
    def route(task: str, profile: str = "implementation") -> dict:
        """Task -> orientation pack for a coding task. Call this FIRST, before
        grepping to find where to work: it returns where to start reading, the
        likely edit targets, relevant tests, call chains, and blast radius, scoped
        to the task. `profile` is one of product_planning, requirements_planning,
        technical_breakdown, implementation (default), code_review, bug_hunt."""
        if map_data is None:
            return {"error": "no Skyhook map found; run `skyhook init` first to "
                             "generate .skyhook/map.json, then retry route."}
        from .route import build_route
        return build_route(map_data, task, profile=profile, graph=store)

    @server.tool()
    def find_symbol(name: str) -> List[dict]:
        """Find symbol definitions by exact name (returns path + line + kind)."""
        return store.find_symbol(name)

    @server.tool()
    def search(query: str) -> List[dict]:
        """Fuzzy-search symbol names (substring match)."""
        return store.search(query)

    @server.tool()
    def symbols_in_file(path: str) -> List[dict]:
        """List the symbols defined in a file."""
        return store.symbols_in_file(path)

    @server.tool()
    def callers_of(name: str, strict: bool = False) -> List[dict]:
        """Symbols that call `name`. Prefer this over grepping for the name. Each
        edge carries a `resolution` grade (same_file/qualified/imported/same_package
        are precise; global and candidate edges are heuristic guesses). `strict`
        drops candidate edges. An empty list means verify by hand, not that there
        are no callers."""
        return store.callers_of(name, strict=strict)

    @server.tool()
    def callees_of(name: str) -> List[dict]:
        """Symbols/functions that `name` calls. Prefer this over reading the body to
        trace calls; edges carry the same `resolution` grades as callers_of."""
        return store.callees_of(name)

    @server.tool()
    def blast_radius(target: str, depth: int = 3) -> dict:
        """Transitive reverse-call impact of a file or symbol. Ask this before a
        refactor instead of grepping for usages. `approximate` is true only when
        heuristic (name-matched) edges contributed; see `resolutionSummary` for the
        precise/heuristic edge split. Coverage is not total, so verify wide edits."""
        return store.blast_radius(target, depth=depth)

    @server.tool()
    def file_exists(path: str) -> dict:
        """Whether a file path exists in the indexed graph."""
        return {"path": path, "exists": store.file_exists(path)}

    @server.tool()
    def graph_stats() -> dict:
        """Coverage stats for the graph (files, symbols, calls, resolved %)."""
        return store.stats()

    return server


def serve(db_path: Path, out_dir: Path | None = None) -> None:
    """Open the graph read-only and run a stdio MCP server. Blocks until closed.

    ``out_dir`` (the ``.skyhook`` directory) is used to load ``map.json`` so the
    ``route`` tool works; if it is missing, ``route`` reports a clear error.
    """
    from .graphstore import GraphStore

    store = GraphStore(str(db_path), read_only=True)
    map_data = None
    if out_dir is not None:
        try:
            from .artifacts import load_existing_map

            map_data = load_existing_map(out_dir)
        except Exception:
            map_data = None
    build_server(store, map_data=map_data).run()
