# AGENTS.md

Context for AI coding assistants working on Puxti. Read before making
non-trivial changes.

## What Puxti does

Puxti is a Python CLI that builds a knowledge graph from a dbt project,
detects schema and semantic changes, and generates downstream updates as
reviewable GitHub PRs. The CLI ships a small set of commands (`scan`,
`capture`, `redefine`, `link`, `correct`, `describe`, `purge`) plus
operational verbs (`config`, `health`).

## Repo layout

- `src/puxti/cli.py` — entry point. All commands defined here using Typer.
- `src/puxti/core/` — engine. `scanner.py` populates the graph from a dbt
  manifest; `capture.py` and `redefine.py` propagate changes; `corrector.py`
  fixes inferred definitions; `graph.py` is the high-level graph interface.
- `src/puxti/connectors/` — integrations. `dbt.py` parses manifests and
  generates SQL diffs; `github.py` opens PRs; `airflow.py` parses DAGs.
  Each connector implements the interface in `base.py`.
- `src/puxti/graph/` — graph storage. `repository.py` is the Neo4j interface;
  `models.py` defines node and edge types.
- `src/puxti/propagation/engine.py` — orchestrates connectors during
  propagation.
- `src/puxti/settings.py` — pydantic settings, reads from `.env`.
- `src/puxti/workspace.py` — discovers and loads `.puxti.yml`.
- `tests/` — mirrors `src/puxti/`.

## Conventions

- Type hints on all function signatures. Run `uv run ruff check src/` to
  verify.
- Format with `uv run ruff format src/`.
- Pydantic for data models. No raw dicts crossing module boundaries.
- Use the structured logger, not `print`. CLI output formatting is the
  exception — Typer/Rich output is fine in `cli.py`.
- Read environment via `Settings` in `settings.py`. Never read `os.environ`
  directly outside that file.
- Never commit `.env`, credentials, or `__pycache__/` directories.

## Running things

```bash
uv sync --extra dev          # install with dev dependencies
uv run pytest                # all tests
uv run pytest tests/core/    # one module's tests
uv run ruff check src/       # lint
uv run ruff format src/      # format
docker compose up -d         # start Neo4j (required for most commands)
```

The full test suite expects Neo4j running locally. Unit tests that don't
touch the graph can run without it.

## Architectural rules

- **Read-first, write-on-confirm.** Commands that modify external state
  (PRs to user repos, writes to the knowledge graph) must show a plan and
  require confirmation before acting. `--dry-run` should always work.
- **PRs as the output format.** Never push directly to a user's main
  branch. Generate PRs.
- **Connectors stay modular.** Don't couple connector logic to core
  propagation logic. Each connector implements `base.Connector`.
- **One graph backend.** Currently Neo4j. A SQLite port is planned;
  don't introduce additional backends in the meantime.
- **No new dependencies without justification.** The package's install
  weight matters for OSS adoption. Discuss new deps in an issue first.

## Out of scope for this repo

- Hosted or multi-tenant functionality — that's not part of the OSS package
- Web UI or frontend code — Puxti is a CLI tool
- Real-time/streaming connectors — batch-oriented dbt and Airflow only
- Auth, license validation, or backend services — removed in v0.6.0

## Git

Don't commit, push, or tag without explicit instruction. Show diffs and
wait for confirmation.

## Further reading

- `README.md` — install and usage from a user's perspective
- `docs/architecture.md` — deeper technical design (when present)
- GitHub issues — current work and discussions
