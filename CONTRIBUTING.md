# Contributing to Puxti

Thanks for considering a contribution. Puxti is a small, focused codebase —
most changes are reviewable in one sitting, and we'd like to keep it that way.

## Before you start

- **Bugs and questions**: open an issue with what you ran, what you expected,
  and what happened. `puxti --version` and your `LLM_PROVIDER` help.
- **Features and non-trivial changes**: open an issue *first* to discuss the
  approach. It saves you from building something we can't merge.
- **Good first contributions**: issues labeled
  [`good first issue`](https://github.com/puxti-labs/puxti/issues?q=is%3Aissue+is%3Aopen+label%3A%22good+first+issue%22),
  or fixing one of the known limitations listed in the README.

## Development setup

Puxti uses [uv](https://docs.astral.sh/uv/). Python 3.12 or 3.13.

```bash
git clone https://github.com/puxti-labs/puxti && cd puxti
uv sync --extra dev        # install everything, including dev tools
uv run pytest              # run the full test suite (~370 tests, no external services)
uv run ruff check src/     # lint
```

No API keys, Docker, or external services are needed to run the tests —
everything LLM- or network-facing is mocked, and the Knowledge Graph is a
local SQLite file.

To try your changes against a real project, the
[puxti-demo-project](https://github.com/puxti-labs/puxti-demo-project)
(dbt) and
[airflow-demo-project](https://github.com/puxti-labs/airflow-demo-project)
repos exist exactly for that.

## What a good PR looks like

- **Tests ship with the change.** Any change that adds or modifies behavior
  adds or updates a test in the same PR. If a change genuinely needs no test
  (docs, comments), say so in the PR description rather than silently skipping it.
- **A CHANGELOG entry** under `## [Unreleased]` for anything user-visible.
- **Docs move with behavior.** If a command gains a flag or behavior shifts,
  update the README and `--help` text in the same PR.
- **No drive-by refactors.** A bug fix is a bug fix; keep unrelated cleanups
  for their own PR so review stays easy.
- **No new lint findings**: `uv run ruff check src/` should not get worse.

Deeper conventions (layering, error handling, tunables policy, security
checklist) live in [AGENTS.md](AGENTS.md) — written for AI coding agents,
equally useful for humans.

## AI-assisted contributions

Welcome — Puxti's audience uses AI coding tools and we do too. We ask the
same thing of every contributor, AI-assisted or not: be able to explain what
your change does, why it's the right approach, and how it interacts with the
rest of the codebase. PRs whose authors can't engage with review feedback
won't be merged regardless of how they were produced.

## Security

Please report suspected vulnerabilities privately — see
[SECURITY.md](SECURITY.md). Don't open public issues for security problems.

## License

By contributing, you agree that your contributions are licensed under the
[Apache License 2.0](LICENSE).
