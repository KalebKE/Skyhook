<p align="center">
  <img src="assets/skyhook-tractor.svg" alt="Skyhook tractor" width="180">
</p>

# Skyhook

Skyhook builds a small, readable map of a repository — backed by a tree-sitter
**AST symbol + call graph** — and uses it to create task-specific route packs for
coding agents, answer structural queries (callers, callees, blast radius), and
serve those queries over MCP.

It is not a vector database or full semantic index. It is a fast wayfinding
layer plus a precise structural graph: where to start reading, which docs and
tests matter, which files an edit impacts, and what calls what — without the
agent grep-exploring the repo. On sample tasks this cut context from reading
the relevant files (~35k tokens) down to the route pack (~2k): a **~7–17x
reduction depending on the repo** — best case on a focused Python codebase,
~7x on a large polyglot one. Measure it for your repo with `skyhook bench`.

## The Problem

Large repositories burn time and tokens before useful work starts. A new agent session often has to rediscover the same things:

- where the main entrypoints are
- which directories own which features or domains
- where architecture docs, ADRs, C4 diagrams, and design notes live
- which tests matter for a change
- which files are likely edit targets for a bug, review, or implementation task
- what local conventions are worth reading before touching code

Normal search helps after you know what to search for. A full index can help too, but it is heavier than many teams need and can still bury the starting point in too much detail. Skyhook is intentionally smaller. It produces a compact map that agents can read before they open a broad set of files.

## How Skyhook Solves It

`skyhook init` scans the repository and writes generated orientation artifacts under `.skyhook/`.

The important artifacts are:

- `.skyhook/INDEX.md`: short entrypoint for agents
- `.skyhook/map.md`: full generated overview
- `.skyhook/map.json`: canonical machine-readable map
- `.skyhook/docs.md`: documentation inventory
- `.skyhook/architecture.md`: architecture and design references
- `.skyhook/tests.md`: discovered tests and verification hints
- `.skyhook/areas/<area>.md`: focused notes for detected code areas
- `.skyhook/graph.json`: diffable export of the AST symbol + call graph (committed)
- `.skyhook/graph.db`: the SQLite graph queried by `skyhook graph` and `skyhook mcp` (regenerable; gitignored)

`skyhook init` builds the map **and** the graph in one pass. `skyhook graph
query` answers structural questions (`callers`, `callees`, `blast-radius`,
`exists`, `symbols-in-file`, `search`, `defs`); `skyhook mcp` serves the same as
read-only MCP tools for any agent/editor.

`skyhook route` takes a task, issue, review note, or bug report and builds a smaller route pack from the map. Route packs are shaped by profile:

- `product_planning`
- `requirements_planning`
- `technical_breakdown`
- `implementation`
- `code_review`
- `bug_hunt`

The route pack is the part an agent should read for a specific job. The map is the durable repo orientation.

## Why It Works This Way

Skyhook favors generated files over a service because agents and humans can inspect the output, commit it if they want, diff it in review, and use it on local machines or CI runners without extra infrastructure.

The map is navigational by design — good enough to point to the right code and
docs quickly. The **AST graph** underneath it adds the precise layer: real
symbols (functions, classes, methods with scope), imports, and call edges parsed
with tree-sitter (Python, Swift, Kotlin, Java, JavaScript, TypeScript, Go,
Elixir). Call resolution is name-based and **approximate** (every result says
so); precise binding via stack-graphs is a future addition. The graph is what
lets `skyhook route` embed exact call chains and blast radius so agents stop
guessing.

Model usage is optional. With an OpenAI-compatible provider, Skyhook can produce better summaries. Without a key, static mode still produces deterministic output from filenames, docs, symbols, imports, tests, and repository structure.

## Install

From a checkout:

```sh
python3 -m pip install -e .
```

From another repository or a remote runner:

```sh
python3 -m pip install "git+https://github.com/KalebKE/Skyhook.git"
```

Run without installing:

```sh
python3 -m skyhook --help
```

## First Run

From the repository you want to map:

```sh
skyhook init
```

Use deterministic offline mode:

```sh
skyhook init --provider static
```

Use a model when an API key is available:

```sh
OPENAI_API_KEY=... skyhook init --provider openai
```

After this, point agents at `.skyhook/INDEX.md` first. For task-specific work, use `skyhook route`.

## Configuration

Optional config lives at `.skyhook/config.yaml`.

```yaml
version: 1
outputDir: .skyhook
model:
  provider: auto
  model: auto
scan:
  include:
    - .
  exclude:
    - build
    - dist
    - node_modules
    - .git
    - .gradle
    - .sim-worktrees
  maxFiles: 5000
docs:
  extraGlobs:
    - "docs/**/*.md"
    - "adr/**/*.md"
    - "architecture/**/*.md"
    - "**/*ADR*.md"
    - "**/*C4*.md"
```

The YAML parser intentionally supports a small subset. Skyhook does not require PyYAML.

Use `include` to keep the scan focused in monorepos. Use `exclude` for generated output, dependency directories, build artifacts, and large local worktrees.

## Model Provider

Skyhook supports an OpenAI-compatible chat completions endpoint through the Python standard library.

Environment variables:

- `OPENAI_API_KEY` or `SKYHOOK_API_KEY`
- `OPENAI_BASE_URL` or `SKYHOOK_BASE_URL`
- `SKYHOOK_MODEL`

Default model: `gpt-4.1-mini`.

If no API key is available and provider is `auto`, Skyhook uses static mode.

## Commands

### `skyhook init`

Generate or refresh the map:

```sh
skyhook init
```

Preview whether output would change:

```sh
skyhook init --provider static --dry-run
```

### `skyhook route`

Route a task through the map:

```sh
skyhook route --task "add retry handling to sync failures"
```

Use a task file:

```sh
skyhook route --profile implementation --task-file issue.md
```

Use a profile that matches the work:

```sh
skyhook route --profile technical_breakdown --task-file issue.md
skyhook route --profile code_review --task-file pr-notes.md
skyhook route --profile bug_hunt --task "diagnose empty dashboard cards"
```

Emit JSON for a harness:

```sh
skyhook route --task-file issue.md --format json
```

Persist the route under `.skyhook/routes/`:

```sh
skyhook route --task-file issue.md --save
```

### `skyhook graph`

Build and query the AST symbol + call graph. `skyhook init` builds it
automatically; `skyhook graph build` rebuilds it on its own (incremental by
default, `--full` to rebuild from scratch).

```sh
skyhook graph build
skyhook graph query callers BillingService          # who calls it
skyhook graph query callees BillingService          # what it calls
skyhook graph query blast-radius src/billing.py      # what an edit impacts
skyhook graph query exists src/billing.py            # does this path exist?
skyhook graph query symbols-in-file src/billing.py
skyhook graph query search Billing
skyhook graph stats
```

Add `--json` for a harness. Call resolution is approximate; results are flagged.

### `skyhook mcp`

Serve the graph as read-only MCP tools (`find_symbol`, `callers_of`,
`callees_of`, `blast_radius`, `file_exists`, `symbols_in_file`, `search`,
`graph_stats`) for any MCP client. Requires the `mcp` extra
(`pip install 'skyhook[mcp]'`, Python >= 3.10).

```sh
skyhook mcp --repo .
```

Register with a client (Claude Code, Cursor, Copilot, a pipeline):

```json
{ "mcpServers": { "skyhook": { "command": "skyhook", "args": ["mcp", "--repo", "/abs/path/to/repo"] } } }
```

### `skyhook bench`

Estimate the context reduction of a graph route pack versus reading the files an
agent would otherwise open:

```sh
skyhook bench --task "fix retry handling in BillingService"
```

### `skyhook check`

Use this when `.skyhook/` artifacts are committed and you want CI to catch stale
maps **and** stale graphs (`graph.json`):

```sh
skyhook check
```

`check` fails when `.skyhook/map.json` does not match the current repository scan digest.

## When To Rerun

Run `skyhook init`:

- when adding Skyhook to a repository
- after large directory moves or module splits
- after adding or removing major entrypoints
- after changing important architecture, ADR, design, or testing docs
- before handing a stale repository to a new agent session
- before opening a PR if `.skyhook/` artifacts are part of the repo contract

Run `skyhook route` for each meaningful task. A route is task-specific and should not be treated as a permanent repo map.

Run `skyhook check` in CI only if you commit `.skyhook/` artifacts and want freshness enforced.

## CI Example

This example assumes the repository commits `.skyhook/` artifacts and wants pull requests to fail when the map is stale.

```yaml
name: skyhook

on:
  pull_request:

jobs:
  check-map:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: python3 -m pip install "git+https://github.com/KalebKE/Skyhook.git"
      - run: skyhook check
```

## Relationship To BurnPlan

Skyhook answers: where should an agent start reading for this repo or task?

BurnPlan answers: what does the project want to become, what weak points are emerging, and how should agent teams use the map over time?

Use Skyhook by itself when you only need wayfinding. Add BurnPlan when you want onboarding interviews, code health notes, documentation proposals, and team routing.

## Development

```sh
python3 -m unittest
python3 -m skyhook init --provider static --dry-run
python3 -m skyhook route --profile implementation --task "add retry handling to sync failures"
```
