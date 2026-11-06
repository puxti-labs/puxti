# Puxti

[![PyPI version](https://img.shields.io/pypi/v/puxti.svg)](https://pypi.org/project/puxti/)
[![PyPI downloads](https://img.shields.io/pypi/dm/puxti.svg)](https://pypi.org/project/puxti/)
[![Python versions](https://img.shields.io/pypi/pyversions/puxti.svg)](https://pypi.org/project/puxti/)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Docs](https://img.shields.io/badge/docs-getpuxti.com-informational)](https://getpuxti.com/docs.html)

Puxti helps data teams propagate schema and semantic changes across dbt and the downstream stack as reviewable PRs. Each change also captures business meaning, so future changes get cheaper and safer.

```bash
pip install puxti
```

Python 3.12 or 3.13 required. No signup, no license key.

---

## How it works

Puxti sits in the critical path of making data changes. Every change goes through two layers:

- **Structural layer** — entities and lineage extracted from your dbt manifest and, optionally, Airflow DAGs, a Prisma schema and SQL view files
- **Semantic layer** — business definitions and conceptual dependencies between entities, captured when changes are made

Together, these layers let Puxti reason about what a change *means* across the stack, not just what it touches mechanically.

### Three types of change

| Case | Example | How Puxti handles it |
|------|---------|----------------------|
| Column rename | `order_date` → `recorded_date` | Deterministic rename in the model, its direct dependents and the matching `schema.yml` entries, opened as a PR. Anything it cannot rename safely is listed for manual review |
| Definition redefinition | `gross_revenue` now excludes refunds | Traverses the semantic graph to find downstream entities whose *meaning* is now affected, proposes SQL diffs labelled by confidence, and annotates deeper models for human review |
| Cross-system change | An Airflow DAG change affects a dbt source | `puxti link` declares the cross-system edge; `puxti capture` opens one PR per affected repo, linked by a change ID |

---

## Requirements

- **Python 3.12 or 3.13** — dbt-duckdb is not yet compatible with Python 3.14
- **An LLM API key** (BYOK) from any supported provider. Your key stays in your environment and calls your provider directly. The default is Anthropic (`claude-sonnet-5-5`); `puxti scan` on a 20-model project typically costs under $0.05. See [LLM providers](https://github.com/puxti-labs/puxti/blob/main/docs/configuration.md#llm-providers-byok).
- **GitHub personal access token** with `repo` scope (Contents + Pull requests write)
- **A dbt project** with a compiled `manifest.json` (`dbt compile` or `dbt run`)

Puxti runs fully locally. No data or metadata leaves your machine except the calls to your LLM provider and GitHub, and anonymous usage telemetry if you opt in (off by default).

---

## Quick start

```bash
pip install puxti
export ANTHROPIC_API_KEY=sk-ant-... GITHUB_TOKEN=ghp_... DBT_PROJECT_DIR=./dbt   # or put them in a .env file
puxti health              # verify the key, the manifest and GitHub access
puxti scan                # build the Knowledge Graph; nothing is written without your confirmation
puxti impact model.jaffle_shop.orders   # what depends on orders, no LLM call
```

Then connect a coding agent so it can query the graph: in Claude Code run `claude mcp add puxti -- puxti mcp serve`, then `puxti mcp init` to install the agent skill. See [MCP](https://github.com/puxti-labs/puxti/blob/main/docs/mcp.md).

---

## Commands

| Command | What it does |
|---|---|
| `puxti scan` | Build the Knowledge Graph from dbt, Prisma and SQL views, including MetricFlow metrics |
| `puxti impact` | Show what depends on an entity, no LLM call |
| `puxti capture` | Propagate a column rename and open a PR |
| `puxti redefine` | Change what an entity means and open a PR for the affected models |
| `puxti correct` | Fix a wrong definition without touching code |
| `puxti define` / `puxti bind` | Define a metric before a model implements it, then bind it |
| `puxti link` | Declare a cross-system edge, such as an Airflow task feeding a dbt source |
| `puxti describe` / `puxti graph` | Inspect the graph in the terminal or as an HTML page |
| `puxti mcp serve` / `puxti mcp init` | Serve the graph to coding agents and teach them to use it |
| `puxti config` / `puxti health` | Show resolved config and check connectivity |
| `puxti purge` | Remove a project or the whole graph |
| `puxti telemetry` | Turn anonymous usage telemetry on or off, or show its state |

---

## Guides

- [Configuration](https://github.com/puxti-labs/puxti/blob/main/docs/configuration.md): environment variables, LLM providers, Postgres storage, `.puxti.yml` workspaces
- [Commands](https://github.com/puxti-labs/puxti/blob/main/docs/commands.md): every command with examples and options
- [Metrics](https://github.com/puxti-labs/puxti/blob/main/docs/metrics.md): MetricFlow import, `define` and `bind`
- [MCP](https://github.com/puxti-labs/puxti/blob/main/docs/mcp.md): Claude Code and Cursor setup, the tools, the agent skill
- [Known limitations](https://github.com/puxti-labs/puxti/blob/main/docs/limitations.md)
- [Architecture](https://github.com/puxti-labs/puxti/blob/main/docs/architecture.md): how puxti works inside
- [getpuxti.com/docs](https://getpuxti.com/docs.html)

---

## Telemetry

Anonymous, opt-in usage analytics, **off by default**. Turn it on with `puxti telemetry on`. When enabled, each command reports its name, duration, exit status and a random install ID: no project data, no entity names, no SQL. See [`TELEMETRY.md`](https://github.com/puxti-labs/puxti/blob/main/TELEMETRY.md).

---

## Support the project

If Puxti saved you a migration, a ⭐ on [GitHub](https://github.com/puxti-labs/puxti) helps other data teams find it.

The CLI asks for this once, after your first successful command in an interactive terminal. It records `star_shown = true` in `~/.puxti/config.toml` and never shows the message again. CI runs and coding agents never see it.

---

## Contributing

Bug reports, feature requests and questions go in [GitHub issues](https://github.com/puxti-labs/puxti/issues). AI-assisted contributions are welcome. See [CONTRIBUTING.md](https://github.com/puxti-labs/puxti/blob/main/CONTRIBUTING.md) for dev setup, the project layout and what a good PR looks like.

---

## License

Apache 2.0. See [LICENSE](https://github.com/puxti-labs/puxti/blob/main/LICENSE).
