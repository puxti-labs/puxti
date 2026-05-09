import json
import pytest
from pathlib import Path

from puxti.connectors.dbt import DbtConnector, _rename_column_in_sql
from puxti.models import ChangeType, EntityType, SemanticChangeEvent


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
                "order_date": {"name": "order_date", "description": "Date placed", "data_type": "date"},
                "amount": {"name": "amount", "description": "Total amount", "data_type": "numeric"},
            },
            "depends_on": {"nodes": ["model.jaffle_shop.stg_orders", "source.jaffle_shop.raw.orders"]},
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
                "total_revenue": {"name": "total_revenue", "description": "", "data_type": "numeric"},
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


# ── generate_changes — column rename ─────────────────────────────────────────

async def test_column_rename_generates_diffs(connector):
    event = SemanticChangeEvent(
        change_event_id="evt-001",
        entity_id="model.jaffle_shop.orders.order_date",
        change_type=ChangeType.STRUCTURAL,
        semantic_context="order_date renamed to recorded_date to clarify it is the date the order was recorded, not the transaction date",
        affected_entity_ids=["model.jaffle_shop.orders", "model.jaffle_shop.revenue", "model.jaffle_shop.stg_orders"],
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
