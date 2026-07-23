"""``skyhook wire``: register Skyhook with a coding agent and deliver the query-first
protocol into that agent's always-on context.

Connecting the MCP tools is not enough on its own; the agent also has to be told to reach
for them, in context it actually reads. ``wire`` writes both, per detected agent, with the
user's permission. All writes are direct file edits (no dependency on the agents' own CLIs),
idempotent (marked blocks / JSON merge), and never touch content outside their markers.
"""
from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

MARK_START = "skyhook:start"
MARK_END = "skyhook:end"

# Canonical agent-facing protocol. Kept in sync with route._GRAPH_FIRST and the INDEX
# "How To Work Here" block; this is the always-on-context version.
PROTOCOL = (
    "This repository is indexed by Skyhook, a real tree-sitter AST symbol + call graph exposed "
    "as MCP tools. For any coding task, call the `route` tool FIRST (or run "
    "`skyhook route --task \"...\"`) to get where to start, the likely edit targets, the tests "
    "that matter, and the blast radius, instead of grepping to find them. Then use `callers_of` "
    "/ `callees_of` / `blast_radius` to trace the code paths. Trust precise edges "
    "(same_file/qualified/imported/same_package); grep only for edges marked global/approximate "
    "or that the graph does not return. `.skyhook/INDEX.md` is the repo map to start from."
)

AGENTS = ("claude", "codex", "cursor")


def _slug(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "-", name).strip("-").lower() or "repo"


def _server_obj(repo_root: Path, always_load: bool = True) -> dict:
    obj = {"command": "skyhook", "args": ["mcp", "--repo", str(repo_root)]}
    if always_load:
        obj["alwaysLoad"] = True
    return obj


# --- idempotent write helpers -------------------------------------------------------------

def _markers(style: str, tag: str = ""):
    suffix = f":{tag}" if tag else ""
    if style == "toml":
        return f"# {MARK_START}{suffix}", f"# {MARK_END}{suffix}"
    return f"<!-- {MARK_START}{suffix} -->", f"<!-- {MARK_END}{suffix} -->"


def upsert_marked_block(path: Path, body: str, style: str = "md", tag: str = "") -> str:
    """Insert or replace the skyhook-marked block in ``path``, leaving all other content
    untouched. Returns ``created`` | ``updated`` | ``unchanged``."""
    start, end = _markers(style, tag)
    block = f"{start}\n{body.strip()}\n{end}\n"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    pat = re.compile(re.escape(start) + r".*?" + re.escape(end) + r"\n?", re.S)
    if pat.search(existing):
        new = pat.sub(block, existing, count=1)
    else:
        prefix = existing
        if prefix and not prefix.endswith("\n"):
            prefix += "\n"
        if prefix and not prefix.endswith("\n\n"):
            prefix += "\n"
        new = prefix + block
    if new == existing:
        return "unchanged"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new, encoding="utf-8")
    return "created" if not existing else "updated"


def merge_json_mcp(path: Path, server_obj: dict, key: str = "skyhook") -> str:
    """Add/replace ``mcpServers.<key>`` in a JSON config, preserving any other servers.
    Returns ``created`` | ``updated`` | ``unchanged``."""
    existing_text = path.read_text(encoding="utf-8") if path.exists() else ""
    data: dict = {}
    if existing_text.strip():
        try:
            data = json.loads(existing_text)
        except Exception:
            data = {}
    servers = data.setdefault("mcpServers", {})
    if servers.get(key) == server_obj:
        return "unchanged"
    servers[key] = server_obj
    new = json.dumps(data, indent=2) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new, encoding="utf-8")
    return "created" if not existing_text else "updated"


def _write_own_file(path: Path, content: str) -> str:
    """Write a file Skyhook fully owns (Cursor rule). Idempotent."""
    existed = path.exists()
    if existed and path.read_text(encoding="utf-8") == content:
        return "unchanged"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return "updated" if existed else "created"


def _cursor_rule_body() -> str:
    return (
        "---\n"
        "description: Skyhook — query the code graph before grepping\n"
        "alwaysApply: true\n"
        "---\n\n" + PROTOCOL + "\n"
    )


def _codex_block(repo_root: Path, slug: str) -> str:
    args = json.dumps(["mcp", "--repo", str(repo_root)])
    return f'[mcp_servers.skyhook-{slug}]\ncommand = "skyhook"\nargs = {args}\n'


# --- detection & plan ---------------------------------------------------------------------

@dataclass
class Action:
    agent: str
    desc: str
    target: str
    run: Callable[[], str]
    global_: bool = False


def detect(repo_root: Path, home: Path) -> List[str]:
    present: List[str] = []
    if shutil.which("claude") or (repo_root / ".claude").exists() or (home / ".claude.json").exists():
        present.append("claude")
    if (home / ".codex").exists():
        present.append("codex")
    if (repo_root / ".cursor").exists() or (home / ".cursor").exists():
        present.append("cursor")
    return present


def build_plan(
    repo_root: Path,
    agents: List[str],
    home: Optional[Path] = None,
    codex_config: Optional[Path] = None,
) -> List[Action]:
    home = home or Path.home()
    codex_config = codex_config or (home / ".codex" / "config.toml")
    repo_root = Path(repo_root).resolve()
    slug = _slug(repo_root.name)
    srv = _server_obj(repo_root, always_load=True)
    srv_plain = _server_obj(repo_root, always_load=False)
    actions: List[Action] = []

    if "claude" in agents:
        mcp = repo_root / ".mcp.json"
        actions.append(Action("claude", "register MCP server (alwaysLoad)", str(mcp),
                              lambda p=mcp: merge_json_mcp(p, srv)))
        cmd = repo_root / "CLAUDE.md"
        actions.append(Action("claude", "query-first protocol", str(cmd),
                              lambda p=cmd: upsert_marked_block(p, PROTOCOL, "md")))

    if "cursor" in agents:
        mcp = repo_root / ".cursor" / "mcp.json"
        actions.append(Action("cursor", "register MCP server", str(mcp),
                              lambda p=mcp: merge_json_mcp(p, srv_plain)))
        rule = repo_root / ".cursor" / "rules" / "skyhook.mdc"
        actions.append(Action("cursor", "query-first rule", str(rule),
                              lambda p=rule: _write_own_file(p, _cursor_rule_body())))

    if "codex" in agents:
        actions.append(Action("codex", f"register mcp_servers.skyhook-{slug}", str(codex_config),
                              lambda: upsert_marked_block(codex_config, _codex_block(repo_root, slug), "toml", tag=slug),
                              global_=True))
        agents_md = repo_root / "AGENTS.md"
        actions.append(Action("codex", "query-first protocol", str(agents_md),
                              lambda p=agents_md: upsert_marked_block(p, PROTOCOL, "md")))

    return actions
