import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from puxti.core.capture import SemanticCapture, _build_user_message
from puxti.llm import INPUT_COST_PER_MTOK, OUTPUT_COST_PER_MTOK, LLMBillingError, LLMResponse, TokenCount
from puxti.models import (
    ChangeEvent,
    ChangeStatus,
    ChangeType,
    Definition,
    EdgeType,
    Entity,
    EntityType,
    SemanticEdge,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_backend_from(response: LLMResponse) -> MagicMock:
    """Build a fake LLMBackend returning a fixed response."""
    backend = MagicMock()
    backend.input_cost_per_mtok = INPUT_COST_PER_MTOK
    backend.output_cost_per_mtok = OUTPUT_COST_PER_MTOK
    backend.complete = AsyncMock(return_value=response)
    return backend


def _make_backend(payload: dict) -> MagicMock:
    """Fake LLMBackend whose completion is the payload as JSON."""
    return _make_backend_from(LLMResponse(text=json.dumps(payload), truncated=False))


ENRICHMENT_PAYLOAD = {
    "enriched_description": "order_date renamed to recorded_date to clarify it captures when the order was recorded in the system, not the transaction date.",
    "affected_entity_ids": ["model.jaffle_shop.orders", "model.jaffle_shop.revenue"],
    "reasoning": "revenue references order_date via date_trunc — renaming the column cascades to that model.",
    "suggested_semantic_edges": [
        {
            "from_entity_id": "model.jaffle_shop.revenue",
            "to_entity_id": "model.jaffle_shop.orders.recorded_date",
            "type": "derived_from",
            "description": "revenue.month is derived from orders.recorded_date via date_trunc",
        }
    ],
}

CHANGE_EVENT = ChangeEvent(
    type=ChangeType.STRUCTURAL,
    source_entity_id="model.jaffle_shop.orders.order_date",
    change={"before": {"name": "order_date"}, "after": {"name": "recorded_date"}},
)


def _make_graph(
    existing_definition: Definition | None = None,
    feeds_producers: list[Entity] | None = None,
) -> MagicMock:
    """Build a mock KnowledgeGraph."""
    graph = MagicMock()
    graph.get_latest_definition = AsyncMock(return_value=existing_definition)
    graph.get_semantic_dependents = AsyncMock(return_value=[])
    graph.get_structural_dependents = AsyncMock(return_value=[])
    graph.get_all_entity_ids = AsyncMock(return_value=[])
    graph.filter_existing_entity_ids = AsyncMock(side_effect=lambda ids: ids)
    graph.get_feeds_producers = AsyncMock(return_value=feeds_producers or [])
    graph.upsert_definition = AsyncMock()
    graph.upsert_semantic_edge = AsyncMock()
    graph.save_change_event = AsyncMock()
    return graph


# ── capture — happy path ───────────────────────────────────────────────────────

async def test_capture_returns_semantic_change_event():
    backend = _make_backend(ENRICHMENT_PAYLOAD)

    capture = SemanticCapture(backend=backend)
    result, _ = await capture.capture(CHANGE_EVENT, "Renaming for clarity.", _make_graph())

    assert result.change_event_id == CHANGE_EVENT.id
    assert result.entity_id == CHANGE_EVENT.source_entity_id
    assert result.change_type == ChangeType.STRUCTURAL
    assert result.semantic_context == ENRICHMENT_PAYLOAD["enriched_description"]
    assert result.affected_entity_ids == ENRICHMENT_PAYLOAD["affected_entity_ids"]
    assert result.reasoning == ENRICHMENT_PAYLOAD["reasoning"]
    assert result.change == CHANGE_EVENT.change


async def test_capture_writes_definition_to_graph():
    backend = _make_backend(ENRICHMENT_PAYLOAD)

    graph = _make_graph()
    capture = SemanticCapture(backend=backend)
    _, commit = await capture.capture(CHANGE_EVENT, "Renaming for clarity.", graph)
    await commit()

    graph.upsert_definition.assert_awaited_once()
    definition: Definition = graph.upsert_definition.call_args.args[0]
    assert definition.entity_id == CHANGE_EVENT.source_entity_id
    assert definition.description == ENRICHMENT_PAYLOAD["enriched_description"]
    assert definition.version == 1
    assert definition.created_by == "llm"
    assert definition.change_event_id == CHANGE_EVENT.id


async def test_capture_increments_version_when_existing_definition():
    existing = Definition(
        entity_id=CHANGE_EVENT.source_entity_id,
        description="Old definition.",
        version=3,
        created_by="user",
    )
    backend = _make_backend(ENRICHMENT_PAYLOAD)

    graph = _make_graph(existing_definition=existing)
    capture = SemanticCapture(backend=backend)
    _, commit = await capture.capture(CHANGE_EVENT, "Renaming for clarity.", graph)
    await commit()

    definition: Definition = graph.upsert_definition.call_args.args[0]
    assert definition.version == 4


async def test_capture_writes_semantic_edges():
    backend = _make_backend(ENRICHMENT_PAYLOAD)

    graph = _make_graph()
    capture = SemanticCapture(backend=backend)
    _, commit = await capture.capture(CHANGE_EVENT, "Renaming for clarity.", graph)
    await commit()

    graph.upsert_semantic_edge.assert_awaited_once()
    edge: SemanticEdge = graph.upsert_semantic_edge.call_args.args[0]
    expected = ENRICHMENT_PAYLOAD["suggested_semantic_edges"][0]
    assert edge.from_entity_id == expected["from_entity_id"]
    assert edge.to_entity_id == expected["to_entity_id"]
    assert edge.type == EdgeType.DERIVED_FROM
    assert edge.description == expected["description"]
    assert edge.created_by == "llm"


async def test_capture_updates_change_event_status():
    backend = _make_backend(ENRICHMENT_PAYLOAD)

    graph = _make_graph()
    event = ChangeEvent(
        type=ChangeType.STRUCTURAL,
        source_entity_id="model.jaffle_shop.orders.order_date",
        change={"before": {"name": "order_date"}, "after": {"name": "recorded_date"}},
    )
    capture = SemanticCapture(backend=backend)
    _, commit = await capture.capture(event, "Renaming for clarity.", graph)
    await commit()

    assert event.status == ChangeStatus.CAPTURED
    assert event.semantic_context == ENRICHMENT_PAYLOAD["enriched_description"]
    graph.save_change_event.assert_awaited_once_with(event)


async def test_capture_no_semantic_edges_when_llm_returns_none():
    payload = {**ENRICHMENT_PAYLOAD, "suggested_semantic_edges": []}
    backend = _make_backend(payload)

    graph = _make_graph()
    capture = SemanticCapture(backend=backend)
    _, commit = await capture.capture(CHANGE_EVENT, "Renaming for clarity.", graph)
    await commit()

    graph.upsert_semantic_edge.assert_not_awaited()


async def test_capture_does_not_write_to_graph_before_commit():
    """KG writes must be deferred — graph must not be touched until commit() is called."""
    backend = _make_backend(ENRICHMENT_PAYLOAD)

    graph = _make_graph()
    capture = SemanticCapture(backend=backend)
    _, _commit = await capture.capture(CHANGE_EVENT, "Renaming for clarity.", graph)

    graph.upsert_definition.assert_not_awaited()
    graph.upsert_semantic_edge.assert_not_awaited()
    graph.save_change_event.assert_not_awaited()


# ── _enrich — JSON parsing ────────────────────────────────────────────────────

async def test_enrich_parses_plain_json():
    backend = _make_backend(ENRICHMENT_PAYLOAD)

    capture = SemanticCapture(backend=backend)
    result = await capture._enrich("some prompt")

    assert result["enriched_description"] == ENRICHMENT_PAYLOAD["enriched_description"]


async def test_enrich_strips_markdown_code_fence():
    backend = _make_backend_from(LLMResponse(
        text=f"```json\n{json.dumps(ENRICHMENT_PAYLOAD)}\n```", truncated=False,
    ))

    capture = SemanticCapture(backend=backend)
    result = await capture._enrich("some prompt")

    assert result["enriched_description"] == ENRICHMENT_PAYLOAD["enriched_description"]


async def test_enrich_strips_plain_code_fence():
    """Handles ``` without a language tag."""
    backend = _make_backend_from(LLMResponse(
        text=f"```\n{json.dumps(ENRICHMENT_PAYLOAD)}\n```", truncated=False,
    ))

    capture = SemanticCapture(backend=backend)
    result = await capture._enrich("some prompt")

    assert result["enriched_description"] == ENRICHMENT_PAYLOAD["enriched_description"]


# ── _build_user_message ───────────────────────────────────────────────────────

def test_build_user_message_includes_event_fields():
    msg = _build_user_message(
        event=CHANGE_EVENT,
        user_input="Renaming for clarity.",
        existing_definition=None,
        semantic_dependent_names=[],
        structural_dependent_names=[],
    )
    assert "order_date" in msg
    assert "recorded_date" in msg
    assert "structural" in msg
    assert "Renaming for clarity." in msg


def test_build_user_message_includes_existing_definition():
    msg = _build_user_message(
        event=CHANGE_EVENT,
        user_input="test",
        existing_definition="The date the order was placed.",
        semantic_dependent_names=[],
        structural_dependent_names=[],
    )
    assert "The date the order was placed." in msg
    assert "Existing definition" in msg


def test_build_user_message_includes_structural_dependents():
    msg = _build_user_message(
        event=CHANGE_EVENT,
        user_input="test",
        existing_definition=None,
        semantic_dependent_names=[],
        structural_dependent_names=["revenue", "monthly_summary"],
    )
    assert "revenue" in msg
    assert "monthly_summary" in msg
    assert "Structurally dependent" in msg


def test_build_user_message_includes_semantic_dependents():
    msg = _build_user_message(
        event=CHANGE_EVENT,
        user_input="test",
        existing_definition=None,
        semantic_dependent_names=["marketing_cost_ratio"],
        structural_dependent_names=[],
    )
    assert "marketing_cost_ratio" in msg
    assert "Semantically dependent" in msg


def test_build_user_message_omits_empty_sections():
    msg = _build_user_message(
        event=CHANGE_EVENT,
        user_input="test",
        existing_definition=None,
        semantic_dependent_names=[],
        structural_dependent_names=[],
    )
    assert "Existing definition" not in msg
    assert "Structurally dependent" not in msg
    assert "Semantically dependent" not in msg


# ── capture — graph context is passed to LLM ──────────────────────────────────

async def test_capture_includes_dependent_names_in_prompt():
    """Verifies that semantic/structural dependent names are included in the LLM prompt."""
    structural_dep = Entity(
        id="model.jaffle_shop.revenue",
        name="revenue",
        type=EntityType.MODEL,
        source_connector="dbt",
    )
    semantic_dep = Entity(
        id="metric.marketing_cost_ratio",
        name="marketing_cost_ratio",
        type=EntityType.METRIC,
        source_connector="dbt",
    )

    graph = _make_graph()
    graph.get_structural_dependents = AsyncMock(return_value=[structural_dep])
    graph.get_semantic_dependents = AsyncMock(return_value=[semantic_dep])

    captured_messages = []

    async def capture_call(**kwargs):
        captured_messages.append(kwargs["user_message"])
        return LLMResponse(text=json.dumps(ENRICHMENT_PAYLOAD), truncated=False)

    backend = _make_backend(ENRICHMENT_PAYLOAD)
    backend.complete = AsyncMock(side_effect=capture_call)

    capture = SemanticCapture(backend=backend)
    await capture.capture(CHANGE_EVENT, "test", graph)  # no need to commit for this assertion

    assert len(captured_messages) == 1
    prompt = captured_messages[0]
    assert "revenue" in prompt
    assert "marketing_cost_ratio" in prompt


# ── estimate_cost ─────────────────────────────────────────────────────────────

async def test_estimate_cost_returns_token_counts_and_cost():
    backend = _make_backend(ENRICHMENT_PAYLOAD)
    backend.count_input_tokens = AsyncMock(return_value=TokenCount(tokens=500, exact=True))

    capture = SemanticCapture(backend=backend)
    result = await capture.estimate_cost("some prompt")

    assert result["input_tokens"] == 500
    assert result["estimated_output_tokens"] > 0
    assert result["estimated_cost_usd"] > 0


async def test_estimate_cost_cost_is_sum_of_input_and_output():
    from puxti.core.capture import _ESTIMATED_OUTPUT_TOKENS

    backend = _make_backend(ENRICHMENT_PAYLOAD)
    backend.count_input_tokens = AsyncMock(
        return_value=TokenCount(tokens=1_000_000, exact=True)  # 1M tokens for easy math
    )

    capture = SemanticCapture(backend=backend)
    result = await capture.estimate_cost("some prompt")

    expected_input_cost = INPUT_COST_PER_MTOK  # 1M tokens × rate
    expected_output_cost = (_ESTIMATED_OUTPUT_TOKENS / 1_000_000) * OUTPUT_COST_PER_MTOK
    assert abs(result["estimated_cost_usd"] - (expected_input_cost + expected_output_cost)) < 0.0001


# ── _enrich — credit error handling ──────────────────────────────────────────

async def test_enrich_raises_runtime_error_on_credit_exhaustion():
    backend = _make_backend(ENRICHMENT_PAYLOAD)
    backend.complete = AsyncMock(side_effect=LLMBillingError(
        "Anthropic API credit balance is too low. "
        "Add credits at https://console.anthropic.com/settings/billing"
    ))

    capture = SemanticCapture(backend=backend)
    with pytest.raises(RuntimeError, match="credit balance"):
        await capture._enrich("some prompt")


# ── FEEDS producer inclusion ──────────────────────────────────────────────────

async def test_capture_includes_feeds_producers_in_affected_ids():
    airflow_task = Entity(
        id="task.airflow.salesforce_sync.extract_opportunities",
        name="extract_opportunities",
        type=EntityType.TASK,
        source_connector="airflow",
        project="salesforce_sync",
    )
    backend = _make_backend(ENRICHMENT_PAYLOAD)

    graph = _make_graph(feeds_producers=[airflow_task])
    capture = SemanticCapture(backend=backend)
    result, _ = await capture.capture(CHANGE_EVENT, "amount is now a roll-up.", graph)

    assert "task.airflow.salesforce_sync.extract_opportunities" in result.affected_entity_ids


async def test_capture_feeds_producers_not_duplicated():
    airflow_task = Entity(
        id="model.jaffle_shop.orders",  # already in LLM affected list
        name="orders",
        type=EntityType.MODEL,
        source_connector="dbt",
        project="jaffle_shop",
    )
    backend = _make_backend(ENRICHMENT_PAYLOAD)

    graph = _make_graph(feeds_producers=[airflow_task])
    capture = SemanticCapture(backend=backend)
    result, _ = await capture.capture(CHANGE_EVENT, "description.", graph)

    assert result.affected_entity_ids.count("model.jaffle_shop.orders") == 1


async def test_capture_no_feeds_producers_unaffected():
    backend = _make_backend(ENRICHMENT_PAYLOAD)

    graph = _make_graph(feeds_producers=[])
    capture = SemanticCapture(backend=backend)
    result, _ = await capture.capture(CHANGE_EVENT, "description.", graph)

    assert result.affected_entity_ids == ENRICHMENT_PAYLOAD["affected_entity_ids"]
