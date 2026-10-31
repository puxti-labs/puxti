import json
from pathlib import Path

import pytest

from puxti.connectors.base import BaseConnector
from puxti.connectors.dbt import (
    ColumnRenameCollisionError,
    DbtConnector,
    _rename_column_in_sql,
)
from puxti.connectors.sql_utils import mentions_in_jinja, rename_column_fully
from puxti.models import ChangeType, EdgeType, EntityStatus, EntityType, SemanticChangeEvent
from puxti.propagation.engine import PropagationEngine

# ── Fixtures ──────────────────────────────────────────────────────────────────

MANIFEST = {
    "metadata": {"dbt_schema_version": "v9", "project_name": "jaffle_shop"},
    "nodes": {
        "model.jaffle_shop.orders": {
            "resource_type": "model",
            "name": "orders",
            "original_file_path": "models/orders.sql",
            "schema": "public",
            "database": "dev",
            "description": "One row per order",
            "tags": [],
            "columns": {
                "order_id": {"name": "order_id", "description": "PK", "data_type": "integer"},
                "order_date": {
                    "name": "order_date", "description": "Date placed", "data_type": "date",
                },
                "amount": {"name": "amount", "description": "Total amount", "data_type": "numeric"},
            },
            "depends_on": {
                "nodes": ["model.jaffle_shop.stg_orders", "source.jaffle_shop.raw.orders"],
            },
        },
        "model.jaffle_shop.stg_orders": {
            "resource_type": "model",
            "name": "stg_orders",
            "original_file_path": "models/staging/stg_orders.sql",
            "schema": "public",
            "database": "dev",
            "description": "Staged orders from raw",
            "tags": [],
            "columns": {
                "order_id": {"name": "order_id", "description": "PK", "data_type": "integer"},
                "order_date": {"name": "order_date", "description": "", "data_type": "date"},
            },
            "depends_on": {"nodes": ["source.jaffle_shop.raw.orders"]},
        },
        "model.jaffle_shop.revenue": {
            "resource_type": "model",
            "name": "revenue",
            "original_file_path": "models/revenue.sql",
            "schema": "public",
            "database": "dev",
            "description": "Revenue aggregated from orders",
            "tags": [],
            "columns": {
                "month": {"name": "month", "description": "", "data_type": "date"},
                "total_revenue": {
                    "name": "total_revenue", "description": "", "data_type": "numeric",
                },
            },
            "depends_on": {"nodes": ["model.jaffle_shop.orders"]},
        },
    },
    "sources": {
        "source.jaffle_shop.raw.orders": {
            "resource_type": "source",
            "name": "orders",
            "source_name": "raw",
            "schema": "raw",
            "database": "dev",
            "description": "Raw orders from application DB",
        }
    },
}

ORDERS_SQL = """
select
    order_id,
    order_date,
    amount,
    customer_id
from {{ ref('stg_orders') }}
""".strip()

STG_ORDERS_SQL = """
select
    id as order_id,
    date as order_date,
    customer_id
from {{ source('raw', 'orders') }}
""".strip()

REVENUE_SQL = """
select
    date_trunc('month', order_date) as month,
    sum(amount) as total_revenue
from {{ ref('orders') }}
group by 1
""".strip()


@pytest.fixture
def dbt_project(tmp_path: Path) -> Path:
    """Creates a minimal dbt project layout with manifest and SQL files."""
    # manifest
    target = tmp_path / "target"
    target.mkdir()
    (target / "manifest.json").write_text(json.dumps(MANIFEST))

    # SQL files
    models = tmp_path / "models"
    models.mkdir()
    staging = models / "staging"
    staging.mkdir()

    (models / "orders.sql").write_text(ORDERS_SQL)
    (staging / "stg_orders.sql").write_text(STG_ORDERS_SQL)
    (models / "revenue.sql").write_text(REVENUE_SQL)

    return tmp_path


@pytest.fixture
def connector(dbt_project: Path) -> DbtConnector:
    return DbtConnector(config={"project_dir": str(dbt_project)})


# ── health_check ──────────────────────────────────────────────────────────────

async def test_health_check_passes_when_manifest_exists(connector):
    assert await connector.health_check() is True


async def test_health_check_fails_when_manifest_missing(tmp_path):
    conn = DbtConnector(config={"project_dir": str(tmp_path)})
    assert await conn.health_check() is False


# ── extract_entities ──────────────────────────────────────────────────────────

async def test_extracts_model_entities(connector):
    entities = await connector.extract_entities()
    model_names = {e.name for e in entities if e.type == EntityType.MODEL}
    assert model_names == {"orders", "stg_orders", "revenue"}


async def test_extracts_column_entities(connector):
    entities = await connector.extract_entities()
    col_names = {e.name for e in entities if e.type == EntityType.COLUMN}
    assert "order_id" in col_names
    assert "order_date" in col_names
    assert "amount" in col_names


async def test_extracts_source_entities(connector):
    entities = await connector.extract_entities()
    source_entities = [e for e in entities if e.type == EntityType.TABLE]
    assert len(source_entities) == 1
    assert source_entities[0].name == "orders"


async def test_project_name_set_on_all_entities(connector):
    """project field on every entity should come from manifest metadata.project_name."""
    entities = await connector.extract_entities()
    assert entities, "expected at least one entity"
    for entity in entities:
        assert entity.project == "jaffle_shop", (
            f"Entity {entity.id} has project={entity.project!r}, expected 'jaffle_shop'"
        )


async def test_project_name_empty_when_missing_from_manifest(tmp_path):
    """If manifest has no project_name, entities should have project=''."""
    manifest_without_project = {
        "metadata": {"dbt_schema_version": "v9"},
        "nodes": {
            "model.demo.orders": {
                "resource_type": "model",
                "name": "orders",
                "original_file_path": "models/orders.sql",
                "schema": "public",
                "database": "dev",
                "description": "",
                "tags": [],
                "columns": {},
                "depends_on": {"nodes": []},
            }
        },
        "sources": {},
    }
    target = tmp_path / "target"
    target.mkdir()
    (target / "manifest.json").write_text(json.dumps(manifest_without_project))
    conn = DbtConnector(config={"project_dir": str(tmp_path)})
    entities = await conn.extract_entities()
    for entity in entities:
        assert entity.project == ""


async def test_model_entity_has_correct_connector(connector):
    entities = await connector.extract_entities()
    for e in entities:
        assert e.source_connector == "dbt"


# ── extract_lineage ───────────────────────────────────────────────────────────

async def test_extracts_model_dependencies(connector):
    edges = await connector.extract_lineage()
    from_ids = {e.from_entity_id for e in edges}
    assert "model.jaffle_shop.orders" in from_ids
    assert "model.jaffle_shop.revenue" in from_ids


async def test_revenue_depends_on_orders(connector):
    edges = await connector.extract_lineage()
    revenue_deps = {
        e.to_entity_id for e in edges
        if e.from_entity_id == "model.jaffle_shop.revenue"
    }
    assert "model.jaffle_shop.orders" in revenue_deps


# ── MetricFlow metrics ────────────────────────────────────────────────────────

def _metric(name: str, metric_type: str, deps: list[str], description: str = "",
            type_params: dict | None = None, filter_: dict | None = None) -> dict:
    return {
        "name": name,
        "description": description,
        "label": name.replace("_", " ").title(),
        "type": metric_type,
        "type_params": type_params or {},
        "filter": filter_,
        "depends_on": {"macros": [], "nodes": deps},
    }


METRICS_MANIFEST = {
    **MANIFEST,
    "semantic_models": {
        "semantic_model.jaffle_shop.orders": {
            "name": "orders",
            "depends_on": {"nodes": ["model.jaffle_shop.orders"]},
            "measures": [
                {"name": "order_total", "agg": "sum", "expr": None},
                {"name": "order_count", "agg": "sum", "expr": "1"},
            ],
        },
        "semantic_model.jaffle_shop.ghost": {"name": "ghost", "depends_on": {"nodes": []},
                                             "measures": []},
    },
    "metrics": {
        "metric.jaffle_shop.order_total": _metric(
            "order_total", "simple", ["semantic_model.jaffle_shop.orders"],
            description="Sum of order totals.",
            type_params={"measure": {"name": "order_total"}},
        ),
        "metric.jaffle_shop.large_orders": _metric(
            "large_orders", "simple", ["semantic_model.jaffle_shop.orders"],
            description="Orders over 20.",
            type_params={"measure": {"name": "order_count"}},
            filter_={"where_filters": [
                {"where_sql_template": "{{ Dimension('order_id__order_total') }} >= 20\n"},
            ]},
        ),
        "metric.jaffle_shop.average_order_value": _metric(
            "average_order_value", "derived",
            ["metric.jaffle_shop.order_total", "metric.jaffle_shop.large_orders"],
            description="Total over count.",
            type_params={
                "expr": "order_total / large_orders",
                "metrics": [{"name": "order_total"}, {"name": "large_orders"}],
            },
        ),
        "metric.jaffle_shop.order_total_growth": _metric(
            "order_total_growth", "derived", ["metric.jaffle_shop.order_total"],
            type_params={
                "expr": "(current - prev) / prev",
                "metrics": [
                    {"name": "order_total", "alias": "current"},
                    {"name": "order_total", "alias": "prev",
                     "offset_window": {"count": 1, "granularity": "month"}},
                ],
            },
        ),
        "metric.jaffle_shop.large_order_pct": _metric(
            "large_order_pct", "ratio",
            ["metric.jaffle_shop.large_orders", "metric.jaffle_shop.order_total"],
            type_params={"numerator": {"name": "large_orders"},
                         "denominator": {"name": "order_total"}},
        ),
        "metric.jaffle_shop.rolling_order_total": _metric(
            "rolling_order_total", "cumulative", ["semantic_model.jaffle_shop.orders"],
            type_params={
                "measure": {"name": "order_total"},
                "cumulative_type_params": {"window": "7 days", "grain_to_date": None},
            },
        ),
        "metric.jaffle_shop.ghost_metric": _metric(
            "ghost_metric", "conversion", ["semantic_model.jaffle_shop.ghost"],
        ),
    },
}


@pytest.fixture
def metrics_connector(tmp_path: Path) -> DbtConnector:
    target = tmp_path / "target"
    target.mkdir()
    (target / "manifest.json").write_text(json.dumps(METRICS_MANIFEST))
    return DbtConnector(config={"project_dir": str(tmp_path)})


async def test_extracts_metric_entities(metrics_connector):
    entities = await metrics_connector.extract_entities()
    metrics = {e.id: e for e in entities if e.type == EntityType.METRIC}
    assert set(metrics) == set(METRICS_MANIFEST["metrics"])
    aov = metrics["metric.jaffle_shop.average_order_value"]
    assert aov.name == "average_order_value"
    assert aov.project == "jaffle_shop"
    assert aov.source_connector == "dbt"
    assert aov.status == EntityStatus.BOUND
    assert aov.metadata["metric_type"] == "derived"


async def test_metric_lineage_resolves_semantic_models_to_models(metrics_connector):
    edges = await metrics_connector.extract_lineage()
    deps: dict[str, list[str]] = {}
    for e in edges:
        if e.from_entity_id.startswith("metric."):
            deps.setdefault(e.from_entity_id, []).append(e.to_entity_id)
    assert deps["metric.jaffle_shop.order_total"] == ["model.jaffle_shop.orders"]
    assert sorted(deps["metric.jaffle_shop.average_order_value"]) == [
        "metric.jaffle_shop.large_orders", "metric.jaffle_shop.order_total",
    ]
    # Two inputs on the same metric collapse to one edge.
    assert deps["metric.jaffle_shop.order_total_growth"] == ["metric.jaffle_shop.order_total"]
    # A semantic model with no backing model yields no edge, not a dangling one.
    assert "metric.jaffle_shop.ghost_metric" not in deps
    assert all(e.type == EdgeType.DEPENDS_ON and e.connector == "dbt" for e in edges)


async def test_manifest_without_metrics_scans_as_before(connector):
    entities = await connector.extract_entities()
    edges = await connector.extract_lineage()
    assert not [e for e in entities if e.type == EntityType.METRIC]
    assert not [e for e in edges if e.from_entity_id.startswith("metric.")]
    assert connector.get_seed_definitions() == {}


def test_seed_definitions_state_each_metric_type(metrics_connector):
    defs = metrics_connector.get_seed_definitions()
    assert defs["metric.jaffle_shop.order_total"] == (
        "Sum of order totals.\n\n"
        "Simple metric: sum of order_total (measure order_total in semantic model orders)"
    )
    assert defs["metric.jaffle_shop.large_orders"] == (
        "Orders over 20.\n\n"
        "Simple metric: sum of 1 (measure order_count in semantic model orders)\n"
        "Filter: {{ Dimension('order_id__order_total') }} >= 20"
    )
    assert defs["metric.jaffle_shop.average_order_value"] == (
        "Total over count.\n\n"
        "Derived metric: order_total / large_orders\n"
        "Inputs: order_total, large_orders"
    )
    # No description: the formula is the whole definition.
    assert defs["metric.jaffle_shop.order_total_growth"] == (
        "Derived metric: (current - prev) / prev\n"
        "Inputs: order_total (as current), order_total offset by 1 month (as prev)"
    )
    assert defs["metric.jaffle_shop.large_order_pct"] == (
        "Ratio metric: large_orders / order_total"
    )
    assert defs["metric.jaffle_shop.rolling_order_total"] == (
        "Cumulative metric: sum of order_total (measure order_total in semantic model "
        "orders), over a 7 days window"
    )
    assert defs["metric.jaffle_shop.ghost_metric"] == "Conversion metric."


# ── generate_changes — column rename ─────────────────────────────────────────

async def test_column_rename_generates_diffs(connector):
    event = SemanticChangeEvent(
        change_event_id="evt-001",
        entity_id="model.jaffle_shop.orders.order_date",
        change_type=ChangeType.STRUCTURAL,
        semantic_context=(
            "order_date renamed to recorded_date to clarify it is the date the order was "
            "recorded, not the transaction date"
        ),
        affected_entity_ids=[
            "model.jaffle_shop.orders",
            "model.jaffle_shop.revenue",
            "model.jaffle_shop.stg_orders",
        ],
        reasoning="Column rename — deterministic propagation across all referencing models",
        change={"before": {"name": "order_date"}, "after": {"name": "recorded_date"}},
    )

    diffs, _ = await connector.generate_changes(event)

    assert len(diffs) > 0
    affected_files = {d.file_path for d in diffs}

    # orders.sql is the source model — always processed
    assert any("orders.sql" in f for f in affected_files)
    # revenue.sql directly references orders via ref("orders")
    assert any("revenue.sql" in f for f in affected_files)
    # stg_orders.sql is upstream of orders (no ref("orders")) — must not be touched
    assert not any("stg_orders.sql" in f for f in affected_files)


async def test_column_rename_skips_transitive_dependents(connector, tmp_path):
    """A transitive dependent that has the old column name from a DIFFERENT
    upstream source must not be touched, even if the LLM included it in
    affected_entity_ids.

    Scenario: stg_orders defines `date as order_date`. It is in
    affected_entity_ids but does not reference the source model (orders) —
    it is upstream. Puxti must not rename it.
    """
    event = SemanticChangeEvent(
        change_event_id="evt-transitive-001",
        entity_id="model.jaffle_shop.orders.order_date",
        change_type=ChangeType.STRUCTURAL,
        semantic_context="rename order_date",
        affected_entity_ids=[
            "model.jaffle_shop.orders",
            "model.jaffle_shop.stg_orders",  # upstream — no ref("orders"), must be skipped
        ],
        reasoning="Scoped rename",
        change={"before": {"name": "order_date"}, "after": {"name": "recorded_date"}},
    )

    diffs, _ = await connector.generate_changes(event)
    affected_files = {d.file_path for d in diffs}

    assert any("orders.sql" in f for f in affected_files)       # source model — processed
    assert not any("stg_orders.sql" in f for f in affected_files)  # upstream — skipped


async def test_column_rename_source_with_only_qualified_refs_goes_to_unverified(tmp_path):
    """When the source model only references the column via a qualified ref (e.g.
    s.type), no bare rename is possible. The source model must land in unverified
    rather than silently disappearing from the PR.
    """
    # Build a project where 'orders' only has 's.order_date' (qualified ref)
    manifest = {
        "metadata": {"dbt_schema_version": "v9", "project_name": "jaffle_shop"},
        "nodes": {
            "model.jaffle_shop.orders": {
                "resource_type": "model",
                "name": "orders",
                "original_file_path": "models/orders.sql",
                "schema": "public", "database": "dev", "description": "", "tags": [],
                "columns": {},
                "depends_on": {"nodes": ["model.jaffle_shop.stg_orders"]},
            },
        },
        "sources": {},
    }
    # Source model SQL: column only appears as qualified ref
    orders_sql = "select s.order_date from {{ ref('stg_orders') }} s"

    target = tmp_path / "target"
    target.mkdir()
    (target / "manifest.json").write_text(json.dumps(manifest))
    models = tmp_path / "models"
    models.mkdir()
    (models / "orders.sql").write_text(orders_sql)

    connector = DbtConnector(config={"project_dir": str(tmp_path)})

    event = SemanticChangeEvent(
        change_event_id="evt-qualified-src",
        entity_id="model.jaffle_shop.orders.order_date",
        change_type=ChangeType.STRUCTURAL,
        semantic_context="rename order_date",
        affected_entity_ids=["model.jaffle_shop.orders"],
        reasoning="",
        change={"before": {"name": "order_date"}, "after": {"name": "recorded_date"}},
    )

    diffs, unverified = await connector.generate_changes(event)

    # No diff for the source — qualified ref, not renameable automatically
    assert diffs == []
    # Source model must appear in unverified so the PR can warn the user
    assert "model.jaffle_shop.orders" in unverified


@pytest.mark.parametrize("is_source", [True, False])
async def test_column_rename_mixed_refs_goes_to_unverified(tmp_path, is_source):
    """A model with both bare and qualified refs must not get a partial rename."""
    manifest = {
        "metadata": {"dbt_schema_version": "v9", "project_name": "jaffle_shop"},
        "nodes": {
            "model.jaffle_shop.orders": {
                "resource_type": "model", "name": "orders",
                "original_file_path": "models/orders.sql",
                "schema": "public", "database": "dev", "description": "", "tags": [],
                "columns": {}, "depends_on": {"nodes": []},
            },
            "model.jaffle_shop.report": {
                "resource_type": "model", "name": "report",
                "original_file_path": "models/report.sql",
                "schema": "public", "database": "dev", "description": "", "tags": [],
                "columns": {}, "depends_on": {"nodes": ["model.jaffle_shop.orders"]},
            },
        },
        "sources": {},
    }
    mixed = "select order_date, s.order_date as d from {{ ref('orders') }} s"
    (tmp_path / "target").mkdir()
    (tmp_path / "target" / "manifest.json").write_text(json.dumps(manifest))
    (tmp_path / "models").mkdir()
    orders_sql = mixed if is_source else "select order_date from t"
    (tmp_path / "models" / "orders.sql").write_text(orders_sql)
    (tmp_path / "models" / "report.sql").write_text(mixed if not is_source else "select 1")

    event = SemanticChangeEvent(
        change_event_id="evt-mixed",
        entity_id="model.jaffle_shop.orders.order_date",
        change_type=ChangeType.STRUCTURAL,
        semantic_context="rename order_date",
        affected_entity_ids=["model.jaffle_shop.orders", "model.jaffle_shop.report"],
        reasoning="",
        change={"before": {"name": "order_date"}, "after": {"name": "recorded_date"}},
    )
    connector = DbtConnector(config={"project_dir": str(tmp_path)})
    diffs, unverified = await connector.generate_changes(event)

    mixed_id = "model.jaffle_shop.orders" if is_source else "model.jaffle_shop.report"
    mixed_file = "models/orders.sql" if is_source else "models/report.sql"
    assert mixed_id in unverified
    assert all(d.file_path != mixed_file for d in diffs)


@pytest.mark.parametrize(
    "sql",
    [
        "select order_date, o.order_date from orders o",
        'select order_date, o."order_date" from orders o',
        "select order_date, o.`order_date` from orders o",
        "select order_date, o.[order_date] from orders o",
        "select order_date, o . order_date from orders o",
        "select order_date, o.ORDER_DATE from orders o",
        "select order_date, (o).order_date from orders o",
        "select order_date, {{ ref('orders') }}.order_date from {{ ref('orders') }}",
        "select order_date, o./* note */order_date from orders o",
        "select order_date, o.-- note\norder_date from orders o",
        'select order_date, o./* note */"order_date" from orders o',
        'select order_date, o. -- note\n "order_date" from orders o',
        # BigQuery struct field access: order_date is the column.
        "select order_date, t.order_date.day from t",
    ],
)
def test_rename_fully_refuses_qualified_reference(sql):
    assert rename_column_fully(sql, "order_date", "recorded_date") is None


@pytest.mark.parametrize(
    "sql, expected",
    [
        ("select order_date from orders", "select recorded_date from orders"),
        ("select 'o.order_date' from orders", "select 'o.order_date' from orders"),
        ("select 1 -- o.order_date", "select 1 -- o.order_date"),
        ("select o.recorded_order_date from orders o",
         "select o.recorded_order_date from orders o"),
        ("select {{ o.order_date }} from orders", "select {{ o.order_date }} from orders"),
        # Dots inside a quoted name are not qualifiers.
        ("select order_date from `proj.ds.order_date`",
         "select recorded_date from `proj.ds.order_date`"),
        ('select "o.order_date" as x, order_date from t',
         'select "o.order_date" as x, recorded_date from t'),
        # A quoted name in another case is a different column.
        ('select order_date, o."ORDER_DATE" from t o',
         'select recorded_date, o."ORDER_DATE" from t o'),
        # Relation names and path segments are not column references.
        ("select order_date from raw.order_date",
         "select recorded_date from raw.order_date"),
        ("select order_date from proj.order_date.tbl",
         "select recorded_date from proj.order_date.tbl"),
        ("select order_date from a join proj.order_date.tbl b on true",
         "select recorded_date from a join proj.order_date.tbl b on true"),
        ('select order_date from "raw"."order_date"',
         'select recorded_date from "raw"."order_date"'),
    ],
)
def test_rename_fully_allows_non_qualified(sql, expected):
    assert rename_column_fully(sql, "order_date", "recorded_date") == expected


async def test_column_rename_diff_content(connector):
    event = SemanticChangeEvent(
        change_event_id="evt-002",
        entity_id="model.jaffle_shop.orders.order_date",
        change_type=ChangeType.STRUCTURAL,
        semantic_context="renamed for clarity",
        affected_entity_ids=["model.jaffle_shop.orders"],
        reasoning="Deterministic rename",
        change={"before": {"name": "order_date"}, "after": {"name": "recorded_date"}},
    )

    diffs, _ = await connector.generate_changes(event)
    orders_diff = next(d for d in diffs if "orders.sql" in d.file_path)

    assert "order_date" in orders_diff.before
    assert "recorded_date" in orders_diff.after
    assert "order_date" not in orders_diff.after


async def test_column_rename_no_diff_when_not_referenced(connector):
    """A rename of a column not referenced in any model produces no diffs."""
    event = SemanticChangeEvent(
        change_event_id="evt-003",
        entity_id="source.jaffle_shop.raw.orders.nonexistent_col",
        change_type=ChangeType.STRUCTURAL,
        semantic_context="test",
        affected_entity_ids=[],
        reasoning="test",
        change={"before": {"name": "nonexistent_col"}, "after": {"name": "new_col"}},
    )

    diffs, _ = await connector.generate_changes(event)
    assert diffs == []


# ── _rename_column_in_sql ─────────────────────────────────────────────────────

def test_rename_bare_identifier():
    sql = "select order_date from orders"
    result = _rename_column_in_sql(sql, "order_date", "recorded_date")
    assert result == "select recorded_date from orders"


def test_rename_does_not_partial_match():
    """'date' should not match inside 'order_date'."""
    sql = "select order_date from orders"
    result = _rename_column_in_sql(sql, "date", "recorded_date")
    # 'date' as a bare word doesn't appear — no change expected
    assert result == sql


def test_rename_qualified_reference_unchanged():
    """Qualified refs are never touched — table alias is ambiguous at text level.
    Renames here risk wrong columns (s.type from a different table) or broken
    SQL (AS alias in WHERE clauses is invalid)."""
    sql = "select o.order_date from orders o"
    result = _rename_column_in_sql(sql, "order_date", "recorded_date")
    assert result == sql  # entirely unchanged


def test_rename_qualified_reference_in_where_unchanged():
    """Qualified refs in WHERE clauses must not be touched — alias form is invalid SQL there."""
    sql = "where s.type in ('reg_season', 'playoff')"
    result = _rename_column_in_sql(sql, "type", "result_type")
    assert result == sql


def test_rename_backtick_quoted():
    sql = "select `order_date` from orders"
    result = _rename_column_in_sql(sql, "order_date", "recorded_date")
    assert "`recorded_date`" in result


def test_rename_double_quoted():
    sql = 'select "order_date" from orders'
    result = _rename_column_in_sql(sql, "order_date", "recorded_date")
    assert '"recorded_date"' in result


def test_rename_preserves_alias():
    sql = "select order_date as order_date from orders"
    result = _rename_column_in_sql(sql, "order_date", "recorded_date")
    assert "recorded_date as recorded_date" in result


# ── Literals, comments, quoting, case ─────────────────────────────────────────

def test_rename_skips_string_literals():
    sql = "select order_date, 'order_date' as label, 'it''s order_date' from t"
    assert _rename_column_in_sql(sql, "order_date", "recorded_date") == (
        "select recorded_date, 'order_date' as label, 'it''s order_date' from t"
    )


def test_rename_skips_sql_comments():
    sql = "select order_date -- order_date is the key\n/* old order_date\nnote */ from t"
    assert _rename_column_in_sql(sql, "order_date", "recorded_date") == (
        "select recorded_date -- order_date is the key\n/* old order_date\nnote */ from t"
    )


def test_rename_apostrophe_in_comment_does_not_open_a_string():
    sql = "-- don't touch\nselect order_date from t"
    assert _rename_column_in_sql(sql, "order_date", "recorded_date") == (
        "-- don't touch\nselect recorded_date from t"
    )


def test_rename_quoted_identifier_containing_comment_marker():
    sql = 'select "a--b", "order_date" from t'
    assert _rename_column_in_sql(sql, "order_date", "recorded_date") == (
        'select "a--b", "recorded_date" from t'
    )


def test_rename_bracket_quoted():
    sql = "select [order_date], [other order_date] from t"
    assert _rename_column_in_sql(sql, "order_date", "recorded_date") == (
        "select [recorded_date], [other order_date] from t"
    )


def test_rename_quoted_is_case_sensitive():
    sql = 'select "ORDER_DATE", `Order_Date`, [ORDER_DATE] from t'
    assert _rename_column_in_sql(sql, "order_date", "recorded_date") == sql


def test_rename_bare_keeps_case_style():
    sql = "select ORDER_DATE, order_date, Order_Date from t"
    assert _rename_column_in_sql(sql, "order_date", "recorded_date") == (
        "select RECORDED_DATE, recorded_date, recorded_date from t"
    )


def test_rename_exact_match_uses_new_name_as_given():
    sql = "select orderDate from t"
    assert _rename_column_in_sql(sql, "orderDate", "recordedDate") == (
        "select recordedDate from t"
    )


def test_rename_literal_containing_jinja_stays_one_literal():
    sql = "where d >= '{{ var(\"s\") }}' and order_date > 1 and s = 'x'"
    assert _rename_column_in_sql(sql, "order_date", "recorded_date") == (
        "where d >= '{{ var(\"s\") }}' and recorded_date > 1 and s = 'x'"
    )


def test_rename_comment_containing_jinja_stays_a_comment():
    sql = "select 1 -- {{ x }} order_date\nfrom t"
    assert _rename_column_in_sql(sql, "order_date", "recorded_date") == sql


def test_rename_inside_array_and_list_brackets():
    sql = "select ARRAY[order_date, x], arr[order_date + 1], [order_date, y] from t"
    assert _rename_column_in_sql(sql, "order_date", "recorded_date") == (
        "select ARRAY[recorded_date, x], arr[recorded_date + 1], [recorded_date, y] from t"
    )


@pytest.mark.parametrize(
    "ref",
    ['o."order_date"', "o.[order_date]", '"o".order_date', "[o].order_date", "o.`order_date`"],
)
def test_rename_qualified_quoted_forms_unchanged(ref):
    sql = f"select {ref} from orders o"
    assert _rename_column_in_sql(sql, "order_date", "recorded_date") == sql


@pytest.mark.parametrize("bad", ["a b", "1abc", "a-b", "a;drop", "", "a`b", 'a"b'])
def test_rename_rejects_invalid_new_identifier(bad):
    with pytest.raises(ValueError):
        _rename_column_in_sql("select order_date from t", "order_date", bad)


# ── Jinja handling ────────────────────────────────────────────────────────────

def test_rename_leaves_ref_and_source_text_untouched():
    """A column sharing a model's name must not alter ref()/source() arguments."""
    sql = "select orders from {{ ref('orders') }}"
    result = _rename_column_in_sql(sql, "orders", "order_count")
    assert result == "select order_count from {{ ref('orders') }}"


def test_rename_leaves_jinja_comment_and_block_untouched():
    sql = "{# order_date note #}\n{% set cols = ['order_date'] %}\nselect order_date from t"
    result = _rename_column_in_sql(sql, "order_date", "recorded_date")
    assert result == (
        "{# order_date note #}\n{% set cols = ['order_date'] %}\nselect recorded_date from t"
    )


def test_rename_leaves_multiline_jinja_expression_untouched():
    sql = (
        "select {{ dbt_utils.star(\n  from=ref('t'),\n  except=['order_date']) }}, "
        "order_date from t"
    )
    result = _rename_column_in_sql(sql, "order_date", "recorded_date")
    assert "except=['order_date']" in result
    assert result.endswith(", recorded_date from t")


def test_rename_closing_delimiter_inside_jinja_string_stays_in_span():
    """A quoted "}}" or "%}" must not end the Jinja span early."""
    sql = "select {{ '}}' if false else 'order_date' }} from orders"
    assert _rename_column_in_sql(sql, "order_date", "recorded_date") == sql
    assert mentions_in_jinja(sql, "order_date")

    sql = "{% set a = '%}' ~ \"order_date\" %}\nselect order_date from t"
    assert _rename_column_in_sql(sql, "order_date", "recorded_date") == (
        "{% set a = '%}' ~ \"order_date\" %}\nselect recorded_date from t"
    )
    assert mentions_in_jinja(sql, "order_date")


def test_rename_apostrophe_in_jinja_comment_does_not_break_spans():
    sql = "{# don't touch #}\nselect order_date from {{ ref('orders') }}"
    assert _rename_column_in_sql(sql, "order_date", "recorded_date") == (
        "{# don't touch #}\nselect recorded_date from {{ ref('orders') }}"
    )


def test_rename_unterminated_jinja_does_not_hang():
    sql = "select order_date from {{ ref('orders') " + "'x' " * 200
    assert "recorded_date" in _rename_column_in_sql(sql, "order_date", "recorded_date")


def test_mentions_in_jinja_detects_macro_argument():
    sql = "select {{ dbt_utils.generate_surrogate_key(['order_date']) }} as k from t"
    assert mentions_in_jinja(sql, "order_date")


def test_mentions_in_jinja_detects_set_block_and_comment():
    assert mentions_in_jinja("{% set c = ['order_date'] %}", "order_date")
    assert mentions_in_jinja("{# order_date #}", "order_date")


def test_mentions_in_jinja_ignores_plain_sql_and_partial_words():
    assert not mentions_in_jinja("select order_date from {{ ref('orders') }}", "order_date")
    assert not mentions_in_jinja("select {{ f('order_dates') }}", "order_date")


def _single_model_project(tmp_path: Path, sql: str) -> DbtConnector:
    manifest = {
        "metadata": {"dbt_schema_version": "v9", "project_name": "jaffle_shop"},
        "nodes": {
            "model.jaffle_shop.orders": {
                "resource_type": "model",
                "name": "orders",
                "original_file_path": "models/orders.sql",
                "schema": "public", "database": "dev", "description": "", "tags": [],
                "columns": {},
                "depends_on": {"nodes": []},
            },
        },
        "sources": {},
    }
    (tmp_path / "target").mkdir()
    (tmp_path / "target" / "manifest.json").write_text(json.dumps(manifest))
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "orders.sql").write_text(sql)
    return DbtConnector(config={"project_dir": str(tmp_path)})


def _rename_event() -> SemanticChangeEvent:
    return SemanticChangeEvent(
        change_event_id="evt-jinja",
        entity_id="model.jaffle_shop.orders.order_date",
        change_type=ChangeType.STRUCTURAL,
        semantic_context="rename order_date",
        affected_entity_ids=["model.jaffle_shop.orders"],
        reasoning="",
        change={"before": {"name": "order_date"}, "after": {"name": "recorded_date"}},
    )


async def test_column_rename_model_with_jinja_reference_goes_to_unverified(tmp_path, caplog):
    """A column referenced inside a Jinja expression cannot be renamed safely.
    The model must be skipped (no half-renamed diff) and flagged unverified."""
    sql = (
        "select order_date,\n"
        "  {{ dbt_utils.generate_surrogate_key(['order_date']) }} as k\n"
        "from raw_orders"
    )
    connector = _single_model_project(tmp_path, sql)

    with caplog.at_level("WARNING", logger="puxti.connectors.dbt"):
        diffs, unverified = await connector.generate_changes(_rename_event())

    assert diffs == []
    assert unverified == ["model.jaffle_shop.orders"]
    assert "inside a Jinja expression" in caplog.text


async def test_column_rename_name_inside_source_args_goes_to_unverified(tmp_path):
    """A column name also found in source() arguments is Jinja text: flagged, not rewritten."""
    connector = _single_model_project(
        tmp_path, "select order_date from {{ source('raw', 'order_date') }}"
    )

    diffs, unverified = await connector.generate_changes(_rename_event())

    assert unverified == ["model.jaffle_shop.orders"]  # 'order_date' inside source()
    assert diffs == []


async def test_column_rename_plain_sql_with_jinja_elsewhere_still_renames(tmp_path):
    connector = _single_model_project(
        tmp_path, "select order_date from {{ ref('stg_orders') }}"
    )

    diffs, unverified = await connector.generate_changes(_rename_event())

    assert unverified == []
    assert len(diffs) == 1
    assert diffs[0].after == "select recorded_date from {{ ref('stg_orders') }}"


# ── column rename — schema.yml and pre-flight checks (#49) ───────────────────

YAML_ORDERS_SQL = """
select
    order_id,
    order_date,
    customer_id
from {{ ref('stg_orders') }}
""".strip()

YAML_CUSTOMER_ORDERS_SQL = """
select
    customer_id,
    order_id,
    order_date
from {{ ref('orders') }}
""".strip()

YAML_SHIPMENTS_SQL = (
    "select id as shipment_id, order_ref, placed_on from {{ ref('raw_shipments') }}"
)

SCHEMA_YML = """version: 2

models:
  # Orders mart: one row per order.
  - name: orders
    config:
      contract:
        enforced: true
    constraints:
      - type: primary_key
        columns: [order_id]
      - type: unique
        columns: [order_id, order_date]
    columns:
      - name: order_id
        data_type: integer
        data_tests: [unique, not_null]
      - name: order_date   # when the order was placed
        description: Date the order was placed.
        data_type: date
        meta:
          owner: finance
        data_tests:
          - not_null

      - name: customer_id
        data_type: integer

  - name: customer_orders
    columns:
      - name: "order_date"
        description: Placed date, carried from orders.
        data_tests:
          - relationships:
              to: ref('orders')
              field: order_date
"""

MARTS_YML = """version: 2

models:
  - name: shipments
    columns:
      - name: order_ref
        tests:
          - relationships:
              arguments:
                to: ref("orders")
                field: order_date
      - name: placed_on
        tests:
          # Same field name, different model: must not change.
          - relationships:
              to: ref('customers')
              field: order_date
"""


def _relationships_test(name: str, model: str, to: str, path: str) -> dict:
    return {
        "resource_type": "test",
        "name": name,
        "original_file_path": path,
        "attached_node": f"model.jaffle_shop.{model}",
        "test_metadata": {
            "name": "relationships",
            "kwargs": {"to": to, "field": "order_date", "column_name": "x"},
        },
        "depends_on": {"nodes": []},
    }


class _StubConnector(BaseConnector):
    """Stands in for any connector that runs after dbt."""

    name = "stub"

    def __init__(self) -> None:
        super().__init__({})
        self.called = False

    async def health_check(self) -> bool:
        return True

    async def extract_entities(self) -> list:
        return []

    async def extract_lineage(self) -> list:
        return []

    async def generate_changes(self, event: SemanticChangeEvent) -> tuple[list, list]:
        self.called = True
        return [], []


def _yaml_project(
    tmp_path: Path,
    *,
    orders_columns: tuple[str, ...] = (),
    customer_orders_columns: tuple[str, ...] = (),
) -> DbtConnector:
    """A project whose models are documented in schema.yml, with a contract,
    column tests, model constraints and relationships tests from other models."""

    def model(name: str, sql_path: str, patch: str | None, columns: tuple[str, ...]) -> dict:
        return {
            "resource_type": "model",
            "name": name,
            "original_file_path": sql_path,
            "patch_path": f"jaffle_shop://{patch}" if patch else None,
            "schema": "public", "database": "dev", "description": "", "tags": [],
            "columns": {c: {"name": c, "description": ""} for c in columns},
            "depends_on": {"nodes": []},
        }

    manifest = {
        "metadata": {
            "dbt_schema_version": "v12", "project_name": "jaffle_shop", "adapter_type": "duckdb",
        },
        "nodes": {
            "model.jaffle_shop.orders": model(
                "orders", "models/orders.sql", "models/schema.yml",
                ("order_id", "order_date", "customer_id", *orders_columns),
            ),
            "model.jaffle_shop.customer_orders": model(
                "customer_orders", "models/customer_orders.sql", "models/schema.yml",
                ("order_date", *customer_orders_columns),
            ),
            "model.jaffle_shop.shipments": model(
                "shipments", "models/marts/shipments.sql", "models/marts/marts.yml",
                ("order_ref", "placed_on"),
            ),
            "test.jaffle_shop.rel_customer_orders": _relationships_test(
                "rel_customer_orders", "customer_orders", "ref('orders')", "models/schema.yml",
            ),
            "test.jaffle_shop.rel_shipments_orders": _relationships_test(
                "rel_shipments_orders", "shipments", 'ref("orders")', "models/marts/marts.yml",
            ),
            "test.jaffle_shop.rel_shipments_customers": _relationships_test(
                "rel_shipments_customers", "shipments", "ref('customers')",
                "models/marts/marts.yml",
            ),
        },
        "sources": {},
    }
    (tmp_path / "target").mkdir()
    (tmp_path / "target" / "manifest.json").write_text(json.dumps(manifest))
    (tmp_path / "models" / "marts").mkdir(parents=True)
    (tmp_path / "models" / "orders.sql").write_text(YAML_ORDERS_SQL)
    (tmp_path / "models" / "customer_orders.sql").write_text(YAML_CUSTOMER_ORDERS_SQL)
    (tmp_path / "models" / "marts" / "shipments.sql").write_text(YAML_SHIPMENTS_SQL)
    (tmp_path / "models" / "schema.yml").write_text(SCHEMA_YML)
    (tmp_path / "models" / "marts" / "marts.yml").write_text(MARTS_YML)
    return DbtConnector(config={"project_dir": str(tmp_path)})


def _yaml_rename_event(new_name: str = "recorded_date") -> SemanticChangeEvent:
    return SemanticChangeEvent(
        change_event_id="evt-49",
        entity_id="model.jaffle_shop.orders.order_date",
        change_type=ChangeType.STRUCTURAL,
        semantic_context="rename order_date",
        affected_entity_ids=["model.jaffle_shop.orders", "model.jaffle_shop.customer_orders"],
        reasoning="",
        change={"before": {"name": "order_date"}, "after": {"name": new_name}},
    )


async def test_column_rename_keeps_schema_yml_consistent(tmp_path):
    """The renamed column's entry (with its tests, description, meta and contract
    data_type), the model's constraint columns, the dependent model's entry and
    the relationships tests pointing at the renamed model all move to the new
    name. Nothing else in the file changes: comments, quoting and blank lines stay."""
    connector = _yaml_project(tmp_path)

    diffs, unverified = await connector.generate_changes(_yaml_rename_event())

    assert unverified == []
    by_path = {d.file_path: d for d in diffs}
    assert set(by_path) == {
        "models/orders.sql",
        "models/customer_orders.sql",
        "models/schema.yml",
        "models/marts/marts.yml",
    }
    assert by_path["models/schema.yml"].before == SCHEMA_YML
    assert by_path["models/schema.yml"].after == (
        SCHEMA_YML
        .replace("columns: [order_id, order_date]", "columns: [order_id, recorded_date]")
        .replace("- name: order_date   #", "- name: recorded_date   #")
        .replace('- name: "order_date"', '- name: "recorded_date"')
        .replace("field: order_date", "field: recorded_date")
    )
    assert by_path["models/marts/marts.yml"].after == MARTS_YML.replace(
        "                field: order_date", "                field: recorded_date"
    )
    # The decoy relationships test (to: ref('customers')) is untouched.
    assert "to: ref('customers')\n              field: order_date" in (
        by_path["models/marts/marts.yml"].after
    )


async def test_column_rename_writes_no_yaml_for_an_undocumented_model(connector):
    """A model without a patch_path has no schema.yml to keep in sync."""
    event = _yaml_rename_event()
    event.affected_entity_ids = ["model.jaffle_shop.orders", "model.jaffle_shop.revenue"]

    diffs, _ = await connector.generate_changes(event)

    assert diffs
    assert all(d.file_path.endswith(".sql") for d in diffs)


async def test_column_rename_collision_in_manifest_columns_aborts(tmp_path):
    """#49 repro: renaming order_date to recorded_date when the model already has
    recorded_date would produce `select recorded_date, recorded_date`."""
    connector = _yaml_project(tmp_path, orders_columns=("recorded_date",))

    with pytest.raises(ColumnRenameCollisionError) as exc_info:
        await connector.generate_changes(_yaml_rename_event())

    message = str(exc_info.value)
    assert "model `orders`" in message
    assert "its YAML documents a column named `recorded_date`" in message
    assert "stale" in message


async def test_column_rename_collision_in_select_list_aborts(tmp_path):
    """An undocumented column counts too: the SELECT list already outputs new_name."""
    connector = _single_model_project(
        tmp_path, "select order_date, created_at as recorded_date from raw_orders"
    )

    with pytest.raises(ColumnRenameCollisionError) as exc_info:
        await connector.generate_changes(_rename_event())

    assert "its SQL already outputs a column named `recorded_date`" in str(exc_info.value)


async def test_column_rename_collision_matches_case_insensitively(tmp_path):
    connector = _single_model_project(
        tmp_path, "select order_date, RECORDED_DATE from raw_orders"
    )

    with pytest.raises(ColumnRenameCollisionError):
        await connector.generate_changes(_rename_event())


async def test_column_rename_collision_in_a_dependent_flags_only_that_model(tmp_path, caplog):
    """A dependent that already outputs new_name is left for manual review. The
    source model still renames, and the dependent's own schema.yml entry stays."""
    connector = _yaml_project(tmp_path)
    (tmp_path / "models" / "customer_orders.sql").write_text(
        "select order_date, shipped_at as recorded_date from {{ ref('orders') }}"
    )

    with caplog.at_level("WARNING", logger="puxti.connectors.dbt"):
        diffs, unverified = await connector.generate_changes(_yaml_rename_event())

    assert unverified == ["model.jaffle_shop.customer_orders"]
    assert "model `customer_orders`" in caplog.text
    by_path = {d.file_path: d for d in diffs}
    assert "models/orders.sql" in by_path
    assert "models/customer_orders.sql" not in by_path
    assert '- name: "order_date"' in by_path["models/schema.yml"].after


async def test_column_rename_collision_in_a_dependent_leaves_other_connectors_running(tmp_path):
    """One downstream model must not stop what other connectors would generate."""
    connector = _yaml_project(tmp_path)
    (tmp_path / "models" / "customer_orders.sql").write_text(
        "select order_date, shipped_at as recorded_date from {{ ref('orders') }}"
    )
    other = _StubConnector()

    results = await PropagationEngine(connectors=[connector, other]).propagate(
        _yaml_rename_event()
    )

    assert other.called
    assert [r.unverified_entity_ids for r in results if r.connector == "dbt"] == [
        ["model.jaffle_shop.customer_orders"]
    ]


async def test_column_rename_stale_yaml_entry_on_a_dependent_flags_it(tmp_path):
    """Only the YAML documents new_name on the dependent: flagged all the same."""
    connector = _yaml_project(tmp_path, customer_orders_columns=("recorded_date",))

    diffs, unverified = await connector.generate_changes(_yaml_rename_event())

    assert unverified == ["model.jaffle_shop.customer_orders"]
    assert "models/customer_orders.sql" not in {d.file_path for d in diffs}


async def test_column_rename_new_name_on_an_untouched_model_is_not_a_collision(tmp_path):
    """A dependent that has new_name but never uses old_name is not rewritten,
    so its own recorded_date column cannot collide."""
    connector = _yaml_project(tmp_path)
    (tmp_path / "models" / "customer_orders.sql").write_text(
        "select customer_id, recorded_date from {{ ref('orders') }}"
    )

    diffs, unverified = await connector.generate_changes(_yaml_rename_event())

    assert unverified == []
    assert "models/customer_orders.sql" not in {d.file_path for d in diffs}


def test_check_column_rename_refuses_a_collision_on_the_source_model(tmp_path):
    connector = _yaml_project(tmp_path, orders_columns=("recorded_date",))

    with pytest.raises(ColumnRenameCollisionError):
        connector.check_column_rename(
            "model.jaffle_shop.orders.order_date", "order_date", "recorded_date"
        )


def test_check_column_rename_passes_without_a_collision(tmp_path):
    connector = _yaml_project(tmp_path)

    connector.check_column_rename(
        "model.jaffle_shop.orders.order_date", "order_date", "recorded_date"
    )


def test_check_column_rename_without_a_manifest_does_not_raise(tmp_path):
    """No manifest means nothing to check against; generation reports that itself."""
    DbtConnector(config={"project_dir": str(tmp_path)}).check_column_rename(
        "model.jaffle_shop.orders.order_date", "order_date", "recorded_date"
    )


async def test_column_rename_that_breaks_parsing_goes_to_unverified(tmp_path, caplog):
    """`where` is a valid identifier to the renamer but not as a bare column:
    the rewrite no longer parses, so the model is flagged and nothing is written
    for it, its schema.yml entry included."""
    connector = _yaml_project(tmp_path)

    with caplog.at_level("WARNING", logger="puxti.connectors.dbt"):
        diffs, unverified = await connector.generate_changes(_yaml_rename_event("where"))

    assert unverified == ["model.jaffle_shop.orders", "model.jaffle_shop.customer_orders"]
    assert diffs == []
    assert "no longer parses" in caplog.text


async def test_column_rename_of_sql_that_did_not_parse_before_is_not_flagged(tmp_path):
    """Masked Jinja control flow can leave SQL that never parsed (two WHEREs from
    an incremental block). The check flags only a rewrite that breaks parsing."""
    connector = _single_model_project(
        tmp_path,
        "select order_date from raw_orders where 1 = 1\n"
        "{% if is_incremental() %} where order_date > (select max(order_date) "
        "from {{ this }}) {% endif %}",
    )

    diffs, unverified = await connector.generate_changes(_rename_event())

    assert unverified == []
    assert len(diffs) == 1


# ── find_model_path — the public seam engines use to locate sources ───────────

def _manifest_connector(tmp_path, nodes: dict) -> DbtConnector:
    import json
    (tmp_path / "target").mkdir()
    (tmp_path / "target" / "manifest.json").write_text(json.dumps({"nodes": nodes}))
    return DbtConnector(config={"project_dir": str(tmp_path)})


def test_find_model_path_returns_repo_relative_path(tmp_path):
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "orders.sql").write_text("select 1")
    connector = _manifest_connector(tmp_path, {
        "model.shop.orders": {"resource_type": "model", "original_file_path": "models/orders.sql"},
    })
    assert connector.find_model_path("model.shop.orders") == "models/orders.sql"


def test_find_model_path_unknown_entity_returns_none(tmp_path):
    connector = _manifest_connector(tmp_path, {})
    assert connector.find_model_path("model.shop.missing") is None


def test_find_model_path_missing_file_returns_none(tmp_path):
    connector = _manifest_connector(tmp_path, {
        "model.shop.orders": {"resource_type": "model", "original_file_path": "models/orders.sql"},
    })
    assert connector.find_model_path("model.shop.orders") is None


def test_find_model_path_missing_manifest_returns_none(tmp_path):
    connector = DbtConnector(config={"project_dir": str(tmp_path)})
    assert connector.find_model_path("model.shop.orders") is None


def test_seed_definitions_include_input_and_measure_filters(tmp_path):
    manifest = {
        **METRICS_MANIFEST,
        "metrics": {
            "metric.jaffle_shop.food_order_pct": _metric(
                "food_order_pct", "ratio", ["metric.jaffle_shop.orders"],
                type_params={
                    "numerator": {"name": "orders", "filter": {"where_filters": [
                        {"where_sql_template": "{{ Dimension('order__is_food') }} = true"},
                    ]}},
                    "denominator": {"name": "orders"},
                },
            ),
            "metric.jaffle_shop.big_order_total": _metric(
                "big_order_total", "simple", ["semantic_model.jaffle_shop.orders"],
                type_params={"measure": {"name": "order_total", "filter": {"where_filters": [
                    {"where_sql_template": "{{ Dimension('order__total') }} > 100"},
                ]}}},
            ),
        },
    }
    (tmp_path / "target").mkdir()
    (tmp_path / "target" / "manifest.json").write_text(json.dumps(manifest))
    defs = DbtConnector(config={"project_dir": str(tmp_path)}).get_seed_definitions()
    assert defs["metric.jaffle_shop.food_order_pct"] == (
        "Ratio metric: orders (where {{ Dimension('order__is_food') }} = true) / orders"
    )
    assert defs["metric.jaffle_shop.big_order_total"] == (
        "Simple metric: sum of order_total (measure order_total in semantic model orders), "
        "where {{ Dimension('order__total') }} > 100"
    )


def test_seed_definitions_without_a_resolvable_measure(tmp_path):
    manifest = {
        **METRICS_MANIFEST,
        "metrics": {
            "metric.jaffle_shop.no_measure": _metric("no_measure", "simple", []),
            "metric.jaffle_shop.unknown_measure": _metric(
                "unknown_measure", "simple", [], type_params={"measure": {"name": "gone"}},
            ),
            "metric.jaffle_shop.inline_agg": _metric(
                "inline_agg", "simple", [], type_params={"agg": "sum", "expr": "amount"},
            ),
            "metric.jaffle_shop.no_measure_cumulative": _metric(
                "no_measure_cumulative", "cumulative", [],
            ),
        },
    }
    (tmp_path / "target").mkdir()
    (tmp_path / "target" / "manifest.json").write_text(json.dumps(manifest))
    defs = DbtConnector(config={"project_dir": str(tmp_path)}).get_seed_definitions()
    assert defs["metric.jaffle_shop.no_measure"] == "Simple metric."
    assert defs["metric.jaffle_shop.unknown_measure"] == "Simple metric: measure gone"
    assert defs["metric.jaffle_shop.inline_agg"] == "Simple metric: sum of amount"
    assert defs["metric.jaffle_shop.no_measure_cumulative"] == (
        "Cumulative metric, over all time."
    )
