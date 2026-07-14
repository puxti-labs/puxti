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

- `src/puxti/cli/` — entry point. One Typer command per module (`capture.py`,
  `scan.py`, …); `_app.py` holds the Typer apps, `_shared.py` the console/runner
  helpers, and `__init__.py` assembles `app` (import order = help listing order).
- `src/puxti/core/` — engine. `scanner.py` populates the graph from a dbt
  manifest; `capture.py` and `redefine.py` propagate changes; `corrector.py`
  fixes inferred definitions; `graph.py` is the high-level graph interface.
- `src/puxti/connectors/` — integrations. `dbt.py` parses manifests and
  generates SQL diffs; `github.py` opens PRs; `airflow.py` parses DAGs.
  Each connector implements the interface in `base.py`.
- `src/puxti/models.py` — pydantic entity, edge, and event types shared
  across modules.
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
  exception — Typer/Rich output is fine in `puxti/cli/`.
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
```

The test suite is self-contained — the Knowledge Graph is a local SQLite
file, so no external services are required.

## Architectural rules

- **Read-first, write-on-confirm.** Commands that modify external state
  (PRs to user repos, writes to the knowledge graph) must show a plan and
  require confirmation before acting. `--dry-run` should always work.
- **PRs as the output format.** Never push directly to a user's main
  branch. Generate PRs.
- **Connectors stay modular.** Don't couple connector logic to core
  propagation logic. Each connector implements `base.Connector`.
- **One graph backend.** Currently SQLite port;
  don't introduce additional backends in the meantime.
- **No new dependencies without justification.** The package's install
  weight matters for OSS adoption. Discuss new deps in an issue first.

## Change planning — trace the full journey before coding

Before writing any implementation plan, list every layer the change touches
from data source to user output, and call out any layer where the current code
would silently fail or produce wrong output (wrong key in the manifest, a node
type the traversal doesn't visit, an unhandled `None`). Get agreement on scope
before writing steps.

Puxti's journey trace is:

**manifest / DAG file → connector parse → scanner → graph write → propagation
engine → connector diff → PR → what the user actually sees.**

A change is only done when every layer in its trace is real. The failure this
guards against is a layer that silently produces *no effect* — a `capture` that
walks the graph, finds the downstream models, and emits a PR whose diff is
empty; a `--dry-run` that prints a plan the real run doesn't execute; a
connector registered but never invoked by the engine. If a layer is deliberate
scaffolding, keep it on a branch — don't ship a CLI flag or a command that
promises behaviour the engine doesn't implement.

The same rule applies to anything Puxti *says*: `describe`/`correct` output and
generated PR bodies must describe what the tool actually did. A PR body claiming
a propagation that didn't happen is the same defect class as an empty diff.

## Security

Before committing any change, check for:

- **Injection.** Three surfaces here:
  - **SQL.** User- and manifest-derived strings (model names, column names,
    descriptions) reach the SQLite queries in `core/graph.py`. Always use
    parameterised queries; never f-string a value into SQL. A dbt model can be
    named anything.
  - **Shell.** Anywhere Puxti shells out (`dbt`, `git`, `gh`), pass argument
    lists — never a composed shell string, never `shell=True`.
  - **Rendered output.** Manifest content flows into PR titles, PR bodies, and
    SQL diffs. Escape/quote it at the boundary; a description field is data, not
    markup.
- **LLM prompt injection.** `describe` and `correct` feed user-controlled content
  (model descriptions, column comments, SQL) into a model. That content must never
  be able to override instructions, exfiltrate other context, or steer a tool call.
  Sanitise before it enters a prompt, and treat model output as untrusted input to
  the next stage — a suggested definition is a *claim to validate*, not a fact.
- **Secret exposure.** This is the sharpest edge for a CLI. The Anthropic API key
  and the GitHub token live in `Settings` and must never appear in logs, tracebacks,
  Rich output, a `config` dump, or — worst case — a generated PR body or commit
  message pushed to a user's repo. Redact by default; a `Settings` value is not
  printable. `health` and `config` report *presence and shape*, never values.
- **Write scope.** Puxti holds a token against someone else's repository. The
  token's scope is the only authorization layer there is. Every connector write
  must target a branch Puxti created and a PR Puxti opened — never a push to a
  default branch, never a force-push, never a write outside the workspace the
  user pointed at.
- **Shared state.** Module-level mutation (caches, singletons, a graph driver
  held at import) is unsafe once anything runs concurrently and hides state
  across CLI invocations in tests.

## Resilient long-running work (LLM, network, graph writes)

`try/except` is necessary but **not sufficient**: it catches neither a **hang**
(the call never returns) nor a **kill** (OOM, Ctrl-C, CI timeout — which runs
neither `except` nor `finally`). Both leave the graph half-written, which is the
worst failure Puxti has: a partially-propagated graph is silently wrong on the
*next* run, not the current one.

Before merging any LLM call, GitHub API call, or multi-step graph write, confirm
all four:

1. **Deadline.** Bound the call with an explicit timeout that the client actually
   honours — the timeout must be passed to the HTTP/SDK layer, or it times
   nothing.
2. **Terminal state.** Every failure path records a durable outcome. An `except`
   that only logs and returns is an invisible failure.
3. **Recovery.** Any multi-step graph write must be **idempotent and re-runnable**
   — a kill skips your cleanup, so the fix is a `scan`/`capture` that converges
   when re-run, not a rollback you hope executes. Prefer transactional writes;
   where that's not possible, make the next run detect and repair the partial
   state.
4. **Observability.** No failure path is silent, and the structured logger is the
   channel — not `print`, not a swallowed exception.

## Tunables and flags

- **Tunables are not flags.** A threshold, cap, or limit ships as a `const` in
  the module that uses it. Promote it to `Settings` **only** when a user genuinely
  needs to change it without editing code. Don't grow the config surface
  speculatively — every `.env` key is documentation debt and an OSS support
  question.
- **Add a flag only when a `git revert` can't do the job**: a kill switch for
  something that writes external state, or a staged rollout. A reversible,
  additive change ships without one.
- **Every non-kill-switch flag is born with a retirement trigger**, stated in the
  PR. Retiring means deleting the flag, the dead branch, *and* the doubled tests.

## Tests

**A task is not done until tests pass.** Write tests alongside code, not after.
Any change that adds or modifies behaviour adds or updates a test in the same
change. If a change genuinely needs no test (docs, comments, packaging), say so
explicitly rather than silently skipping it.

```bash
uv run pytest                # all tests (no external services needed)
uv run pytest tests/core/    # one module
```

- **Test the destructive path from both sides.** Every command that writes
  external state must be tested with `--dry-run` **on** (asserting nothing was
  written: no PR opened, no graph mutation) and **off** (asserting the write
  actually happened). A suite that only exercises the happy write path can't
  catch a `--dry-run` that lies, and a suite that only mocks the connector can't
  catch a write that never lands.
- **Test the wrong input, not just the right one.** A malformed manifest, a
  missing node, a cyclic dependency, a model the graph has never seen. These are
  the inputs a real dbt project produces.
- **Exercise the branch you gated.** A flagged or config-gated path that no test
  turns on is never executed in CI. Set it inside the test and restore it
  afterwards.
- Anything touching graph SQL gets an **integration** test that runs against a
  real SQLite database (in-memory or a tmp file) — asserting on a generated query
  *string* proves nothing about whether the database will accept it.

## Dead code

Orphaning a module is free and invisible, so it grows unbounded unless something
fails on it. Keep `ruff` clean (unused imports and locals are errors, not
warnings) and check for orphaned modules before pushing. If a file you introduced
is unreferenced, wire it up end-to-end or delete it — don't leave it for later.

## Documentation

**Update docs when functionality changes.** When a command gains a flag, a scope
changes, a model changes, or behaviour shifts — update `README.md`, this file,
and any relevant doc in the same step. Not later.

Config defaults are read from `settings.py`, not from prose — this file names
*which* setting gates a path, never asserts its current default value. Defaults
drift; docs about defaults rot.

## Git

**Never commit, push, or tag without explicit instruction.** Completing a change
does not imply permission to commit it. Show diffs and wait for confirmation.

- Work on a **feature branch cut from `main`**, in its own worktree, so parallel
  sessions stay isolated:
  ```bash
  git fetch origin && git worktree add ../puxti-<description> -b feat/<description> origin/main
  ```
  Do all work for the task inside that worktree. Never start a new task in the
  main checkout.
- **Never push to `main` directly**, and never merge your own PR — leave it open
  for review.
- Before opening a PR, rebase if needed:
  `git fetch origin && git log --oneline HEAD..origin/main`
- Clean up after merge: `git worktree remove ../puxti-<description>`

## Out of scope for this repo

- Hosted or multi-tenant functionality — that's not part of the OSS package
- Web UI or frontend code — Puxti is a CLI tool
- Real-time/streaming connectors — batch-oriented dbt and Airflow only
- Auth, license validation, or backend services — removed in v0.6.0

## Further reading

- `README.md` — install and usage from a user's perspective
- `docs/architecture.md` — deeper technical design (when present)
- GitHub issues — current work and discussions
