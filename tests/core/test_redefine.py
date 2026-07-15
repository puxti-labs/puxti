"""Tests for SemanticRedefiner — Case 3: definition redefinition."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from puxti.core.redefine import SemanticRedefiner, _annotation_only_diff, _annotate_sql, _conflict_annotation_diff, _first_sentence, _ensure_newline
from puxti.llm import LLMResponse
from puxti.models import Entity, EntityType


# ── Fixtures ──────────────────────────────────────────────────────────────────

ORDERS_ENTITY = Entity(
    id="model.demo_shop.orders",
    name="orders",
    type=EntityType.MODEL,
    source_connector="dbt",
)

CUSTOMERS_ENTITY = Entity(
    id="model.demo_shop.customers",
    name="customers",
    type=EntityType.MODEL,
    source_connector="dbt",
)

ORDERS_SQL = "select id, gross_revenue from stg_orders"
CUSTOMERS_SQL = "select customer_id, sum(gross_revenue) as lifetime_value from orders group by 1"

OLD_DEF = "gross_revenue is total order value including refunds."
NEW_DEF = "gross_revenue now excludes refunds — only settled transactions count."


def _make_llm_response(payload: dict) -> LLMResponse:
    return LLMResponse(text=json.dumps(payload), truncated=False)


def _make_connector(sql_map: dict | None = None, node_path: str = "models/orders.sql") -> MagicMock:
    connector = MagicMock()
    # `is None` — an explicitly-passed empty dict must mean "no SQL files"
    connector.get_model_sql_map = MagicMock(return_value=sql_map if sql_map is not None else {
        ORDERS_ENTITY.id: ORDERS_SQL,
        CUSTOMERS_ENTITY.id: CUSTOMERS_SQL,
    })
    # engines resolve file locations only through the public connector seam
    paths = {ORDERS_ENTITY.id: node_path, CUSTOMERS_ENTITY.id: "models/customers.sql"}
    connector.find_model_path = MagicMock(side_effect=paths.get)
    return connector


def _make_backend(payload: dict) -> MagicMock:
    backend = MagicMock()
    backend.complete = AsyncMock(return_value=_make_llm_response(payload))
    return backend


# ── generate_diffs — depth-based confidence ───────────────────────────────────

async def test_hop1_generates_sql_diff(tmp_path):
    connector = _make_connector(node_path="models/orders.sql")
    # Make the file exist
    llm_payload = {
        "reasoning": "gross_revenue now excludes refunds so filter needed.",
        "proposed_sql": "select id, gross_revenue from stg_orders where not is_refund",
        "confidence": "high",
    }
    backend = _make_backend(llm_payload)
    redefiner = SemanticRedefiner(backend=backend)

    diffs = await redefiner.generate_diffs(
        entity_id="model.demo_shop.orders.gross_revenue",
        old_definition=OLD_DEF,
        new_definition=NEW_DEF,
        dependents_with_depth=[(ORDERS_ENTITY, 1)],
        connector=connector,
    )

    assert len(diffs) == 1
    assert "PUXTI [high confidence]" in diffs[0].after
    assert "is_refund" in diffs[0].after
    assert "hop depth 1" in diffs[0].description


async def test_hop2_generates_sql_with_verify_label(tmp_path):
    connector = _make_connector(node_path="models/customers.sql")
    llm_payload = {
        "reasoning": "CLV uses gross_revenue, now excludes refunds.",
        "proposed_sql": "select customer_id, sum(gross_revenue) as lifetime_value from orders where not is_refund group by 1",
        "confidence": "medium",
    }
    backend = _make_backend(llm_payload)
    redefiner = SemanticRedefiner(backend=backend)

    diffs = await redefiner.generate_diffs(
        entity_id="model.demo_shop.orders.gross_revenue",
        old_definition=OLD_DEF,
        new_definition=NEW_DEF,
        dependents_with_depth=[(CUSTOMERS_ENTITY, 2)],
        connector=connector,
    )

    assert len(diffs) == 1
    assert "PUXTI [verify carefully]" in diffs[0].after
    assert "hop depth 2" in diffs[0].description


async def test_hop3_returns_annotation_only(tmp_path):
    deep_entity = Entity(
        id="model.demo_shop.board_dashboard",
        name="board_dashboard",
        type=EntityType.MODEL,
        source_connector="dbt",
    )
    connector = _make_connector(sql_map={deep_entity.id: "select * from customers"})
    connector.find_model_path = MagicMock(
        side_effect={deep_entity.id: "models/board.sql"}.get
    )

    backend = MagicMock()
    redefiner = SemanticRedefiner(backend=backend)

    diffs = await redefiner.generate_diffs(
        entity_id="model.demo_shop.orders.gross_revenue",
        old_definition=OLD_DEF,
        new_definition=NEW_DEF,
        dependents_with_depth=[(deep_entity, 3)],
        connector=connector,
    )

    assert len(diffs) == 1
    assert "PUXTI [manual review required]" in diffs[0].after
    assert "too deep to generate SQL reliably" in diffs[0].after
    # LLM should NOT have been called for hop 3+
    backend.complete.assert_not_called()


async def test_no_sql_file_returns_no_diff():
    connector = _make_connector(sql_map={})  # no SQL for this entity
    backend = MagicMock()
    redefiner = SemanticRedefiner(backend=backend)

    diffs = await redefiner.generate_diffs(
        entity_id="model.demo_shop.orders.gross_revenue",
        old_definition=OLD_DEF,
        new_definition=NEW_DEF,
        dependents_with_depth=[(ORDERS_ENTITY, 1)],
        connector=connector,
    )

    assert diffs == []


async def test_llm_null_proposed_sql_falls_back_to_annotation(tmp_path):
    """When LLM cannot determine the SQL change, fall back to annotation only."""
    connector = _make_connector(node_path="models/orders.sql")
    llm_payload = {
        "reasoning": "Cannot determine the exact change needed.",
        "proposed_sql": None,
        "confidence": "low",
    }
    backend = _make_backend(llm_payload)
    redefiner = SemanticRedefiner(backend=backend)

    diffs = await redefiner.generate_diffs(
        entity_id="model.demo_shop.orders.gross_revenue",
        old_definition=OLD_DEF,
        new_definition=NEW_DEF,
        dependents_with_depth=[(ORDERS_ENTITY, 1)],
        connector=connector,
    )

    assert len(diffs) == 1
    assert "PUXTI [manual review required]" in diffs[0].after
    assert "LLM could not determine the SQL change" in diffs[0].after
    assert "too deep" not in diffs[0].after


# ── _annotate_sql ─────────────────────────────────────────────────────────────

def test_annotate_sql_prepends_comment_block():
    result = _annotate_sql("SELECT 1", "high confidence", "reason here", "entity.id")
    assert result.startswith("-- PUXTI [high confidence]")
    assert "reason here" in result
    assert "SELECT 1" in result
    assert "upstream changes first" in result
    assert "entity.id" in result
    assert result.endswith("\n")


def test_annotation_only_diff_prepends_review_block(tmp_path):
    connector = _make_connector(node_path="models/orders.sql")
    diff = _annotation_only_diff(
        entity=ORDERS_ENTITY,
        model_sql=ORDERS_SQL,
        entity_id="model.demo_shop.orders.gross_revenue",
        new_definition=NEW_DEF,
        depth=4,
        connector=connector,
    )

    assert diff is not None
    assert "PUXTI [manual review required]" in diff.after
    assert ORDERS_SQL in diff.after
    assert diff.before == ORDERS_SQL


# ── naming conflict ───────────────────────────────────────────────────────────

async def test_llm_conflict_flag_produces_conflict_annotation(tmp_path):
    """When LLM returns conflict:true, produce a conflict annotation without changing SQL."""
    connector = _make_connector(node_path="models/orders.sql")
    llm_payload = {
        "reasoning": "The model has an existing customer_type column computed differently.",
        "proposed_sql": None,
        "confidence": "low",
        "conflict": True,
        "conflict_description": (
            "This model already contains 'customer_type' as a CASE expression "
            "('new' vs 'returning') based on purchase behaviour. The new definition "
            "introduces 'customer_type' meaning corporate vs. private person — "
            "a different semantic dimension. Removing the existing logic would be a "
            "silent breaking change."
        ),
    }
    backend = _make_backend(llm_payload)
    redefiner = SemanticRedefiner(backend=backend)

    diffs = await redefiner.generate_diffs(
        entity_id="model.demo_shop.customers",
        old_definition="customers are classified as new or returning based on purchase count",
        new_definition="customers are classified as corporate or private persons",
        dependents_with_depth=[(ORDERS_ENTITY, 1)],
        connector=connector,
    )

    assert len(diffs) == 1
    diff = diffs[0]
    assert "PUXTI [naming conflict" in diff.after
    assert "CONFLICT:" in diff.after
    assert "corporate vs. private" in diff.after
    # Original SQL must be untouched
    assert diff.before == ORDERS_SQL
    assert diff.after.endswith(ORDERS_SQL)
    assert "naming conflict" in diff.description.lower()


def test_conflict_annotation_diff_preserves_original_sql(tmp_path):
    """_conflict_annotation_diff must not alter the model SQL."""
    connector = _make_connector(node_path="models/orders.sql")
    diff = _conflict_annotation_diff(
        entity=ORDERS_ENTITY,
        model_sql=ORDERS_SQL,
        entity_id="model.demo_shop.customers",
        new_definition="customers are corporate or private",
        conflict_description="existing customer_type uses different value domain",
        connector=connector,
    )

    assert diff is not None
    assert diff.before == ORDERS_SQL
    assert diff.after.endswith(ORDERS_SQL)
    assert "naming conflict" in diff.after.lower()
    assert "existing customer_type uses different value domain" in diff.after
    assert "naming conflict" in diff.description.lower().lower()


# ── _first_sentence / _ensure_newline ────────────────────────────────────────

def test_first_sentence_stops_at_period():
    assert _first_sentence("Added column. More detail here.") == "Added column"

def test_first_sentence_stops_at_newline():
    assert _first_sentence("Added column\nMore detail here.") == "Added column"

def test_first_sentence_truncates_at_max_len():
    long = "x" * 250
    result = _first_sentence(long)
    assert len(result) == 200
    assert result.endswith("…")

def test_ensure_newline_adds_newline():
    assert _ensure_newline("select 1") == "select 1\n"

def test_ensure_newline_does_not_double_newline():
    assert _ensure_newline("select 1\n") == "select 1\n"

def test_ensure_newline_strips_multiple_trailing_newlines():
    assert _ensure_newline("select 1\n\n\n") == "select 1\n"


# ── generate_passthrough_diffs ────────────────────────────────────────────────

STG_ORDERS_ENTITY = Entity(
    id="model.demo_shop.stg_orders",
    name="stg_orders",
    type=EntityType.MODEL,
    source_connector="dbt",
)
STG_ORDERS_SQL = "select id, subtotal from raw_orders"


def _make_passthrough_connector():
    connector = MagicMock()
    connector.get_model_sql_map = MagicMock(return_value={
        ORDERS_ENTITY.id: ORDERS_SQL,
        STG_ORDERS_ENTITY.id: STG_ORDERS_SQL,
    })
    paths = {
        ORDERS_ENTITY.id: "models/orders.sql",
        STG_ORDERS_ENTITY.id: "models/staging/stg_orders.sql",
    }
    connector.find_model_path = MagicMock(side_effect=paths.get)
    return connector


async def test_passthrough_diffs_generates_diff_for_entity_and_ancestors(tmp_path):
    payload = {"reasoning": "Added order_segment to SELECT.", "proposed_sql": "select id, subtotal, order_segment from raw_orders", "confidence": "high"}
    backend = _make_backend(payload)

    redefiner = SemanticRedefiner(backend=backend)
    connector = _make_passthrough_connector()

    diffs = await redefiner.generate_passthrough_diffs(
        entity_id=ORDERS_ENTITY.id,
        new_attribute="order_segment",
        ancestors_with_depth=[(STG_ORDERS_ENTITY, 1)],
        connector=connector,
    )

    # Entity itself (depth 0) + ancestor stg_orders (depth 1) = 2 LLM calls
    assert backend.complete.await_count == 2
    assert len(diffs) == 2
    assert all("passthrough" in d.after for d in diffs)


async def test_passthrough_diffs_skips_semantically_irrelevant_model(tmp_path):
    """When LLM returns no_change:true (model has no relation to attribute), skip it."""
    payload = {"reasoning": "stg_supplies has no customer dimension.", "proposed_sql": None, "no_change": True, "confidence": "high"}
    backend = _make_backend(payload)

    redefiner = SemanticRedefiner(backend=backend)
    connector = _make_passthrough_connector()

    diffs = await redefiner.generate_passthrough_diffs(
        entity_id=ORDERS_ENTITY.id,
        new_attribute="customer_type",
        ancestors_with_depth=[(STG_ORDERS_ENTITY, 1)],
        connector=connector,
    )

    assert diffs == []


async def test_passthrough_diffs_skips_unchanged_sql(tmp_path):
    """If the LLM returns the same SQL (e.g. select * already passes it), no diff."""
    payload = {"reasoning": "select * already includes order_segment.", "proposed_sql": ORDERS_SQL, "confidence": "high"}
    backend = _make_backend(payload)

    redefiner = SemanticRedefiner(backend=backend)
    connector = _make_passthrough_connector()

    diffs = await redefiner.generate_passthrough_diffs(
        entity_id=ORDERS_ENTITY.id,
        new_attribute="order_segment",
        ancestors_with_depth=[],
        connector=connector,
    )

    assert diffs == []


async def test_passthrough_diffs_includes_graph_definition_in_prompt(tmp_path):
    """When graph is provided, definition is included in the LLM prompt."""
    payload = {"reasoning": "Added order_segment.", "proposed_sql": "select id, subtotal, order_segment from raw_orders", "confidence": "high"}
    backend = _make_backend(payload)

    from puxti.models import Definition
    mock_definition = Definition(
        entity_id=ORDERS_ENTITY.id,
        description="One row per order from the source system.",
        version=1,
        created_by="scan",
    )
    mock_graph = MagicMock()
    mock_graph.get_latest_definition = AsyncMock(return_value=mock_definition)

    redefiner = SemanticRedefiner(backend=backend)
    connector = _make_passthrough_connector()

    await redefiner.generate_passthrough_diffs(
        entity_id=ORDERS_ENTITY.id,
        new_attribute="order_segment",
        ancestors_with_depth=[],
        connector=connector,
        graph=mock_graph,
    )

    content = backend.complete.call_args.kwargs["user_message"]
    assert "One row per order from the source system." in content


# ── propose_semantic_edges — error path ───────────────────────────────────────

async def test_propose_semantic_edges_recovers_on_max_tokens_truncation():
    """max_tokens truncation returns empty edges rather than crashing the scan."""
    backend = MagicMock()
    backend.complete = AsyncMock(return_value=LLMResponse(
        text='{"edges": [{"from_entity_id": "a", "to',  # truncated
        truncated=True,
    ))

    from puxti.core.scanner import SemanticScanner
    scanner = SemanticScanner(backend=backend)

    edges, truncated = await scanner.propose_semantic_edges(
        [ORDERS_ENTITY, CUSTOMERS_ENTITY],
        {ORDERS_ENTITY.id: "def", CUSTOMERS_ENTITY.id: "def"},
    )
    assert edges == []
    assert truncated == 1
