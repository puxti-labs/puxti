"""Tests for the MCP server tool functions."""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── helpers ───────────────────────────────────────────────────────────────────


def _mock_entity(entity_id: str, name: str, etype: str = "model", project: str = "jaffle_shop"):
    e = MagicMock()
    e.id = entity_id
    e.name = name
    e.type = MagicMock(value=etype)
    e.source_connector = "dbt"
    e.project = project
    return e


def _mock_graph(entity=None, semantic_deps=None, structural_deps=None, definition=None, edges=None, history=None):
    g = AsyncMock()
    g.get_entity_by_id = AsyncMock(return_value=entity)
    g.get_semantic_dependents_with_depth = AsyncMock(return_value=semantic_deps or [])
    g.get_structural_dependents = AsyncMock(return_value=structural_deps or [])
    g.get_latest_definition = AsyncMock(return_value=definition)
    g.get_entity_semantic_edges = AsyncMock(return_value=edges or [])
    g.get_definition_history = AsyncMock(return_value=history or [])
    return g


# ── impact_of_change ──────────────────────────────────────────────────────────


async def test_impact_of_change_entity_not_found():
    graph = _mock_graph(entity=None)
    with patch("puxti.mcp_server._graph_connect", new=AsyncMock(return_value=graph)):
        from puxti.mcp_server import impact_of_change
        result = json.loads(await impact_of_change(entity_id="model.jaffle_shop.missing"))
    assert "error" in result


async def test_impact_of_change_no_dependents():
    entity = _mock_entity("model.jaffle_shop.orders", "orders")
    graph = _mock_graph(entity=entity)
    with patch("puxti.mcp_server._graph_connect", new=AsyncMock(return_value=graph)):
        from puxti.mcp_server import impact_of_change
        result = json.loads(await impact_of_change(entity_id="model.jaffle_shop.orders"))
    assert result["entity_id"] == "model.jaffle_shop.orders"
    assert result["total_count"] == 0
    assert result["dependents"] == []


async def test_impact_of_change_returns_semantic_dependents():
    entity = _mock_entity("model.jaffle_shop.orders", "orders")
    dep = _mock_entity("model.jaffle_shop.customers", "customers")
    graph = _mock_graph(entity=entity, semantic_deps=[(dep, 1)])
    with patch("puxti.mcp_server._graph_connect", new=AsyncMock(return_value=graph)):
        from puxti.mcp_server import impact_of_change
        result = json.loads(await impact_of_change(entity_id="model.jaffle_shop.orders"))
    assert result["total_count"] == 1
    assert result["dependents"][0]["name"] == "customers"
    assert result["dependents"][0]["hop"] == 1
    assert result["dependents"][0]["relationship"] == "semantic"


async def test_impact_of_change_merges_both_relationship_types():
    entity = _mock_entity("model.jaffle_shop.orders", "orders")
    dep = _mock_entity("model.jaffle_shop.customers", "customers")
    # Same entity in both semantic and structural
    graph = _mock_graph(entity=entity, semantic_deps=[(dep, 1)], structural_deps=[dep])
    with patch("puxti.mcp_server._graph_connect", new=AsyncMock(return_value=graph)):
        from puxti.mcp_server import impact_of_change
        result = json.loads(await impact_of_change(entity_id="model.jaffle_shop.orders"))
    assert result["total_count"] == 1
    assert "semantic" in result["dependents"][0]["relationship"]
    assert "structural" in result["dependents"][0]["relationship"]


async def test_impact_of_change_passes_change_type():
    entity = _mock_entity("model.jaffle_shop.orders", "orders")
    graph = _mock_graph(entity=entity)
    with patch("puxti.mcp_server._graph_connect", new=AsyncMock(return_value=graph)):
        from puxti.mcp_server import impact_of_change
        result = json.loads(await impact_of_change(entity_id="model.jaffle_shop.orders", change_type="rename"))
    assert result["change_type"] == "rename"


# ── consumers ────────────────────────────────────────────────────────────────


async def test_consumers_entity_not_found():
    graph = _mock_graph(entity=None)
    with patch("puxti.mcp_server._graph_connect", new=AsyncMock(return_value=graph)):
        from puxti.mcp_server import consumers
        result = json.loads(await consumers(entity_id="model.jaffle_shop.missing"))
    assert "error" in result


async def test_consumers_returns_structural_dependents():
    entity = _mock_entity("model.jaffle_shop.orders", "orders")
    dep = _mock_entity("model.jaffle_shop.reports", "reports")
    graph = _mock_graph(entity=entity, structural_deps=[dep])
    with patch("puxti.mcp_server._graph_connect", new=AsyncMock(return_value=graph)):
        from puxti.mcp_server import consumers
        result = json.loads(await consumers(entity_id="model.jaffle_shop.orders"))
    assert result["total_count"] == 1
    assert result["consumers"][0]["name"] == "reports"
    assert result["consumers"][0]["entity_id"] == "model.jaffle_shop.reports"


async def test_consumers_empty():
    entity = _mock_entity("model.jaffle_shop.orders", "orders")
    graph = _mock_graph(entity=entity)
    with patch("puxti.mcp_server._graph_connect", new=AsyncMock(return_value=graph)):
        from puxti.mcp_server import consumers
        result = json.loads(await consumers(entity_id="model.jaffle_shop.orders"))
    assert result["total_count"] == 0
    assert result["consumers"] == []


# ── definition_history ────────────────────────────────────────────────────────


async def test_definition_history_entity_not_found():
    graph = _mock_graph(entity=None)
    with patch("puxti.mcp_server._graph_connect", new=AsyncMock(return_value=graph)):
        from puxti.mcp_server import definition_history
        result = json.loads(await definition_history(entity_id="model.jaffle_shop.missing"))
    assert "error" in result


async def test_definition_history_no_definitions():
    entity = _mock_entity("model.jaffle_shop.orders", "orders")
    graph = _mock_graph(entity=entity, history=[])
    with patch("puxti.mcp_server._graph_connect", new=AsyncMock(return_value=graph)):
        from puxti.mcp_server import definition_history
        result = json.loads(await definition_history(entity_id="model.jaffle_shop.orders"))
    assert result["total_versions"] == 0
    assert result["history"] == []


async def test_definition_history_returns_all_versions():
    from datetime import datetime, timezone
    from puxti.models import Definition

    entity = _mock_entity("model.jaffle_shop.orders", "orders")
    v1 = Definition(entity_id="model.jaffle_shop.orders", description="First definition.", version=1, created_by="llm")
    v2 = Definition(entity_id="model.jaffle_shop.orders", description="Updated definition.", version=2, created_by="user")
    graph = _mock_graph(entity=entity, history=[v1, v2])
    with patch("puxti.mcp_server._graph_connect", new=AsyncMock(return_value=graph)):
        from puxti.mcp_server import definition_history
        result = json.loads(await definition_history(entity_id="model.jaffle_shop.orders"))
    assert result["total_versions"] == 2
    assert result["history"][0]["version"] == 1
    assert result["history"][0]["description"] == "First definition."
    assert result["history"][1]["version"] == 2
    assert result["history"][1]["created_by"] == "user"


# ── describe_entity ───────────────────────────────────────────────────────────


async def test_describe_entity_not_found():
    graph = _mock_graph(entity=None)
    with patch("puxti.mcp_server._graph_connect", new=AsyncMock(return_value=graph)):
        from puxti.mcp_server import describe_entity
        result = json.loads(await describe_entity(entity_id="model.jaffle_shop.missing"))
    assert "error" in result


async def test_describe_entity_no_definition():
    entity = _mock_entity("model.jaffle_shop.orders", "orders")
    graph = _mock_graph(entity=entity, definition=None, edges=[])
    with patch("puxti.mcp_server._graph_connect", new=AsyncMock(return_value=graph)):
        from puxti.mcp_server import describe_entity
        result = json.loads(await describe_entity(entity_id="model.jaffle_shop.orders"))
    assert result["name"] == "orders"
    assert result["definition"] is None
    assert result["semantic_edges"] == []


async def test_describe_entity_with_definition_and_edges():
    from puxti.models import Definition, EdgeType, SemanticEdge

    entity = _mock_entity("model.jaffle_shop.orders", "orders")
    definition = Definition(
        entity_id="model.jaffle_shop.orders",
        description="One row per settled order.",
        version=1,
        created_by="scan",
    )
    edge = SemanticEdge(
        from_entity_id="model.jaffle_shop.customers",
        to_entity_id="model.jaffle_shop.orders",
        type=EdgeType.DERIVED_FROM,
        description="customers derived from orders",
        created_by="scan",
    )
    graph = _mock_graph(entity=entity, definition=definition, edges=[edge])
    with patch("puxti.mcp_server._graph_connect", new=AsyncMock(return_value=graph)):
        from puxti.mcp_server import describe_entity
        result = json.loads(await describe_entity(entity_id="model.jaffle_shop.orders"))

    assert result["name"] == "orders"
    assert result["definition"]["description"] == "One row per settled order."
    assert result["definition"]["version"] == 1
    assert len(result["semantic_edges"]) == 1
    assert result["semantic_edges"][0]["direction"] == "incoming"
    assert result["semantic_edges"][0]["type"] == "derived_from"


async def test_describe_entity_outgoing_edge_direction():
    from puxti.models import EdgeType, SemanticEdge

    entity = _mock_entity("model.jaffle_shop.orders", "orders")
    edge = SemanticEdge(
        from_entity_id="model.jaffle_shop.orders",
        to_entity_id="model.jaffle_shop.customers",
        type=EdgeType.FEEDS,
        description="orders feeds customers",
        created_by="scan",
    )
    graph = _mock_graph(entity=entity, edges=[edge])
    with patch("puxti.mcp_server._graph_connect", new=AsyncMock(return_value=graph)):
        from puxti.mcp_server import describe_entity
        result = json.loads(await describe_entity(entity_id="model.jaffle_shop.orders"))
    assert result["semantic_edges"][0]["direction"] == "outgoing"
