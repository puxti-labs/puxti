# Commands

A walk through every puxti command in the order you usually need them. Run `puxti <command> --help` for the full option list. Setup comes first: see [Configuration](configuration.md).

## Step 1 — Bootstrap the Knowledge Graph

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

An LLM edge proposal with an invalid relationship type or a missing description, or one that is not an object, is skipped with a warning. The remaining valid edges are still shown for confirmation before they are written.

When the dbt project defines MetricFlow metrics, scan imports them too. See [Metrics](metrics.md#metricflow-metrics-from-dbt).

---

## Step 1b — Link upstream producers (optional, but recommended)

If your dbt sources are loaded by Airflow DAGs, declare the cross-system link once:

```bash
puxti link --from task.airflow.salesforce_sync.extract_opportunities --to source.clariva.raw_opportunities --description "Extracts Salesforce opportunities. amount is a roll-up of OpportunityLineItem.TotalPrice post Q1 2024 migration."
```

Puxti creates a `FEEDS` edge in the Knowledge Graph connecting the Airflow task to the dbt source. When you later run `puxti capture` on a change to `raw_opportunities.amount`, the generated PR will include full cross-system context.

### `link` options

| Flag | Required | Description |
|------|----------|-------------|
| `--from` | Yes | Entity producing data (`task.airflow.<dag>.<task>` or `model.<project>.<name>`) |
| `--to` | Yes | Entity receiving data (`source.<project>.<table>` or `model.<project>.<name>`) |
| `--description` / `-d` | Yes | Semantic description of the relationship — what data flows and what it means |

---

## Step 1c — Query impact before making a change (optional)

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

### `impact` options

| Flag | Required | Description |
|------|----------|-------------|
| `<entity>` | Yes | Entity ID to analyze (positional argument) |
| `--change-type` | No | One of `rename`, `redefine`, `drop`, `type_change` — scopes the risk annotation |
| `--json` | No | Output as JSON (same shape as the MCP `impact_of_change` tool) |

---

## Define and bind a metric before it exists (optional)

See [Metrics](metrics.md#define-a-metric-before-it-exists) for `puxti define` and `puxti bind`.

---

## Step 2a — Capture a column rename

```bash
puxti capture --entity "model.jaffle_shop.orders.order_date" --before "order_date" --after "recorded_date" --description "Renamed to clarify this is the date the order was recorded in the system, not the transaction date." --repo "your-org/your-dbt-repo"
```

Puxti will:

1. Refuse the rename if the model already has a `recorded_date` column, before any LLM call
2. Call the LLM to enrich your description and reason about affected entities
3. Scan your dbt models for references to `order_date`
4. Generate diffs replacing all occurrences with `recorded_date`, plus the matching `schema.yml` changes: the column's entry (with its tests, description, meta and contract `data_type`), model constraint column lists, and `relationships` tests on other models that point at the renamed column
5. Show unified diffs and any models needing manual review, then ask for confirmation
6. Open PRs in the configured repositories only after an explicit `y`

Declining or leaving the PR prompt blank creates no branches or PRs. This is
separate from the earlier Knowledge Graph prompt: cancelling PR creation does
not undo enrichment you already chose to save. `--dry-run` still estimates cost
without generating diffs or opening PRs.

If a model the rename would rewrite already has a `recorded_date` column, nothing is generated and the command exits with an error. A model whose rewritten SQL no longer parses is listed as unverified and left unchanged.

### `capture` options

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

## Step 2b — Redefine what an entity means

Use this when the *meaning* of an entity changes — not a rename, a conceptual shift (e.g. a revenue metric now excludes refunds).

```bash
puxti redefine --entity "model.jaffle_shop.orders.gross_revenue" --description "gross_revenue now excludes refunds — only settled transactions count." --repo "your-org/your-dbt-repo"
```

Puxti will:

1. Traverse the semantic graph to find downstream entities whose meaning is now affected
2. Walk the structural ancestor chain upstream for passthrough changes
3. Generate SQL diffs with depth-aware confidence annotations
4. Open a PR with both upstream passthrough diffs and downstream semantic diffs

### `redefine` options

| Flag | Required | Description |
|------|----------|-------------|
| `--entity` / `-e` | Yes | Entity ID whose meaning is changing |
| `--description` / `-d` | Yes | What the entity now means |
| `--repo` | No | GitHub repo for the PR (`owner/repo`) — required unless `--dry-run` or set in `.puxti.yml` |
| `--base-branch` | No | Base branch for the PR (default: `main`, or from `.puxti.yml`) |
| `--dbt-project-dir` | No | Path to dbt project root (overrides `.puxti.yml` and `DBT_PROJECT_DIR`) |
| `--dry-run` | No | Estimate token count and cost without calling the LLM or opening a PR |

---

## Step 2c — Correct an inaccurate definition

Use this when Puxti inferred a wrong definition during scan, or when you want to refine one without triggering code propagation.

```bash
puxti correct --entity "model.jaffle_shop.orders"
```

### `correct` options

| Flag | Required | Description |
|------|----------|-------------|
| `--entity` / `-e` | Yes | Entity ID to correct |
| `--project` / `-p` | No | Validate the entity belongs to this project before proceeding |

---

## Step 3 — Inspect the Knowledge Graph

```bash
# Full overview — all entities grouped by project, all semantic edges
puxti describe

# Filter overview to a single project
puxti describe --project jaffle_shop

# Single entity — full definition, incoming and outgoing edges
puxti describe --entity "model.jaffle_shop.orders"
```

---

## Step 3b — Visualize the graph

Render the Knowledge Graph as a self-contained, interactive HTML page:

```bash
puxti graph -o kg.html          # write the page
puxti graph -o kg.html --open   # write it and open in your browser
puxti graph --project jaffle_shop -o kg.html   # limit to one project
```

The page has an interactive force-directed view of your entities and their lineage
and semantic relationships. Click a node to see its definition, relationships, and
definition history. It is read-only, has no external dependencies (open it in any
browser, no server needed), and proposed metrics from `puxti define` are shown with a
distinct style so intent-vs-implemented is visible at a glance.

### `graph` options

| Flag | Required | Description |
|------|----------|-------------|
| `--output` / `-o` | No | Path to write the HTML file to (default: `puxti-graph.html`) |
| `--project` / `-p` | No | Only include entities from this project |
| `--open` | No | Open the generated file in your browser |

---

## Step 4 — Purge project data

```bash
# Remove a single project
puxti purge --project jaffle_shop

# Wipe the entire Knowledge Graph
puxti purge --all
```

---

## Use the graph from a coding agent

`puxti mcp serve` and `puxti mcp init` are covered in [MCP](mcp.md).
