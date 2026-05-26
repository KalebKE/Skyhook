# Skyhook

Skyhook is a lightweight CLI for generating repository orientation maps for coding agents.

It is not a full semantic index, vector database, or search replacement. It is a fast wayfinding layer: scan the repo, identify the important code and documentation structure, and write compact artifacts that help an agent decide where to read next.

## Why

Large agent sessions waste time rediscovering the same things:

- where the app entrypoints are
- which directories own which domains
- where ADRs, C4 diagrams, architecture docs, and design docs live
- which README or agent instructions should be read first
- which files are likely relevant before opening a PR

Skyhook turns that into a small set of generated files agents can read quickly.
It also creates task-specific route packs so an agent can start with the
smallest useful slice of context for the current issue.

## Install

From a checkout:

```sh
python3 -m pip install -e .
```

Run without installing:

```sh
python3 -m skyhook --help
```

## Commands

### `skyhook init`

Run this when introducing Skyhook to a repo:

```sh
skyhook init
```

It writes:

- `.skyhook/INDEX.md`
- `.skyhook/map.md`
- `.skyhook/map.json`
- `.skyhook/docs.md`
- `.skyhook/architecture.md`
- `.skyhook/tests.md`
- `.skyhook/areas/<area>.md`

Use a model when `OPENAI_API_KEY` is set:

```sh
OPENAI_API_KEY=... skyhook init --provider openai
```

Use deterministic offline mode:

```sh
skyhook init --provider static
```

### `skyhook route`

Run this when an agent has a task or issue body and needs the shortest useful
path into the repository:

```sh
skyhook route --task "add retry handling to sync failures"
```

It reads `.skyhook/map.json` and prints a compact route pack with:

- files and docs to read first
- likely edit targets
- relevant tests
- architecture and design references
- constraints, gotchas, and search terms
- evidence explaining why each item was selected

Choose a route profile for the kind of work:

```sh
skyhook route --profile technical_breakdown --task-file issue.md
skyhook route --profile code_review --task-file pr-notes.md
skyhook route --profile bug_hunt --task "diagnose empty dashboard cards"
```

Built-in profiles:

- `product_planning`
- `requirements_planning`
- `technical_breakdown`
- `implementation`
- `code_review`
- `bug_hunt`

Use an issue file:

```sh
skyhook route --task-file issue.md
```

Emit JSON for harnesses:

```sh
skyhook route --task-file issue.md --format json
```

Persist the route under `.skyhook/routes/`:

```sh
skyhook route --task-file issue.md --save
```

### `skyhook check`

Use this in CI:

```sh
skyhook check
```

It validates required artifacts and fails if `.skyhook/map.json` is stale relative to the current scan digest.

## Model Provider

Skyhook supports an OpenAI-compatible chat completions endpoint through the Python standard library.

Environment variables:

- `OPENAI_API_KEY` or `SKYHOOK_API_KEY`
- `OPENAI_BASE_URL` or `SKYHOOK_BASE_URL`
- `SKYHOOK_MODEL`

Default model: `gpt-4.1-mini`.

If no API key is available and provider is `auto`, Skyhook uses the static fallback so the CLI remains usable in local and CI environments.

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

The current implementation uses a small YAML subset so Skyhook does not require PyYAML.

## Output Model

`map.json` is the canonical artifact. Markdown files are rendered from the same structure.

Top-level sections:

- `repo`
- `scan`
- `orientation`
- `codeAreas`
- `docs`
- `architecture`
- `symbols`
- `tests`

The data model is intentionally navigational. It should point agents to the right code and docs, not list every symbol in a repository.
Code areas may also include responsibilities, public contracts, dependencies,
relevant tests, verification commands, change rules, danger zones, common task
patterns, and evidence.

## Generated Layers

The generated markdown is intentionally layered:

- `INDEX.md` is the compact entrypoint for agents.
- `map.md` is the full generated overview.
- `areas/<area>.md` contains focused subsystem context.
- `tests.md` captures discovered tests and verification hints.
- `docs.md` and `architecture.md` point agents to durable project intent.
- `routes/<hash>.md` is created only when `skyhook route --save` is used.

## Development

```sh
python3 -m unittest
python3 -m skyhook init --provider static --dry-run
python3 -m skyhook route --profile implementation --task "add retry handling to sync failures"
```
