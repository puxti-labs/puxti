# Metrics

Puxti handles metrics in two ways: it imports the MetricFlow metrics a dbt project already defines, and it lets you define a metric before any model implements it.

## MetricFlow metrics from dbt

When the dbt project defines metrics (dbt 1.6+ writes them to `target/manifest.json` alongside `semantic_manifest.json`), scan imports each one as a metric entity with the ID `metric.<project>.<name>`. Lineage goes from a simple or cumulative metric to the model behind its semantic model, and from a derived or ratio metric to its input metrics. Each metric's definition comes straight from its YAML, with no LLM call: the description, then what it computes (for example `Derived metric: lifetime_spend_pretax / count_lifetime_orders`). Scan shows these definitions and asks for one confirmation before writing them (one per metric with `--interactive`, where you can also edit or skip each; an edited definition counts as yours). On a re-scan, a metric whose YAML changed gets a new definition version, and a definition someone wrote with `define`, `redefine` or `correct` is never overwritten; scan lists those metrics instead. A project without metrics scans exactly as before.

## Define a metric before it exists

Sometimes the definition comes before the model. You can register a metric's
meaning now and let a model catch up later. `puxti define` writes a *proposed*
metric into the Knowledge Graph. No LLM call, no dbt manifest, no PR: the
definition is intent, not code.

```bash
puxti define --name net_revenue_retention --description "Revenue retained from existing customers, net of churn and contraction." --derived-from model.jaffle_shop.subscriptions
```

A proposed metric is never reported as a fact by the [MCP tools](mcp.md) until
it is bound to a real entity. `puxti describe` lists proposed metrics in their own
"Proposed (unbound)" section.

`puxti define` refuses an ID that already belongs to a bound entity, such as a dbt metric scan imported; change that one's meaning with `puxti redefine` or fix it with `puxti correct`.

Two ways to bind a proposed metric to the model that implements it:

- `puxti scan` reconciles automatically. A metric defined with `--project` set to
  the dbt project name has the same ID as the dbt metric that implements it, so
  scan turns it into a bound metric in place and keeps your definition. Otherwise,
  when a scanned model, view or dbt metric has the same name as a proposed metric,
  scan offers the bind and you confirm it. It never binds silently, and it only
  matches on an exact (case-insensitive) name.
- `puxti bind` binds explicitly, for the cases scan cannot match by name:

```bash
puxti bind --proposed metric.proposed.net_revenue_retention --to model.jaffle_shop.fct_nrr
```

Binding carries the metric's definition onto the real entity as a new version and
moves its semantic edges across.

### `define` options

| Flag | Required | Description |
|------|----------|-------------|
| `--name` / `-n` | Yes | Metric name (e.g. `net_revenue_retention`) |
| `--description` / `-d` | Yes | What the metric means |
| `--project` / `-p` | No | Project to namespace the metric under |
| `--derived-from` | No | Existing entity ID this metric is derived from; anchors it into impact analysis |

### `bind` options

| Flag | Required | Description |
|------|----------|-------------|
| `--proposed` | Yes | Proposed metric entity ID (from `puxti define` / `puxti describe`) |
| `--to` | Yes | Real entity ID that implements the metric |

## Limitations

### Proposed metrics are inert until reconciled

A metric created with `puxti define` is a definition with no backing model. It holds no value and has no structural dependents until it is bound. `puxti scan` only auto-suggests a bind when a scanned model, view or dbt metric has the exact same name. A dbt metric wins over a model with the same name (jaffle-shop has both an `orders` metric and an `orders` model), and a name that two metrics or two models share is skipped; for anything else, bind it explicitly with `puxti bind`.

### Consumers are one hop

`puxti impact` and `impact_of_change` follow structural dependents all the way downstream, so a derived metric built on a simple metric built on `orders` shows up in the impact of `orders`, two hops away. `consumers` lists direct dependents only. A column rename also leaves the semantic model YAML that references the column untouched.
