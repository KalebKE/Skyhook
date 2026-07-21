"""Skyhook MCP server.

Exposes the AST code graph (``.skyhook/graph.db``) as read-only MCP tools so any
MCP client (Claude Code, Cursor, Copilot, a pipeline) can query structure
instead of grep-exploring. Tools mirror ``skyhook graph query`` 1:1.

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


def build_server(store):
    """Register the read-only graph tools on a FastMCP server. Returns the server."""
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise McpUnavailable(
            "the MCP server needs the 'mcp' extra: pip install 'skyhook[mcp]' "
            "(requires Python >= 3.10). Until then, use `skyhook graph query`."
        ) from exc

    server = FastMCP("skyhook")

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
        """Symbols that call `name`. Each edge carries a `resolution` grade
        (same_file/qualified/imported/same_package are precise; global and
        candidate edges are heuristic). `strict` drops candidate edges."""
        return store.callers_of(name, strict=strict)

    @server.tool()
    def callees_of(name: str) -> List[dict]:
        """Symbols/functions that `name` calls."""
        return store.callees_of(name)

    @server.tool()
    def blast_radius(target: str, depth: int = 3) -> dict:
        """Transitive reverse-call impact of a file or symbol. `approximate` is
        true only when heuristic (name-matched) edges contributed; see
        `resolutionSummary` for the precise/heuristic edge split."""
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


def serve(db_path: Path) -> None:
    """Open the graph read-only and run a stdio MCP server. Blocks until closed."""
    from .graphstore import GraphStore

    store = GraphStore(str(db_path), read_only=True)
    build_server(store).run()
