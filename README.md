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

- **Structural layer** — entities, lineage edges, and column-level relationships extracted from your dbt manifest
- **Semantic layer** — business definitions and conceptual dependencies between entities, captured when changes are made

Together, these layers let Puxti reason about what a change *means* across the stack, not just what it touches mechanically.

### Three types of change

| Case | Example | How Puxti handles it |
|------|---------|----------------------|
| Column rename | `order_date` → `recorded_date` | Deterministic find-and-replace across all dbt models, opened as a PR |
| Definition redefinition | `gross_revenue` now excludes refunds | Traverses the semantic graph to find downstream entities whose *meaning* is now affected — annotates files for human review |
| Cross-system change | An Airflow DAG change affects a dbt source | `puxti link` declares the cross-system edge; `puxti capture` generates coordinated PRs in both repos |

---

## Requirements

- **Python 3.12 or 3.13** — dbt-duckdb is not yet compatible with Python 3.14
- **An LLM API key** (BYOK — bring your own key, from any supported provider) — for semantic reasoning. Your key stays in your environment and is used to call your provider directly — nothing goes through a Puxti-controlled server. The default provider is Anthropic (`claude-sonnet-4-6`): typical cost for `puxti scan` on a 20-model project is under $0.05; `puxti capture` on a single column is under $0.02. See [LLM providers](#llm-providers-byok) for OpenAI, Mistral, GLM, DeepSeek, Groq, OpenRouter, Gemini, AWS Bedrock, and local Ollama/vLLM.
- **GitHub personal access token** — with `repo` scope (Contents + Pull requests write)
- **A dbt project** with a compiled `manifest.json` (`dbt compile` or `dbt run`)

---

## Installation

```bash
pip install puxti
```

No authentication required. Puxti runs fully locally — no data or metadata leaves your machine except for the Anthropic API calls described above.

---

## Configuration

Copy the example env file and fill in your credentials:

```bash
cp .env.example .env
```

| Variable | Description |
|---|---|
| `ANTHROPIC_API_KEY` | Your Anthropic API key — get one at [console.anthropic.com](https://console.anthropic.com). Only needed with the default provider. |
| `GITHUB_TOKEN` | Personal access token with `repo` scope (Contents + Pull requests write) |
| `DBT_PROJECT_DIR` | Path to your dbt project root (the directory containing `dbt_project.yml`) |
| `DBT_PROFILES_DIR` | Path to your dbt profiles directory — usually `~/.dbt` |
| `LLM_PROVIDER` | LLM provider (optional, default `anthropic`) — see [LLM providers](#llm-providers-byok) |
| `LLM_MODEL` | Model ID (required for non-Anthropic providers; overrides the default `claude-sonnet-4-6` otherwise) |
| `LLM_API_KEY` | API key for the selected provider (falls back to `ANTHROPIC_API_KEY` for the default provider) |
| `LLM_BASE_URL` | Override the provider's base URL (custom endpoints, non-default Bedrock regions) |
| `LLM_INPUT_COST_PER_MTOK` / `LLM_OUTPUT_COST_PER_MTOK` | USD per million tokens for `--dry-run` cost estimates on models puxti doesn't know (optional) |
| `LLM_CONCURRENCY` | Max parallel LLM calls during `puxti scan` (optional, default 4) — raise only if your provider's rate-limit tier allows it |

---

## LLM providers (BYOK)

Puxti is provider-agnostic: bring a key from any of these and set two or three env vars. Every provider below speaks the OpenAI-compatible wire format at its own endpoint; Anthropic (the default) uses its native API.

```bash
# Example: Mistral
LLM_PROVIDER=mistral
LLM_MODEL=mistral-large-latest
LLM_API_KEY=...
```

| `LLM_PROVIDER` | Endpoint | Where to get a key |
|---|---|---|
| `anthropic` *(default)* | native Anthropic API | [console.anthropic.com](https://console.anthropic.com) — or just set `ANTHROPIC_API_KEY` |
| `openai` | api.openai.com | [platform.openai.com](https://platform.openai.com) |
| `mistral` | api.mistral.ai | [console.mistral.ai](https://console.mistral.ai) |
| `glm` | open.bigmodel.cn | [bigmodel.cn](https://open.bigmodel.cn) (Zhipu / GLM) |
| `deepseek` | api.deepseek.com | [platform.deepseek.com](https://platform.deepseek.com) |
| `groq` | api.groq.com | [console.groq.com](https://console.groq.com) |
| `openrouter` | openrouter.ai | [openrouter.ai](https://openrouter.ai) — one key, many models |
| `gemini` | Gemini OpenAI-compat endpoint | [aistudio.google.com](https://aistudio.google.com) |
| `bedrock` | Bedrock Mantle (`us-east-1`; other regions via `LLM_BASE_URL`) | Bedrock API key from the AWS console |
| `ollama` | localhost:11434 — no key needed | local install from [ollama.com](https://ollama.com) |
| `custom` | anything OpenAI-compatible via `LLM_BASE_URL` (vLLM, proxies) | your infrastructure |

Notes:

- **Cost estimates** (`--dry-run`): exact token counts and dollar costs on Anthropic; approximate token counts elsewhere, with dollar costs only when you provide `LLM_INPUT_COST_PER_MTOK` / `LLM_OUTPUT_COST_PER_MTOK` — puxti never guesses a price.
- **Quality**: the semantic prompts expect strict JSON output; frontier models handle this reliably, small local models may not.
- **Not covered**: Vertex AI (OAuth-based short-lived tokens, not a durable key). Claude on Bedrock works through the `bedrock` provider.

---

## Workspace config (`.puxti.yml`)

If your projects live in separate repos, add a `.puxti.yml` at the root of your workspace — the directory that contains the repo clones. Puxti discovers it by walking up from your current directory (git-style).

```yaml
version: 1

connectors:
  dbt:
    project_dir: ./dbt
    repo: your-org/your-dbt-repo
    base_branch: main

  airflow:
    project_dir: ./airflow
    repo: your-org/your-airflow-repo
    dags_dir: dags/
    base_branch: main

  # Application schema (Prisma ORM) — tables and fields become entities,
  # relations become lineage. schema_path defaults to prisma/schema.prisma.
  prisma:
    project_dir: ./app
    repo: your-org/your-app-repo
    schema_path: prisma/schema.prisma

  # Plain CREATE VIEW .sql files — views, their columns, and the tables they
  # read from become entities and lineage. dialect is any sqlglot dialect name.
  sql_views:
    project_dir: ./app
    repo: your-org/your-app-repo
    views_dir: db/views
    dialect: postgres
    default_schema: public
```

With this in place:
- `--repo` is no longer required for `capture`, `redefine`, or `health`
- `--dbt-project-dir` resolves from `connectors.dbt.project_dir` automatically
- `puxti health` checks GitHub write access for each connector repo
- `puxti scan` scans every configured producer (dbt, prisma, sql_views) and
  links references across them — a view reading a Prisma-managed table gets a
  real lineage edge
- `puxti capture` propagates column renames through every configured connector
  and opens one PR per repo

Prisma rename diffs patch `schema.prisma` only — applying them still requires
`prisma migrate dev` and `prisma generate`, and every generated PR says so.

Run `puxti config` from anywhere inside the workspace to verify it was found:

```
.puxti.yml:  /your/workspace/.puxti.yml  found
  connectors: dbt (your-org/your-dbt-repo · ./dbt), airflow (your-org/your-airflow-repo · ./airflow)
```

Precedence: CLI flags > `.puxti.yml` > environment variables.

---

## Verify setup

```bash
puxti config   # show resolved config values and file locations
puxti health   # verify connectivity to all services
```

Expected output from `puxti health`:

```
✓ Knowledge Graph  (~/.puxti/graph.db)
✓ Anthropic API key
✓ dbt manifest
✓ GitHub write access — your-org/your-dbt-repo (dbt)
```

---

## Usage

### Step 1 — Bootstrap the Knowledge Graph

Run this once before using `capture` or `redefine`. It reads your dbt manifest, populates the structural lineage graph, and uses the LLM to infer starter definitions for each model.

Two modes:

- **Interactive** — walks through each model one by one. The LLM proposes a definition; you confirm, edit, or skip before anything is written. Slower, higher accuracy. Recommended for first onboarding.
- **Auto** — generates all definitions in one pass, shows a summary table, and asks for a single confirmation before writing. Faster, but review carefully.

```bash
# Interactive mode
puxti scan --dbt-project-dir /path/to/dbt --interactive

# Auto mode
puxti scan --dbt-project-dir /path/to/dbt
```

Nothing is written to the Knowledge Graph without your explicit confirmation.

After definitions are confirmed, Puxti also proposes initial semantic edges between models and asks you to confirm those as a group.

---

### Step 1b — Link upstream producers (optional, but recommended)

If your dbt sources are loaded by Airflow DAGs, declare the cross-system link once:

```bash
puxti link --from task.airflow.salesforce_sync.extract_opportunities --to source.clariva.raw_opportunities --description "Extracts Salesforce opportunities. amount is a roll-up of OpportunityLineItem.TotalPrice post Q1 2024 migration."
```

Puxti creates a `FEEDS` edge in the Knowledge Graph connecting the Airflow task to the dbt source. When you later run `puxti capture` on a change to `raw_opportunities.amount`, the generated PR will include full cross-system context.

#### `link` options

| Flag | Required | Description |
|------|----------|-------------|
| `--from` | Yes | Entity producing data (`task.airflow.<dag>.<task>` or `model.<project>.<name>`) |
| `--to` | Yes | Entity receiving data (`source.<project>.<table>` or `model.<project>.<name>`) |
| `--description` / `-d` | Yes | Semantic description of the relationship — what data flows and what it means |

---

### Step 1c — Query impact before making a change (optional)

Before running `capture` or `redefine`, you can see exactly what depends on an entity — no LLM calls, no changes made:

```bash
# Show all dependents of an entity
puxti impact model.jaffle_shop.orders

# Scope to a specific change type
puxti impact model.jaffle_shop.orders --change-type rename
puxti impact model.jaffle_shop.orders --change-type redefine
puxti impact model.jaffle_shop.orders --change-type drop

# JSON output (same shape as the MCP impact_of_change tool)
puxti impact model.jaffle_shop.orders --json
```

Output shows each dependent entity, its hop distance from the target, and whether the relationship is semantic (concept-level) or structural (lineage/SQL reference). With `--change-type`, structural dependents are flagged as primary risk for `rename` and `drop`; semantic dependents for `redefine`.

#### `impact` options

| Flag | Required | Description |
|------|----------|-------------|
| `<entity>` | Yes | Entity ID to analyze (positional argument) |
| `--change-type` | No | One of `rename`, `redefine`, `drop`, `type_change` — scopes the risk annotation |
| `--json` | No | Output as JSON (same shape as the MCP `impact_of_change` tool) |

---

### Step 2a — Capture a column rename

```bash
puxti capture --entity "model.jaffle_shop.orders.order_date" --before "order_date" --after "recorded_date" --description "Renamed to clarify this is the date the order was recorded in the system, not the transaction date." --repo "your-org/your-dbt-repo"
```

Puxti will:

1. Call the LLM to enrich your description and reason about affected entities
2. Scan your dbt models for references to `order_date`
3. Generate diffs replacing all occurrences with `recorded_date`
4. Open a PR on `your-org/your-dbt-repo` with the diffs and semantic context

#### `capture` options

| Flag | Required | Description |
|------|----------|-------------|
| `--entity` / `-e` | Yes | Full entity ID (e.g. `model.project.model.column`) |
| `--before` | Yes | Value before the change (old column name) |
| `--after` | Yes | Value after the change (new column name) |
| `--description` / `-d` | Yes | Human description of what the change means and why |
| `--repo` | No | GitHub repo for the PR (`owner/repo`) — required unless `--dry-run` or set in `.puxti.yml` |
| `--base-branch` | No | Base branch for the PR (default: `main`, or from `.puxti.yml`) |
| `--dbt-project-dir` | No | Path to dbt project root (overrides `.puxti.yml` and `DBT_PROJECT_DIR`) |
| `--dry-run` | No | Estimate token count and cost without running the capture |

---

### Step 2b — Redefine what an entity means

Use this when the *meaning* of an entity changes — not a rename, a conceptual shift (e.g. a revenue metric now excludes refunds).

```bash
puxti redefine --entity "model.jaffle_shop.orders.gross_revenue" --description "gross_revenue now excludes refunds — only settled transactions count." --repo "your-org/your-dbt-repo"
```

Puxti will:

1. Traverse the semantic graph to find downstream entities whose meaning is now affected
2. Walk the structural ancestor chain upstream for passthrough changes
3. Generate SQL diffs with depth-aware confidence annotations
4. Open a PR with both upstream passthrough diffs and downstream semantic diffs

#### `redefine` options

| Flag | Required | Description |
|------|----------|-------------|
| `--entity` / `-e` | Yes | Entity ID whose meaning is changing |
| `--description` / `-d` | Yes | What the entity now means |
| `--repo` | No | GitHub repo for the PR (`owner/repo`) — required unless `--dry-run` or set in `.puxti.yml` |
| `--base-branch` | No | Base branch for the PR (default: `main`, or from `.puxti.yml`) |
| `--dbt-project-dir` | No | Path to dbt project root (overrides `.puxti.yml` and `DBT_PROJECT_DIR`) |
| `--dry-run` | No | Estimate token count and cost without calling the LLM or opening a PR |

---

### Step 2c — Correct an inaccurate definition

Use this when Puxti inferred a wrong definition during scan, or when you want to refine one without triggering code propagation.

```bash
puxti correct --entity "model.jaffle_shop.orders"
```

#### `correct` options

| Flag | Required | Description |
|------|----------|-------------|
| `--entity` / `-e` | Yes | Entity ID to correct |
| `--project` / `-p` | No | Validate the entity belongs to this project before proceeding |

---

### Step 3 — Inspect the Knowledge Graph

```bash
# Full overview — all entities grouped by project, all semantic edges
puxti describe

# Filter overview to a single project
puxti describe --project jaffle_shop

# Single entity — full definition, incoming and outgoing edges
puxti describe --entity "model.jaffle_shop.orders"
```

---

### Step 4 — Purge project data

```bash
# Remove a single project
puxti purge --project jaffle_shop

# Wipe the entire Knowledge Graph
puxti purge --all
```

---

## Known Limitations

**Re-scanning after dbt project changes**
`puxti scan` is safe to re-run at any time — it upserts entities and definitions, so new models and columns will be added and existing ones updated. However, there is no prompt or workflow that reminds you to re-scan when your dbt project changes. Run `puxti scan` again whenever you add models, rename columns in your dbt layer, or change descriptions in yml files.

**Finding entity IDs**
Commands like `capture`, `redefine`, and `correct` require a full entity ID (e.g. `model.jaffle_shop.orders.order_date`). If you are not sure of an entity's ID, run `puxti describe` first — it lists all entities with their full IDs.

**The `correct` → `redefine` handoff**
When you classify a correction as a "real change", Puxti prints the `redefine` command to run next and asks whether to run it immediately. Press Enter or anything other than `y` to keep the copy-paste workflow.

---

## Use with Claude Code / Cursor (MCP)

Puxti ships an MCP server so coding agents can query your knowledge graph without leaving their context window.

**Add to Claude Code** (`~/.claude/claude_code_config.json`):

```json
{
  "mcpServers": {
    "puxti": {
      "command": "puxti",
      "args": ["mcp", "serve"]
    }
  }
}
```

Once connected, four read-only tools are available:

| Tool | What it answers |
|---|---|
| `impact_of_change` | Which entities depend on this one and would break? |
| `consumers` | Which models directly read from this entity? |
| `describe_entity` | What does this entity mean? What semantic edges does it have? |
| `definition_history` | How has the meaning of this entity evolved over time? |

Run `puxti scan` in your dbt project first to populate the graph.

### Teach the agent to use the tools

The tools give an agent *access* to the graph; they don't tell it *when* to consult the
graph or how to report what it found. Without that, an agent answers a metric question
from the first plausible model and returns a stale-but-confident number — the exact
failure that makes agentic analytics untrustworthy.

`puxti mcp init` writes an agent skill that closes the gap:

```bash
puxti mcp init            # writes .claude/skills/puxti-analytics/SKILL.md
puxti mcp init --print    # same markdown to stdout — paste into Cursor rules, CLAUDE.md, any agent
```

The skill instructs the agent to check each entity's current definition and
`definition_history` before trusting a model, honor the latest definition (a model whose
SQL lags its definition is flagged as unreliable, not reported as fact), and end every
metric answer with a provenance footer citing the definition version, who authored it,
and when. It is puxti's small-scale version of the "skills" layer from Anthropic's
self-service analytics work.

---

## Telemetry

Puxti collects anonymous, opt-in usage analytics. Telemetry is **off by default**.

```bash
puxti telemetry on     # enable
puxti telemetry off    # disable
puxti telemetry show   # check current status
```

When enabled, each command reports: command name, duration, exit status (0 or 1), and a random install ID (no project data, no entity names, no SQL). Events go to PostHog EU (`eu.i.posthog.com`). See [`TELEMETRY.md`](TELEMETRY.md) for the full field list.

---

## Contributing

Open an issue on [GitHub](https://github.com/puxti-labs/puxti) to report
bugs, request features, or ask questions. A good first contribution is
fixing one of the known limitations above.

AI-assisted contributions are welcome — Puxti's audience uses AI coding
tools and we do too. We ask the same thing of every contributor, AI-assisted
or not: be able to explain what your change does, why it's the right
approach, and how it interacts with the rest of the codebase. PRs whose
authors can't engage with review feedback won't be merged regardless of
how they were produced.

See [CONTRIBUTING.md](CONTRIBUTING.md) for dev setup and what a good PR
looks like. For non-trivial changes, open an issue first to discuss the
approach before writing code.

---

## License

Apache 2.0. See [LICENSE](./LICENSE).

---

## Development

### Setup

```bash
uv sync --extra dev    # install runtime + dev dependencies (pytest, ruff, twine)
```

### Running tests

```bash
uv run pytest                       # run all tests
uv run pytest tests/cli            # run CLI command tests only
uv run pytest -v                    # verbose output
```

### Linting and formatting

```bash
uv run ruff check src/     # lint
uv run ruff format src/    # format
```

---

## Project structure

```
src/puxti/
├── cli/                    # entry point — one module per command
│   ├── _app.py             # Typer apps + root callback
│   ├── _shared.py          # consoles, async runner, workspace loading
│   └── capture.py, scan.py, …   # one command each
├── models.py               # all data types (Pydantic)
├── settings.py             # config (reads from .env)
├── workspace.py            # .puxti.yml discovery and resolution
├── core/
│   ├── capture.py          # semantic capture — LLM enrichment + Knowledge Graph write
│   ├── corrector.py        # puxti correct — definition correction without propagation
│   ├── graph.py            # Knowledge Graph — SQLite backend (~/.puxti/graph.db)
│   ├── scanner.py          # puxti scan — bootstraps KG from producer connectors
│   ├── resolution.py       # cross-connector table-reference resolution
│   └── redefine.py         # puxti redefine — semantic change propagation
├── connectors/
│   ├── base.py             # connector interface
│   ├── registry.py         # builds configured producers from .puxti.yml
│   ├── airflow.py          # Airflow connector — DAG parsing + task diff generation
│   ├── dbt.py              # dbt connector — entity extraction + diffs
│   ├── prisma.py           # Prisma connector — schema.prisma models, relations, rename diffs
│   ├── sql_views.py        # SQL views connector — CREATE VIEW files via sqlglot
│   └── github.py           # GitHub connector — PR creation
└── propagation/
    └── engine.py           # propagation engine — orchestrates connectors
```
