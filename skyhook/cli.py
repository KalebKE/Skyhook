from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from .analysis import enrich_map
from .artifacts import check_outputs, load_existing_map, output_dir, outputs_would_change, write_outputs, write_route
from .config import load_config
from .model import ModelError, choose_orienter
from .render import render_route_markdown
from .route import DEFAULT_PROFILE, build_route, profile_names
from .scanner import scan_repo
from .schema import canonical_json, validate_map


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            return cmd_init(args)
        if args.command == "check":
            return cmd_check(args)
        if args.command == "route":
            return cmd_route(args)
        if args.command == "graph":
            return cmd_graph(args)
        if args.command == "mcp":
            return cmd_mcp(args)
        if args.command == "bench":
            return cmd_bench(args)
    except KeyboardInterrupt:
        print("skyhook: interrupted", file=sys.stderr)
        return 130
    except (ModelError, ValueError) as exc:
        print(f"skyhook: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"skyhook: {exc}", file=sys.stderr)
        return 2
    parser.print_help(sys.stderr)
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="skyhook", description="Generate lightweight code and documentation maps for agents.")
    sub = parser.add_subparsers(dest="command")

    init = sub.add_parser("init", help="scan a repo and generate Skyhook map artifacts")
    add_common_args(init)
    init.add_argument("--dry-run", action="store_true", help="scan and synthesize without writing files")

    check = sub.add_parser("check", help="validate Skyhook map artifacts for CI")
    add_common_args(check, provider=False)

    route = sub.add_parser("route", help="create a task-specific route pack from an existing Skyhook map")
    add_common_args(route, provider=False)
    route.add_argument("--task", default=None, help="task or issue text to route")
    route.add_argument("--task-file", default=None, help="file containing task or issue text")
    route.add_argument("--profile", default=DEFAULT_PROFILE, help="route profile: " + ", ".join(profile_names()))
    route.add_argument("--format", choices=["markdown", "json"], default="markdown", help="output format")
    route.add_argument("--save", action="store_true", help="write route artifacts under .skyhook/routes")

    graph = sub.add_parser("graph", help="build or query the AST symbol+call graph")
    gsub = graph.add_subparsers(dest="graph_command")

    gbuild = gsub.add_parser("build", help="build .skyhook/graph.db from source")
    add_common_args(gbuild, provider=False)
    gbuild.add_argument("--full", action="store_true", help="rebuild from scratch (default: incremental)")

    gquery = gsub.add_parser("query", help="query the graph")
    add_common_args(gquery, provider=False)
    gquery.add_argument(
        "kind",
        choices=["defs", "callers", "callees", "blast-radius", "exists", "symbols-in-file", "search"],
    )
    gquery.add_argument("arg", help="symbol name, file path, or search text")
    gquery.add_argument("--json", action="store_true", help="emit JSON")
    gquery.add_argument("--depth", type=int, default=3, help="blast-radius depth")
    gquery.add_argument("--strict", action="store_true", help="resolved-only (no candidate matches)")

    gstats = gsub.add_parser("stats", help="graph coverage stats")
    add_common_args(gstats, provider=False)
    gstats.add_argument("--json", action="store_true", help="emit JSON")

    mcp = sub.add_parser("mcp", help="run the read-only MCP server over the graph")
    add_common_args(mcp, provider=False)

    bench = sub.add_parser("bench", help="estimate the token reduction of a graph route pack")
    add_common_args(bench, provider=False)
    bench.add_argument("--task", required=True, help="task text to route + measure")
    bench.add_argument("--profile", default="implementation")
    return parser


def add_common_args(parser: argparse.ArgumentParser, provider: bool = True) -> None:
    parser.add_argument("--repo", default=".", help="repository root, default: current directory")
    parser.add_argument("--config", default=None, help="path to .skyhook/config.yaml")
    parser.add_argument("--output-dir", default=None, help="override output directory")
    if provider:
        parser.add_argument("--provider", default=None, help="model provider: auto, openai, static")
        parser.add_argument("--model", default=None, help="model name override")
        parser.add_argument("--max-files", type=int, default=None, help="maximum files to scan")


def cmd_init(args: argparse.Namespace) -> int:
    repo_root, cfg, out_dir = load_runtime(args)
    data, scan = build_map(
        repo_root,
        cfg,
        provider=getattr(args, "provider", None),
        model=getattr(args, "model", None),
        max_files=getattr(args, "max_files", None),
        previous=None,
        error_prefix="generated map",
    )
    if getattr(args, "dry_run", False):
        print_report("init", scan, out_dir, wrote=False, would_change=outputs_would_change(out_dir, data))
        return 0
    write_outputs(out_dir, data)
    _build_graph_artifacts(scan, out_dir)
    print_report("init", scan, out_dir, wrote=True)
    return 0


def _build_graph_artifacts(scan, out_dir: Path) -> None:
    """Build .skyhook/graph.db (+ diffable graph.json) from an existing scan."""
    from .graphstore import build_graph

    build_graph(scan, out_dir / "graph.db", full=False)
    _ensure_graph_gitignore(out_dir)


def _ensure_graph_gitignore(out_dir: Path, ignore_all: bool = False) -> None:
    """Gitignore the regenerable binary db; the JSON export is what gets committed.

    ``ignore_all=True`` is for a transiently-created ``.skyhook/`` in a repo that
    has not adopted Skyhook: ignore the entire directory (including this
    ``.gitignore``) so the on-demand graph never shows up in the agent's git
    tree. Only written when no ``.gitignore`` already exists (an adopted repo
    keeps its committed one, which ignores just ``graph.db``).
    """
    gitignore = out_dir / ".gitignore"
    if ignore_all:
        if not gitignore.exists():
            gitignore.write_text("*\n")
        return
    lines = []
    if gitignore.exists():
        lines = gitignore.read_text().splitlines()
    if "graph.db" not in lines:
        lines.append("graph.db")
        gitignore.write_text("\n".join(lines) + "\n")


def _ensure_graph(repo_root: Path, cfg, out_dir: Path) -> Path:
    """Return a queryable graph.db, building it transiently if absent.

    Used by ``mcp``/``graph query``/``graph stats`` so they work in a fresh
    worktree without a prior ``skyhook init``. The build is transient: db only
    (no graph.json commit-artifact), and if we create ``.skyhook/`` ourselves we
    gitignore the whole thing so the agent's tree stays clean.
    """
    db_path = out_dir / "graph.db"
    if db_path.exists():
        return db_path
    fresh = not out_dir.exists()
    out_dir.mkdir(parents=True, exist_ok=True)
    from .graphstore import build_graph

    build_graph(scan_repo(repo_root, cfg), db_path, full=True, export_json=False)
    _ensure_graph_gitignore(out_dir, ignore_all=fresh)
    return db_path


def cmd_graph(args: argparse.Namespace) -> int:
    from .graphstore import GraphStore

    repo_root, cfg, out_dir = load_runtime(args)
    db_path = out_dir / "graph.db"
    sub = getattr(args, "graph_command", None)

    if sub == "build":
        scan = scan_repo(repo_root, cfg)
        _build_graph_artifacts(scan, out_dir)
        store = GraphStore(str(db_path), read_only=True)
        print(f"skyhook graph: built {db_path}")
        print(f"- {store.stats()}")
        store.close()
        return 0

    db_path = _ensure_graph(repo_root, cfg, out_dir)
    store = GraphStore(str(db_path), read_only=True)
    try:
        if sub == "stats":
            _emit_graph(store.stats(), args)
            return 0
        if sub == "query":
            _emit_graph(_run_graph_query(store, args), args)
            return 0
    finally:
        store.close()
    raise ValueError("skyhook graph requires a subcommand: build, query, or stats")


def cmd_bench(args: argparse.Namespace) -> int:
    """Compare a graph-enriched route pack against reading the relevant files."""
    repo_root, _cfg, out_dir = load_runtime(args)
    data = load_existing_map(out_dir)
    if data is None:
        raise ModelError("run `skyhook init` first")
    graph = _open_graph(out_dir)
    try:
        route = build_route(data, args.task, profile=args.profile, graph=graph)
    finally:
        if graph is not None:
            graph.close()
    pack_chars = len(canonical_json(route))

    paths = list(route.get("likelyEditTargets", []) or [])
    if route.get("blastRadius"):
        paths += route["blastRadius"].get("impactedFiles", []) or []
    paths = list(dict.fromkeys(p for p in paths if isinstance(p, str)))
    baseline_chars = 0
    read = 0
    for p in paths:
        fp = repo_root / p
        if fp.exists() and fp.is_file():
            baseline_chars += len(fp.read_text(errors="replace"))
            read += 1

    tok = lambda c: c // 4
    print(f"task: {args.task}")
    print(f"graph route pack : {pack_chars:>8} chars  (~{tok(pack_chars)} tokens)  [graph={'on' if graph else 'off'}]")
    print(f"read-files baseline: {baseline_chars:>8} chars  (~{tok(baseline_chars)} tokens)  over {read} files")
    if pack_chars and baseline_chars:
        print(f"context reduction: {baseline_chars / pack_chars:.1f}x")
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    repo_root, cfg, out_dir = load_runtime(args)
    # Self-bootstrap (transient, tree-clean) so the server works in a fresh
    # worktree without a prior `skyhook init`/`graph build`.
    db_path = _ensure_graph(repo_root, cfg, out_dir)
    from .mcp_server import McpUnavailable, serve

    try:
        serve(db_path)
    except McpUnavailable as exc:
        print(f"skyhook: {exc}", file=sys.stderr)
        return 2
    return 0


def _open_graph(out_dir: Path):
    """Open the graph read-only if it exists, else None (route degrades gracefully)."""
    db_path = out_dir / "graph.db"
    if not db_path.exists():
        return None
    try:
        from .graphstore import GraphStore

        return GraphStore(str(db_path), read_only=True)
    except Exception:
        return None


def _run_graph_query(store, args: argparse.Namespace):
    kind, arg = args.kind, args.arg
    if kind == "exists":
        return {"path": arg, "exists": store.file_exists(arg)}
    if kind == "defs":
        return store.find_symbol(arg)
    if kind == "symbols-in-file":
        return store.symbols_in_file(arg)
    if kind == "callers":
        return store.callers_of(arg, strict=getattr(args, "strict", False))
    if kind == "callees":
        return store.callees_of(arg)
    if kind == "blast-radius":
        return store.blast_radius(arg, depth=getattr(args, "depth", 3))
    if kind == "search":
        return store.search(arg)
    raise ValueError(f"unknown query kind: {kind}")


def _emit_graph(result, args: argparse.Namespace) -> None:
    import json as _json

    if getattr(args, "json", False):
        print(_json.dumps(result, indent=2))
        return
    if isinstance(result, dict) and "impacted" in result:  # blast-radius
        print(f"blast radius of {result['target']} (approximate): {len(result['impacted'])} symbols, "
              f"{len(result.get('impactedFiles', []))} files")
        for item in result["impacted"][:40]:
            print(f"  d{item['distance']}  {item['path']}::{item['name']}")
        return
    if isinstance(result, dict):
        for k, v in result.items():
            print(f"{k}: {v}")
        return
    for item in result:
        if isinstance(item, dict):
            loc = item.get("path", "")
            line = item.get("startLine") or item.get("line")
            loc = f"{loc}:{line}" if line else loc
            print(f"  {item.get('name','')}  [{item.get('structuralKind') or item.get('kind','')}]  {loc}".rstrip())
        else:
            print(f"  {item}")


def cmd_check(args: argparse.Namespace) -> int:
    repo_root, cfg, out_dir = load_runtime(args)
    scan = scan_repo(repo_root, cfg)
    errors = check_outputs(out_dir, scan.digest)
    errors += _check_graph(scan, out_dir)
    if errors:
        print("skyhook check failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        print("Run `skyhook init` to refresh artifacts.", file=sys.stderr)
        return 1
    print(f"skyhook check passed: {out_dir}")
    return 0


def _check_graph(scan, out_dir: Path) -> list:
    """When a committed graph.json exists, verify it matches a fresh in-memory build."""
    import json as _json

    graph_json = out_dir / "graph.json"
    if not graph_json.exists():
        return []
    from .graphstore import GraphStore
    from .resolve import resolve_calls

    store = GraphStore(":memory:")
    store.build(scan, full=True)
    resolve_calls(store)
    fresh = store.export_dict()
    store.close()
    try:
        committed = _json.loads(graph_json.read_text())
    except (OSError, ValueError):
        return ["graph.json is unreadable; run `skyhook graph build`"]
    # Compare structure (ignore the upstream scanDigest field).
    fresh.pop("scanDigest", None)
    committed.pop("scanDigest", None)
    if _json.dumps(fresh, sort_keys=True) != _json.dumps(committed, sort_keys=True):
        return ["graph.json is stale; run `skyhook graph build`"]
    return []


def cmd_route(args: argparse.Namespace) -> int:
    _repo_root, _cfg, out_dir = load_runtime(args)
    data = load_existing_map(out_dir)
    if data is None:
        raise ModelError(f"missing map artifact: {out_dir / 'map.json'}. Run `skyhook init` first.")
    errors = validate_map(data)
    if errors:
        raise ModelError("map failed validation: " + "; ".join(errors))
    graph = _open_graph(out_dir)
    try:
        route = build_route(data, _read_task(args), profile=args.profile, graph=graph)
    finally:
        if graph is not None:
            graph.close()
    if getattr(args, "save", False):
        paths = write_route(out_dir, route)
        print(f"skyhook route: wrote {out_dir / 'routes'}", file=sys.stderr)
        for path in paths:
            print(f"- {path}", file=sys.stderr)
    if getattr(args, "format", "markdown") == "json":
        print(canonical_json(route), end="")
    else:
        print(render_route_markdown(route), end="")
    return 0


def build_map(
    repo_root: Path,
    cfg: Any,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    max_files: Optional[int] = None,
    previous: Optional[Mapping[str, Any]] = None,
    error_prefix: str = "generated map",
):
    if max_files:
        cfg.scan.max_files = max_files
    if model:
        cfg.model.model = model
    scan = scan_repo(repo_root, cfg)
    orienter = choose_orienter(cfg, provider)
    data = enrich_map(orienter.orient(scan, previous=previous), scan)
    errors = validate_map(data)
    if errors:
        raise ModelError(f"{error_prefix} failed validation: " + "; ".join(errors))
    return data, scan


def load_runtime(args: argparse.Namespace):
    repo_root = Path(args.repo).resolve()
    cfg = load_config(repo_root, args.config)
    out_dir = output_dir(repo_root, cfg.output_dir, args.output_dir)
    try:
        rel_output = out_dir.relative_to(repo_root).as_posix()
    except ValueError:
        rel_output = ""
    if rel_output and rel_output not in cfg.scan.exclude:
        cfg.scan.exclude.append(rel_output)
    return repo_root, cfg, out_dir


def print_report(command: str, scan, out_dir: Path, wrote: bool, would_change: Optional[bool] = None) -> None:
    action = "wrote" if wrote else "dry-run"
    print(f"skyhook {command}: {action} {out_dir}")
    print(f"- files scanned: {len(scan.files)}")
    print(f"- primary languages: {', '.join(list(scan.language_counts.keys())[:5]) or 'none detected'}")
    print(f"- frameworks: {', '.join(scan.frameworks) or 'none detected'}")
    print(f"- docs: {len(scan.docs)}")
    if would_change is not None:
        print(f"- artifacts would change: {'yes' if would_change else 'no'}")
    if wrote:
        print("- artifacts: INDEX.md, map.md, map.json, docs.md, architecture.md, tests.md, areas/*.md")


def _read_task(args: argparse.Namespace) -> str:
    if getattr(args, "task", None):
        return str(args.task)
    if getattr(args, "task_file", None):
        return Path(args.task_file).read_text(encoding="utf-8")
    if not sys.stdin.isatty():
        return sys.stdin.read()
    raise ValueError("skyhook route requires --task, --task-file, or task text on stdin")


if __name__ == "__main__":
    raise SystemExit(main())
