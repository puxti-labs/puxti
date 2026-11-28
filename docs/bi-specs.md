# BI specs (draft, `puxti/v1alpha1`)

Datasets, charts and dashboards as short YAML files in git, one asset per file. The format is
defined by the `puxti-bi-core` package in this repository and published as a
[JSON Schema](../packages/puxti-bi-core/src/puxti_bi_core/schemas/v1alpha1.json).

**Status:** the schema is `v1alpha1` and may still change in breaking ways. `puxti scan` reads specs
into the Knowledge Graph, so `puxti impact` shows the charts and dashboards a dbt change reaches
(see [In the Knowledge Graph](#in-the-knowledge-graph)), and `puxti validate` checks specs in CI
(see [Validate in CI](#validate-in-ci)), and `puxti impact --state` lists the specs a dbt change
breaks before it merges (see [Impact of a dbt change in CI](#impact-of-a-dbt-change-in-ci)).
Deploying to a BI tool comes next. The plan is in
[#28](https://github.com/puxti-labs/puxti/issues/28).

## Layout

Specs live in a `bi/` folder inside the dbt project by default. Every `.yml` and `.yaml` file
(any case) under it is a spec; folders starting with `.` are skipped. Subfolders are free-form.

Each asset names the layer below it: a dataset names a dbt model, a chart names a dataset, and a
dashboard names charts. Names are lowercase slugs (`[a-z][a-z0-9_]*`) and must be unique within a
kind; a dataset and a dashboard may share a name.

To get completion and checks in editors that use the YAML language server, start a file with:

```yaml
# yaml-language-server: $schema=https://raw.githubusercontent.com/puxti-labs/puxti/main/packages/puxti-bi-core/src/puxti_bi_core/schemas/v1alpha1.json
```

## Dataset

```yaml
apiVersion: puxti/v1alpha1
kind: dataset
name: orders
model: fct_orders            # dbt model
columns: [ordered_at, region, amount]   # optional; defaults to every column dbt knows
metrics:
  - name: revenue
    agg: sum                 # sum, count, count_distinct, avg, min, max
    column: amount
  - name: order_count
    agg: count               # count is the only agg that needs no column
  - name: active_customers
    dbt_metric: active_customers   # a dbt semantic layer metric; simple metrics only
```

## Chart

Every chart has `dataset` and `type`. Metrics are names defined on the dataset; columns and
`group_by` entries are dbt column names.

| `type` | Fields |
| --- | --- |
| `line`, `bar` | `x: {column, grain}` (required; `grain` is day, week, month, quarter or year), `metrics` (one or more), `group_by` |
| `table` | `columns` for raw rows, or `metrics` with optional `group_by` for an aggregate |
| `big_number` | `metric` (exactly one) |
| `pie` | `metric`, `group_by` (one or more) |

```yaml
apiVersion: puxti/v1alpha1
kind: chart
name: revenue_by_month
title: Revenue by month
dataset: orders
type: line
x: { column: ordered_at, grain: month }
metrics: [revenue]
group_by: [region]
```

## Dashboard

Rows of charts. Widths are in twelfths of the dashboard; charts without a width share what is
left evenly, so `[a, b, c]` gives each a third. Widths in a row add up to 12 at most, and what is
left must divide evenly among the charts without a width: five charts in a row, or a width of 5
next to two charts without one, need explicit widths. `height` is
relative and each BI tool maps it to its own units.

```yaml
apiVersion: puxti/v1alpha1
kind: dashboard
name: sales
title: Sales overview
rows:
  - charts: [total_revenue, order_count]
  - charts:
      - { chart: revenue_by_month, width: 8 }
      - revenue_by_region
    height: 2
```

## In the Knowledge Graph

Add a `bi` connector to `.puxti.yml` (see [Configuration](configuration.md#workspace-config-puxtiyml))
and `puxti scan` reads the spec folder with no LLM calls, so scanning specs alone needs no LLM configuration. Each asset becomes an entity:
`dataset.bi.<name>`, `chart.bi.<name>` and `dashboard.bi.<name>`. Each one depends on what it
reads:

- a dashboard on each of its charts
- a chart on its dataset, and on the dbt columns it reads: its `x` column, `group_by`, table
  `columns`, and the column behind each dataset metric it shows
- a dataset on its dbt model, on the columns it lists or aggregates, and on the dbt metrics it
  names with `dbt_metric`

dbt names resolve against what the dbt connector scanned, so scan both together. `puxti impact
model.<project>.orders` then lists the datasets, charts and dashboards above `orders`, and
`puxti impact model.<project>.orders.amount` lists the ones that read `amount`.

A file that fails to load is skipped with a warning that names the file and line, and so is a
reference to a spec that does not exist. dbt only knows the columns declared in a model's YAML,
so a reference to an undeclared column stays unresolved, and scan lists it in its warning about
references no connector owns.

`puxti capture` lists the BI assets a change reaches under the entities to review by hand: for a
model, every dataset on it and their charts; for a column, the datasets and charts that name it,
directly or through a dataset metric. Dashboards holding a listed chart are listed too. Puxti does
not edit specs yet.

## Validate in CI

`puxti validate` checks the spec folder offline: no LLM calls, no Knowledge Graph, no BI tool.

- every file against the schema
- references between specs: a chart's dataset, the metrics a chart shows, the columns a chart
  reads (when its dataset lists `columns`), a dashboard's charts
- against dbt: a dataset's model exists (a versioned model resolves to its latest version), every
  column a dataset or chart names exists, `sum` and `avg` aggregate a numeric column, an `x` with
  a `grain` is a date or time column, and a `dbt_metric` exists and is a simple metric

dbt facts come from `target/manifest.json` and, when present, `target/catalog.json`. The manifest
only knows the columns a model's YAML declares; the catalog has every column and its type. Without
a catalog, a column nothing declares, or a type nothing records, is a warning rather than an error.
A column the YAML declares but the catalog lacks, such as one added in the same pull request
before the warehouse is rebuilt, is a warning too. Run `dbt docs generate` before `puxti validate` in CI so columns and types are checked in full.

Each finding names the file and line. Errors exit 1; warnings print and exit 0.

```bash
dbt parse && dbt docs generate
puxti validate --dbt-project-dir .
```

```
bi/charts/revenue.yml:8: error: column 'region' is not in dbt model 'orders'
bi/datasets/orders.yml:7: error: `sum` needs a numeric column; 'status' is VARCHAR
✗ 2 error(s), 0 warning(s) in bi
```

The spec folder and dbt project come from the `bi` and `dbt` connectors in `.puxti.yml`, or
`--specs-dir` and `--dbt-project-dir`. `--json` prints the same findings as the MCP
`validate_bi_specs` tool.

## Impact of a dbt change in CI

`puxti impact --state DIR` compares two dbt builds: the base in `DIR` (usually the production
job's `target/`, holding `manifest.json` and, ideally, `catalog.json`) and the current project's
`target/`. It lists the BI specs the change touches, offline: puxti runs no dbt command, no LLM calls and no
Knowledge Graph.

- **broken**: a check from `puxti validate` that passes against the base and fails against the
  current build: a removed model, column or dbt metric, or a column whose new type no longer fits
  (`sum` on text, a grain on a non-date). Charts on a broken dataset and dashboards holding a
  broken chart are broken too.
- **affected**: a dataset whose model changed (removed, SQL changed, a column removed or retyped,
  or its `depends_on` changed, as when an unpinned `ref()` follows a new latest version), sits
  downstream of a changed model, seed or snapshot, or now resolves to a different node (a new
  latest version), and the charts and dashboards built on it, while every reference still works.

A spec that was already broken on the base is not reported; `puxti validate` covers it. Broken
assets exit 1; affected assets and warnings exit 0.

Columns are compared from the same source on both sides: the catalog when both builds have an
entry for the model, else the columns its YAML declares. When neither is available for an
affected dataset's model (changed itself or downstream of a change), a removed column cannot be
seen, so the dataset gets a warning saying so instead of passing silently. For complete column
and type changes, give both builds a `catalog.json`.

dbt checksums a seed larger than 1 MiB by its file path, so an edit to its rows leaves the
checksum unchanged. Such a seed is always reported as changed, with a note that data edits could
not be ruled out, and everything downstream of it is affected.

`dbt docs generate` reads the catalog from the warehouse, so it describes the tables that exist,
not the SQL in the pull request. Build the changed models into the CI schema first; otherwise
the PR's catalog shows the old tables, and a column the PR's SQL removes looks unchanged. Build
into a fresh or empty CI schema: an incremental model built into a table that already exists
keeps the columns the PR's SQL removed (with `on_schema_change: append_new_columns`, for
example), and the catalog then still lists them. If the CI schema has to persist, add
`--full-refresh` so incremental models are rebuilt.

Keep the production run's `target/manifest.json` and `catalog.json` where CI can fetch them (an
artifact of the deploy workflow, a bucket, or your dbt platform's artifacts API), then in the
pull request job:

```bash
dbt build --select state:modified+ --defer --state prod-target   # build what the PR changed
dbt docs generate                                                 # the current build's catalog
puxti impact --state prod-target --dbt-project-dir .
```

Without a build (`dbt parse` alone), there is no current catalog: models, dbt metrics and the
columns declared in YAML are still compared, and an affected dataset whose model's columns
nothing records gets a warning. `dbt parse` does not touch `target/catalog.json`, and puxti does
not check how old that file is: one left from an earlier build is read as the current catalog,
replacing the YAML comparison and hiding that warning. Delete `target/catalog.json`, or parse
into a clean target directory, before running `dbt parse`.

```
Changed dbt models (base: prod-target)
  model.shop.orders: column 'amount' removed
 Asset             Kind       Status  Reason
 orders            dataset    broken  column 'amount' is not in dbt model 'orders'
                                      dbt model model.shop.orders: column 'amount' removed
 revenue_by_month  chart      broken  dataset 'orders' is broken
 sales             dashboard  broken  chart 'revenue_by_month' is broken
bi/datasets/orders.yml:6: error: column 'amount' is not in dbt model 'orders'
✗ 3 broken, 0 affected, 0 warning(s)
```

Lineage between models comes from `depends_on` in both manifests, so the dependents of a removed
model are found too. Downstream models are followed at model level: a dataset downstream of a
changed model is affected, without the column detail. `--json` prints the changed models, the
assets with their reasons, and the findings.

## Common fields and the escape hatch

Every spec takes `apiVersion`, `kind`, `name`, and optional `title` and `description`. `id` keeps
an existing asset ID when an asset is adopted from a live BI tool; without it the ID is derived
from the kind and name. Unknown fields are errors.

Specs are plain YAML with three restrictions: a key may appear only once in a mapping, aliases
(`*name`) are not allowed, and only `true` and `false` are booleans, so `title: yes` is text.

Charts and dashboards take a `superset:` block of raw Superset fields, merged into the compiled
Superset asset last. It covers chart types and options the spec does not have yet. Other BI tools
ignore it, and each tool gets its own block when its library exists.

## How fields map to each BI tool

Superset is the first deploy target. QuickSight is a possible second, so the spec avoids fields
only one tool can express. This table is the design check for that; the QuickSight column is
from documentation memory and is unverified until a compile spike runs against a real account.

| Spec | Superset | QuickSight (unverified) |
| --- | --- | --- |
| dataset `model` | Dataset on the model's relation | DataSet with a relational table on a data source; direct query by default |
| dataset `columns` | Dataset columns | Input columns, each with a type, so types must be known |
| metric `agg` + `column` | Dataset metric with a SQL expression, e.g. `SUM(amount)` | Aggregation function on the field in the visual's field wells |
| metric `dbt_metric` | SQL expression generated from the simple metric | Same, as a calculated field |
| chart `line` | `echarts_timeseries_line` | `LineChartVisual` |
| chart `bar` | `echarts_timeseries_bar` | `BarChartVisual` |
| chart `table` | `table`, raw or aggregate query mode | `TableVisual`, unaggregated or aggregated field wells |
| chart `big_number` | `big_number_total` | `KPIVisual` |
| chart `pie` | `pie` | `PieChartVisual` |
| `x.column` + `x.grain` | `x_axis` + `time_grain_sqla` | Category field with a date granularity |
| `group_by` | `groupby` | Colors field well (line, bar), category (pie), group by (table) |
| dashboard row width | `width` on the 12-column grid, unchanged | Column span on the 36-column grid, width × 3 |
| dashboard row `height` | Grid height units | Row span |
| `id`, or derived | Asset UUID | Asset ID, with a per-target prefix since environments often share an AWS account |
| `superset:` block | Merged into the compiled asset | Ignored |

Superset also needs a `query_context` for every chart, which the compiler builds from the same
fields. QuickSight also needs permissions on every asset and a published dashboard version; those
come from target configuration, not the spec.
