"""Tests for SemanticCorrector — edge re-assessment and correction application."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from puxti.core.corrector import SemanticCorrector
from puxti.models import EdgeAssessment, EdgeType, Entity, EntityType, SemanticEdge


# ── Fixtures ──────────────────────────────────────────────────────────────────

ORDERS = Entity(
    id="model.demo_shop.orders",
    name="orders",
    type=EntityType.MODEL,
    source_connector="dbt",
)

REVENUE = Entity(
    id="model.demo_shop.gross_revenue",
    name="gross_revenue",
    type=EntityType.MODEL,
    source_connector="dbt",
)

CUSTOMERS = Entity(
    id="model.demo_shop.customers",
    name="customers",
    type=EntityType.MODEL,
    source_connector="dbt",
)

EDGE_ORDERS_REVENUE = SemanticEdge(
    from_entity_id=ORDERS.id,
    to_entity_id=REVENUE.id,
    type=EdgeType.DERIVED_FROM,
    description="gross_revenue is derived from orders",
    created_by="scan",
)

EDGE_CUSTOMERS_ORDERS = SemanticEdge(
    from_entity_id=CUSTOMERS.id,
    to_entity_id=ORDERS.id,
    type=EdgeType.DERIVED_FROM,
    description="customer metrics are derived from orders",
    created_by="scan",
)


def _mock_llm(payload: dict) -> MagicMock:
    content = MagicMock()
    content.text = json.dumps(payload)
    response = MagicMock()
    response.content = [content]
    return response


# ── reassess_edges ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_reassess_edges_returns_assessment_per_edge():
    client = MagicMock()
    client.messages = MagicMock()
    client.messages.create = AsyncMock(return_value=_mock_llm({
        "assessments": [
            {
                "from_entity_id": ORDERS.id,
                "to_entity_id": REVENUE.id,
                "action": "keep",
                "updated_description": None,
                "reasoning": "Still valid under new definition",
            },
            {
                "from_entity_id": CUSTOMERS.id,
                "to_entity_id": ORDERS.id,
                "action": "update",
                "updated_description": "customer metrics derived from settled orders only",
                "reasoning": "New definition excludes refunds so description needs updating",
            },
        ]
    }))

    corrector = SemanticCorrector(client=client)
    assessments = await corrector.reassess_edges(
        entity_id=ORDERS.id,
        old_definition="orders includes all transactions",
        new_definition="orders includes only settled transactions, excluding refunds",
        edges=[EDGE_ORDERS_REVENUE, EDGE_CUSTOMERS_ORDERS],
    )

    assert len(assessments) == 2
    keep = next(a for a in assessments if a.action == "keep")
    assert keep.from_entity_id == ORDERS.id
    assert keep.to_entity_id == REVENUE.id

    update = next(a for a in assessments if a.action == "update")
    assert update.updated_description == "customer metrics derived from settled orders only"


@pytest.mark.asyncio
async def test_reassess_edges_returns_empty_for_no_edges():
    client = MagicMock()
    corrector = SemanticCorrector(client=client)
    assessments = await corrector.reassess_edges(
        entity_id=ORDERS.id,
        old_definition="old",
        new_definition="new",
        edges=[],
    )
    assert assessments == []
    client.messages.create.assert_not_called()


@pytest.mark.asyncio
async def test_reassess_edges_defaults_to_keep_on_json_error():
    client = MagicMock()
    content = MagicMock()
    content.text = "not valid json {"
    response = MagicMock()
    response.content = [content]
    client.messages = MagicMock()
    client.messages.create = AsyncMock(return_value=response)

    corrector = SemanticCorrector(client=client)
    assessments = await corrector.reassess_edges(
        entity_id=ORDERS.id,
        old_definition="old",
        new_definition="new",
        edges=[EDGE_ORDERS_REVENUE],
    )

    assert len(assessments) == 1
    assert assessments[0].action == "keep"


@pytest.mark.asyncio
async def test_reassess_edges_defaults_unassessed_edges_to_keep():
    """LLM only returns one assessment — the other edge should default to keep."""
    client = MagicMock()
    client.messages = MagicMock()
    client.messages.create = AsyncMock(return_value=_mock_llm({
        "assessments": [
            {
                "from_entity_id": ORDERS.id,
                "to_entity_id": REVENUE.id,
                "action": "remove",
                "updated_description": None,
                "reasoning": "No longer relevant",
            }
        ]
    }))

    corrector = SemanticCorrector(client=client)
    assessments = await corrector.reassess_edges(
        entity_id=ORDERS.id,
        old_definition="old",
        new_definition="new",
        edges=[EDGE_ORDERS_REVENUE, EDGE_CUSTOMERS_ORDERS],
    )

    assert len(assessments) == 2
    kept = [a for a in assessments if a.action == "keep"]
    assert len(kept) == 1
    assert kept[0].from_entity_id == CUSTOMERS.id


# ── apply_assessments ─────────────────────────────────────────────────────────

def test_apply_assessments_keep():
    corrector = SemanticCorrector(client=MagicMock())
    assessments = [
        EdgeAssessment(
            from_entity_id=ORDERS.id,
            to_entity_id=REVENUE.id,
            action="keep",
            reasoning="still valid",
        )
    ]
    updated_edges, removed_pairs, updated_pairs = corrector.apply_assessments(
        [EDGE_ORDERS_REVENUE], assessments
    )
    assert len(updated_edges) == 1
    assert updated_edges[0].description == EDGE_ORDERS_REVENUE.description
    assert removed_pairs == []
    assert updated_pairs == []


def test_apply_assessments_remove():
    corrector = SemanticCorrector(client=MagicMock())
    assessments = [
        EdgeAssessment(
            from_entity_id=ORDERS.id,
            to_entity_id=REVENUE.id,
            action="remove",
            reasoning="no longer valid",
        )
    ]
    updated_edges, removed_pairs, updated_pairs = corrector.apply_assessments(
        [EDGE_ORDERS_REVENUE], assessments
    )
    assert updated_edges == []
    assert removed_pairs == [(ORDERS.id, REVENUE.id)]
    assert updated_pairs == []


def test_apply_assessments_update():
    corrector = SemanticCorrector(client=MagicMock())
    new_desc = "gross_revenue derived from settled orders only"
    assessments = [
        EdgeAssessment(
            from_entity_id=ORDERS.id,
            to_entity_id=REVENUE.id,
            action="update",
            updated_description=new_desc,
            reasoning="description needs updating",
        )
    ]
    updated_edges, removed_pairs, updated_pairs = corrector.apply_assessments(
        [EDGE_ORDERS_REVENUE], assessments
    )
    assert len(updated_edges) == 1
    assert updated_edges[0].description == new_desc
    assert updated_edges[0].created_by == "correct"
    assert removed_pairs == []
    assert updated_pairs == [(ORDERS.id, REVENUE.id)]


def test_apply_assessments_mixed():
    corrector = SemanticCorrector(client=MagicMock())
    assessments = [
        EdgeAssessment(
            from_entity_id=ORDERS.id,
            to_entity_id=REVENUE.id,
            action="remove",
            reasoning="no longer valid",
        ),
        EdgeAssessment(
            from_entity_id=CUSTOMERS.id,
            to_entity_id=ORDERS.id,
            action="keep",
            reasoning="still valid",
        ),
    ]
    updated_edges, removed_pairs, updated_pairs = corrector.apply_assessments(
        [EDGE_ORDERS_REVENUE, EDGE_CUSTOMERS_ORDERS], assessments
    )
    assert len(updated_edges) == 1
    assert updated_edges[0].from_entity_id == CUSTOMERS.id
    assert removed_pairs == [(ORDERS.id, REVENUE.id)]
    assert updated_pairs == []
