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
    print_report("init", scan, out_dir, wrote=True)
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    repo_root, cfg, out_dir = load_runtime(args)
    scan = scan_repo(repo_root, cfg)
    errors = check_outputs(out_dir, scan.digest)
    if errors:
        print("skyhook check failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        print("Run `skyhook init` to refresh artifacts.", file=sys.stderr)
        return 1
    print(f"skyhook check passed: {out_dir}")
    return 0


def cmd_route(args: argparse.Namespace) -> int:
    _repo_root, _cfg, out_dir = load_runtime(args)
    data = load_existing_map(out_dir)
    if data is None:
        raise ModelError(f"missing map artifact: {out_dir / 'map.json'}. Run `skyhook init` first.")
    errors = validate_map(data)
    if errors:
        raise ModelError("map failed validation: " + "; ".join(errors))
    route = build_route(data, _read_task(args), profile=args.profile)
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
