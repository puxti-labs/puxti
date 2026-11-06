# Known limitations

## Re-scanning after dbt project changes

`puxti scan` is safe to re-run at any time — it upserts entities and definitions, so new models and columns will be added and existing ones updated. However, there is no prompt or workflow that reminds you to re-scan when your dbt project changes. Run `puxti scan` again whenever you add models, rename columns in your dbt layer, or change descriptions in yml files.

## Column renames and Jinja

The rename is a text replacement and never edits Jinja (`{{ }}`, `{% %}`, `{# #}`). If the old column name appears inside a Jinja expression, for example a macro argument such as `dbt_utils.generate_surrogate_key(['order_date'])`, Puxti skips that model and lists it as unverified in the PR so you can update it by hand.

## Column renames and qualified references

Qualified references such as `o.order_date` or `{{ ref('orders') }}.order_date` are never rewritten, because the text alone does not say which table the qualifier points to. Any model or view with a qualified reference to the renamed column is skipped entirely and listed as unverified, so the PR never carries a half-renamed model. Relation names such as `from raw.order_date` do not count as column references.

The SQL views connector renames a column across the whole `.sql` file. A file that also defines a view the change does not affect is skipped and listed as unverified, because the rewrite would rename the column in that view too. Keep one view per file to get automatic renames.

## Reviewing skipped changes

`puxti capture` lists each skipped entity with the connector's reason. The same
reason appears in the generated PR: for example, a Jinja reference, a qualified
column reference, a missing column, a dependency that cannot be verified as
a direct reference to the source, a dependent that already has the new name, or
a rewrite that no longer parses. Airflow and Prisma also report missing source
files, unavailable task docstrings, or missing schema models as applicable.
Review these entities manually before merging; generated diffs do not resolve
the skipped cases. A connector with no diffs opens no PR, but its skipped
entities still appear in the CLI.

Connector integrations should return `UnverifiedEntity` records (with `entity_id`, `reason`,
and optional `detail` and `is_source` keyword arguments) from `generate_changes` and consume
`PropagationResult.unverified_entities`. `reason` is an `UnverifiedReason` enum;
`detail` is optional. `is_source` is `True` for the entity that owns the changed
column (the source model or view), `False` for a dependent, and `None` (the
default) when the connector does not say. The dbt and SQL views connectors set
it; Airflow and Prisma leave it unset. The PR description shows the alias
instructions for a source entity with a qualified reference, and falls back to
matching on the model name when `is_source` is `None`. Legacy ID-only connector results and the
`unverified_entity_ids` constructor argument are accepted with an `unspecified`
reason. The read-only `unverified_entity_ids` property remains available; newly
serialized results use `unverified_entities` to retain reasons and details.

## Column renames and schema.yml

The rename updates the YAML that dbt validates: the column's entry in the model's `schema.yml` (found through the manifest's `patch_path`), the model's constraint column lists, and `relationships` tests (`field:`) that point at a renamed model. Edits change only the renamed names, so comments and formatting stay as you wrote them. Other mentions of the column in YAML are not rewritten, such as model-level tests that name it through `column_name:`, arguments of package tests (`dbt_utils.unique_combination_of_columns`), semantic model `dimensions` and `measures`, column entries under a versioned model's `versions:`, free-text descriptions and `docs` blocks. Review those in the PR.

The parse check renders Jinja approximately (both branches of an `{% if %}` are kept), so it flags a model only when its SQL parsed before the rename and does not after.

The new-name check ignores case, as unquoted SQL identifiers do, so a rename that only changes case (`order_date` → `ORDER_DATE`) is refused.

## Column renames reach direct dependents only

`capture` rewrites the source model and the models that `ref()` it directly. Deeper models are listed as unverified, since without column lineage puxti cannot tell where their copy of the column comes from. Merge the first PR, then run `capture` again on the intermediate model's column to reach the next level. See [Commands](commands.md#renames-reach-direct-dependents-only).

## Finding entity IDs

Commands like `capture`, `redefine`, and `correct` require a full entity ID (e.g. `model.jaffle_shop.orders.order_date`). If you are not sure of an entity's ID, run `puxti describe` first — it lists all entities with their full IDs.

## The `correct` → `redefine` handoff

When you classify a correction as a "real change", Puxti prints the `redefine` command to run next and asks whether to run it immediately. Press Enter or anything other than `y` to keep the copy-paste workflow.

## Proposed metrics are inert until reconciled

A metric created with `puxti define` is a definition with no backing model. It holds no value and has no structural dependents until it is bound. `puxti scan` only auto-suggests a bind when a scanned model, view or dbt metric has the exact same name. A dbt metric wins over a model with the same name (jaffle-shop has both an `orders` metric and an `orders` model), and a name that two metrics or two models share is skipped; for anything else, bind it explicitly with `puxti bind`.

## Metric impact is one hop

`impact_of_change` and `consumers` list direct dependents. A simple metric built on `orders` shows up in the impact of `orders`; a derived metric built on that simple metric does not, until you run impact on the simple metric itself. A column rename also leaves the semantic model YAML that references the column untouched.
