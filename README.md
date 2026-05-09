# Puxti

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
- **Docker** — for running Neo4j locally (until the SQLite port lands)
- **Anthropic API key** (BYOK) — for semantic reasoning. Your key stays in your environment and is used to call the Anthropic API directly — nothing goes through a Puxti-controlled server. Calls use `claude-sonnet-4-6`. Typical cost for `puxti scan` on a 20-model project is under $0.05; `puxti capture` on a single column is under $0.02.
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
| `NEO4J_URI` | Bolt connection string — `bolt://localhost:7687` if running locally via Docker |
| `NEO4J_USERNAME` | Neo4j username — `neo4j` by default |
| `NEO4J_PASSWORD` | Neo4j password — must match `NEO4J_AUTH` in `docker-compose.yml` |
| `ANTHROPIC_API_KEY` | Your Anthropic API key — get one at [console.anthropic.com](https://console.anthropic.com) |
| `GITHUB_TOKEN` | Personal access token with `repo` scope (Contents + Pull requests write) |
| `DBT_PROJECT_DIR` | Path to your dbt project root (the directory containing `dbt_project.yml`) |
| `DBT_PROFILES_DIR` | Path to your dbt profiles directory — usually `~/.dbt` |

---

## Workspace config (`.puxti.yml`)

If your dbt and Airflow projects live in separate repos, add a `.puxti.yml` at the root of your workspace — the directory that contains both repo clones. Puxti discovers it by walking up from your current directory (git-style).

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
```

With this in place:
- `--repo` is no longer required for `capture`, `redefine`, or `health`
- `--dbt-project-dir` resolves from `connectors.dbt.project_dir` automatically
- `puxti health` checks GitHub write access for each connector repo

Run `puxti config` from anywhere inside the workspace to verify it was found:

```
.puxti.yml:  /your/workspace/.puxti.yml  found
  connectors: dbt (your-org/your-dbt-repo · ./dbt), airflow (your-org/your-airflow-repo · ./airflow)
```

Precedence: CLI flags > `.puxti.yml` > environment variables.

---

## Start Neo4j

Puxti uses Neo4j as its Knowledge Graph store. The included `docker-compose.yml` runs Neo4j 5.20 (Community) with the APOC plugin enabled — required for graph traversal queries.

```bash
docker compose up -d
```

Neo4j browser is available at `http://localhost:7474` — useful for inspecting the Knowledge Graph directly. The Bolt endpoint at `bolt://localhost:7687` is what Puxti connects to.

To stop Neo4j without losing data:

```bash
docker compose down
```

To wipe the graph data entirely (e.g. to start fresh):

```bash
docker compose down -v
```

## Verify setup

```bash
puxti config   # show resolved config values and file locations
puxti health   # verify connectivity to all services
```

Expected output from `puxti health`:

```
✓ Neo4j
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
When you classify a correction as a "real change", Puxti prints the `redefine` command to run next but does not execute it automatically. You need to copy-paste and run it yourself.

---

## Telemetry

Puxti v0.6.0 collects no telemetry. A future release will add anonymous, opt-in usage analytics with clear documentation and a one-command opt-out. Until then, the only signal we have is GitHub stars and PyPI download counts.

---

## What's next

- **MCP server for coding agents (Claude Code, Cursor)** — query Puxti's knowledge graph from your AI assistant
- **SQLite backend** — drop the Docker requirement, run Puxti with zero external dependencies

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

There's no formal contributor guide yet. For non-trivial changes, open an
issue first to discuss the approach before writing code.

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
uv run pytest tests/test_cli.py     # run CLI command tests only
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
├── cli.py                  # entry point — all commands
├── models.py               # all data types (Pydantic)
├── settings.py             # config (reads from .env)
├── workspace.py            # .puxti.yml discovery and resolution
├── core/
│   ├── capture.py          # semantic capture — LLM enrichment + Knowledge Graph write
│   ├── corrector.py        # puxti correct — definition correction without propagation
│   ├── graph.py            # Knowledge Graph — Neo4j interface
│   ├── scanner.py          # puxti scan — bootstraps KG from dbt manifest
│   └── redefine.py         # puxti redefine — semantic change propagation
├── graph/
│   ├── models.py           # graph node/edge type definitions
│   └── repository.py       # low-level Neo4j read/write operations
├── connectors/
│   ├── base.py             # connector interface
│   ├── airflow.py          # Airflow connector — DAG parsing + task diff generation
│   ├── dbt.py              # dbt connector — entity extraction + diffs
│   └── github.py           # GitHub connector — PR creation
└── propagation/
    └── engine.py           # propagation engine — orchestrates connectors
```
