# Known limitations

## Re-scanning after dbt project changes

`puxti scan` is safe to re-run at any time — it upserts entities and definitions, so new models and columns will be added and existing ones updated. However, there is no prompt or workflow that reminds you to re-scan when your dbt project changes. Run `puxti scan` again whenever you add models, rename columns in your dbt layer, or change descriptions in yml files.

## Column renames and Jinja

The rename is a text replacement and never edits Jinja (`{{ }}`, `{% %}`, `{# #}`). If the old column name appears inside a Jinja expression, for example a macro argument such as `dbt_utils.generate_surrogate_key(['order_date'])`, Puxti skips that model and lists it as unverified in the PR so you can update it by hand.

## Column renames and qualified references

Qualified references such as `o.order_date` or `{{ ref('orders') }}.order_date` are never rewritten, because the text alone does not say which table the qualifier points to. Any model or view with a qualified reference to the renamed column is skipped entirely and listed as unverified, so the PR never carries a half-renamed model. Relation names such as `from raw.order_date` do not count as column references.

## Reviewing skipped changes

`puxti capture` lists each skipped entity with the connector's reason. The same
reason appears in the generated PR: for example, a Jinja reference, a qualified
column reference, a missing column, or a dependency that cannot be verified as
a direct reference to the source. Airflow and Prisma also report missing source
files, unavailable task docstrings, or missing schema models as applicable.
Review these entities manually before merging; generated diffs do not resolve
the skipped cases. A connector with no diffs opens no PR, but its skipped
entities still appear in the CLI.

Connector integrations should return `UnverifiedEntity` records (with `entity_id`, `reason`,
and optional `detail` keyword arguments) from `generate_changes` and consume
`PropagationResult.unverified_entities`. `reason` is an `UnverifiedReason` enum;
`detail` is optional. Legacy ID-only connector results and the
`unverified_entity_ids` constructor argument are accepted with an `unspecified`
reason. The read-only `unverified_entity_ids` property remains available; newly
serialized results use `unverified_entities` to retain reasons and details.

## Finding entity IDs

Commands like `capture`, `redefine`, and `correct` require a full entity ID (e.g. `model.jaffle_shop.orders.order_date`). If you are not sure of an entity's ID, run `puxti describe` first — it lists all entities with their full IDs.

## The `correct` → `redefine` handoff

When you classify a correction as a "real change", Puxti prints the `redefine` command to run next and asks whether to run it immediately. Press Enter or anything other than `y` to keep the copy-paste workflow.

## Proposed metrics are inert until reconciled

A metric created with `puxti define` is a definition with no backing model. It holds no value and has no structural dependents until it is bound. `puxti scan` only auto-suggests a bind when a scanned model, view or dbt metric has the exact same name. A dbt metric wins over a model with the same name (jaffle-shop has both an `orders` metric and an `orders` model), and a name that two metrics or two models share is skipped; for anything else, bind it explicitly with `puxti bind`.

## Metric impact is one hop

`impact_of_change` and `consumers` list direct dependents. A simple metric built on `orders` shows up in the impact of `orders`; a derived metric built on that simple metric does not, until you run impact on the simple metric itself. A column rename also leaves the semantic model YAML that references the column untouched.
