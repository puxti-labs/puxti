"""Tests for the MCP server tool functions."""
import json
from unittest.mock import AsyncMock, MagicMock, patch

from puxti.models import EntityStatus

# ── helpers ───────────────────────────────────────────────────────────────────


def _mock_entity(entity_id: str, name: str, etype: str = "model", project: str = "jaffle_shop",
                 status: EntityStatus = EntityStatus.BOUND):
    e = MagicMock()
    e.id = entity_id
    e.name = name
    e.type = MagicMock(value=etype)
    e.source_connector = "dbt"
    e.project = project
    e.status = status
    return e


def _mock_graph(
    entity=None, semantic_deps=None, structural_deps=None, definition=None, edges=None,
    history=None,
):
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
        result = json.loads(
            await impact_of_change(entity_id="model.jaffle_shop.orders", change_type="rename")
        )
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
    from puxti.models import Definition

    entity = _mock_entity("model.jaffle_shop.orders", "orders")
    v1 = Definition(
        entity_id="model.jaffle_shop.orders", description="First definition.",
        version=1, created_by="llm",
    )
    v2 = Definition(
        entity_id="model.jaffle_shop.orders", description="Updated definition.",
        version=2, created_by="user",
    )
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
    # created_at is returned so an agent can cite freshness in one call (provenance footer).
    assert "created_at" in result["definition"]
    assert result["definition"]["created_at"]
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


# ── proposed metrics are not reported as facts ────────────────────────────────

async def test_describe_entity_proposed_is_flagged_not_bound():
    from puxti.models import Definition

    entity = _mock_entity("metric.proposed.nrr", "nrr", etype="metric",
                          status=EntityStatus.PROPOSED)
    definition = Definition(
        entity_id="metric.proposed.nrr",
        description="Net revenue retention, net of churn.",
        version=1,
        created_by="user",
    )
    graph = _mock_graph(entity=entity, definition=definition, edges=[])
    with patch("puxti.mcp_server._graph_connect", new=AsyncMock(return_value=graph)):
        from puxti.mcp_server import describe_entity
        result = json.loads(await describe_entity(entity_id="metric.proposed.nrr"))
    assert result["bound"] is False
    assert result["status"] == "proposed"
    assert "Do NOT report a value" in result["note"]
    # The definition is still returned — as intent, not as a fact.
    assert result["definition"]["description"] == "Net revenue retention, net of churn."


async def test_impact_of_change_proposed_returns_marker():
    entity = _mock_entity("metric.proposed.nrr", "nrr", etype="metric",
                          status=EntityStatus.PROPOSED)
    graph = _mock_graph(entity=entity)
    with patch("puxti.mcp_server._graph_connect", new=AsyncMock(return_value=graph)):
        from puxti.mcp_server import impact_of_change
        result = json.loads(await impact_of_change(entity_id="metric.proposed.nrr"))
    assert result["proposed"] is True
    assert result["dependents"] == []


# ── search_entities ───────────────────────────────────────────────────────────


async def _search_graph():
    """A real in-memory graph, so the tool is exercised against actual SQL."""
    from pathlib import Path

    from puxti.core.graph import KnowledgeGraph
    from puxti.models import Definition, Entity, EntityType

    graph = KnowledgeGraph(db_path=Path(":memory:"))
    await graph.connect()
    for name in ("orders", "stg_orders", "customers"):
        await graph.upsert_entity(
            Entity(id=f"model.jaffle_shop.{name}", name=name, type=EntityType.MODEL,
                   source_connector="dbt", project="jaffle_shop")
        )
    await graph.upsert_entity(
        Entity(id="metric.jaffle_shop.average_order_value", name="average_order_value",
               type=EntityType.METRIC, source_connector="user", project="jaffle_shop",
               status=EntityStatus.PROPOSED)
    )
    await graph.upsert_definition(
        Definition(entity_id="metric.jaffle_shop.average_order_value", version=1,
                   created_by="user", description="\nRevenue per order.\nExcludes refunds.")
    )
    return graph


async def test_search_entities_returns_metric_with_first_definition_line():
    graph = await _search_graph()
    with patch("puxti.mcp_server._graph_connect", new=AsyncMock(return_value=graph)):
        from puxti.mcp_server import search_entities
        result = json.loads(await search_entities(query="average order"))
    await graph.close()
    assert result["total_count"] == 1
    hit = result["results"][0]
    assert hit == {
        "entity_id": "metric.jaffle_shop.average_order_value",
        "name": "average_order_value",
        "type": "metric",
        "project": "jaffle_shop",
        "status": "proposed",
        "definition": "Revenue per order.",
    }


async def test_search_entities_type_filter_ranks_exact_name_first():
    graph = await _search_graph()
    with patch("puxti.mcp_server._graph_connect", new=AsyncMock(return_value=graph)):
        from puxti.mcp_server import search_entities
        result = json.loads(await search_entities(query="orders", type="model"))
    await graph.close()
    assert result["type"] == "model"
    assert [r["entity_id"] for r in result["results"]] == [
        "model.jaffle_shop.orders", "model.jaffle_shop.stg_orders",
    ]
    assert result["results"][0]["definition"] is None


async def test_search_entities_unknown_type_is_an_error():
    graph = _mock_graph()
    with patch("puxti.mcp_server._graph_connect", new=AsyncMock(return_value=graph)):
        from puxti.mcp_server import search_entities
        result = json.loads(await search_entities(query="orders", type="widget"))
    assert "widget" in result["error"]
    assert "metric" in result["error"]
    graph.search_entities.assert_not_called()


async def test_search_entities_clamps_limit():
    graph = _mock_graph()
    graph.search_entities = AsyncMock(return_value=[])
    with patch("puxti.mcp_server._graph_connect", new=AsyncMock(return_value=graph)):
        from puxti.mcp_server import search_entities
        await search_entities(query="orders", limit=10_000)
        await search_entities(query="orders", limit=0)
    # The tool asks for one row beyond the clamped limit to detect truncation.
    limits = [c.kwargs["limit"] for c in graph.search_entities.call_args_list]
    assert limits == [101, 2]


async def test_search_entities_flags_truncated_results():
    graph = await _search_graph()
    with patch("puxti.mcp_server._graph_connect", new=AsyncMock(return_value=graph)):
        from puxti.mcp_server import search_entities
        cut = json.loads(await search_entities(query="jaffle_shop", limit=2))
        full = json.loads(await search_entities(query="jaffle_shop", limit=4))
    await graph.close()
    assert cut["total_count"] == 2
    assert cut["truncated"] is True
    assert full["total_count"] == 4
    assert full["truncated"] is False
