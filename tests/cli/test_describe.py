"""`puxti describe` — overview, project filter, single-entity detail."""

from unittest.mock import AsyncMock, MagicMock, patch

from puxti.cli import app
from tests.cli._helpers import plain, runner

# ── describe — help and argument validation ───────────────────────────────────

def test_describe_shows_help():
    result = runner.invoke(app, ["describe", "--help"])
    assert result.exit_code == 0
    assert "--entity" in plain(result.output)


def test_describe_empty_graph_shows_message():
    mock_graph = MagicMock()
    mock_graph.connect = AsyncMock()
    mock_graph.close = AsyncMock()
    mock_graph.get_all_entities_with_definitions = AsyncMock(return_value=[])
    mock_graph.get_all_semantic_edges = AsyncMock(return_value=[])

    with patch("puxti.cli.describe.KnowledgeGraph", return_value=mock_graph):
        result = runner.invoke(app, ["describe"])

    assert result.exit_code == 0
    assert "empty" in result.output.lower() or "scan" in result.output.lower()


def test_describe_overview_shows_entities():
    from puxti.models import Definition, Entity, EntityType

    entity = Entity(
        id="model.jaffle_shop.orders",
        name="orders",
        type=EntityType.MODEL,
        source_connector="dbt",
        project="jaffle_shop",
    )
    definition = Definition(
        entity_id="model.jaffle_shop.orders",
        description="One row per settled order.",
        version=1,
        created_by="scan",
    )

    mock_graph = MagicMock()
    mock_graph.connect = AsyncMock()
    mock_graph.close = AsyncMock()
    mock_graph.get_all_entities_with_definitions = AsyncMock(return_value=[(entity, definition)])
    mock_graph.get_all_semantic_edges = AsyncMock(return_value=[])

    with patch("puxti.cli.describe.KnowledgeGraph", return_value=mock_graph):
        result = runner.invoke(app, ["describe"])

    assert result.exit_code == 0
    assert "orders" in result.output
    assert "jaffle_shop" in result.output
    assert "One row per settled order" in result.output


def test_describe_project_filter_shows_only_matching_project():
    from puxti.models import Definition, Entity, EntityType

    entity_a = Entity(id="model.proj_a.orders", name="orders", type=EntityType.MODEL, source_connector="dbt", project="proj_a")
    entity_b = Entity(id="model.proj_b.sales", name="sales", type=EntityType.MODEL, source_connector="dbt", project="proj_b")
    def_a = Definition(entity_id="model.proj_a.orders", description="Orders for project A.", version=1, created_by="scan")
    def_b = Definition(entity_id="model.proj_b.sales", name="sales", description="Sales for project B.", version=1, created_by="scan")

    mock_graph = MagicMock()
    mock_graph.connect = AsyncMock()
    mock_graph.close = AsyncMock()
    mock_graph.get_all_entities_with_definitions = AsyncMock(return_value=[(entity_a, def_a), (entity_b, def_b)])
    mock_graph.get_all_semantic_edges = AsyncMock(return_value=[])

    with patch("puxti.cli.describe.KnowledgeGraph", return_value=mock_graph):
        result = runner.invoke(app, ["describe", "--project", "proj_a"])

    assert result.exit_code == 0
    assert "orders" in result.output
    assert "Orders for project A" in result.output
    assert "sales" not in result.output
    assert "proj_b" not in result.output


def test_describe_project_filter_exits_when_project_not_found():
    from puxti.models import Definition, Entity, EntityType

    entity = Entity(id="model.proj_a.orders", name="orders", type=EntityType.MODEL, source_connector="dbt", project="proj_a")
    definition = Definition(entity_id="model.proj_a.orders", description="Orders.", version=1, created_by="scan")

    mock_graph = MagicMock()
    mock_graph.connect = AsyncMock()
    mock_graph.close = AsyncMock()
    mock_graph.get_all_entities_with_definitions = AsyncMock(return_value=[(entity, definition)])
    mock_graph.get_all_semantic_edges = AsyncMock(return_value=[])

    with patch("puxti.cli.describe.KnowledgeGraph", return_value=mock_graph):
        result = runner.invoke(app, ["describe", "--project", "nonexistent"])

    assert result.exit_code == 1


def test_describe_single_entity_not_found_exits_nonzero():
    mock_graph = MagicMock()
    mock_graph.connect = AsyncMock()
    mock_graph.close = AsyncMock()
    mock_graph.get_entity_by_id = AsyncMock(return_value=None)

    with patch("puxti.cli.describe.KnowledgeGraph", return_value=mock_graph):
        result = runner.invoke(app, ["describe", "--entity", "model.jaffle_shop.missing"])

    assert result.exit_code == 1


def test_describe_single_entity_shows_definition_and_edges():
    from puxti.models import Definition, EdgeType, Entity, EntityType, SemanticEdge

    entity = Entity(
        id="model.jaffle_shop.orders",
        name="orders",
        type=EntityType.MODEL,
        source_connector="dbt",
        project="jaffle_shop",
    )
    definition = Definition(
        entity_id="model.jaffle_shop.orders",
        description="One row per settled order.",
        version=2,
        created_by="correct",
    )
    edge = SemanticEdge(
        from_entity_id="model.jaffle_shop.customers",
        to_entity_id="model.jaffle_shop.orders",
        type=EdgeType.DERIVED_FROM,
        description="customer metrics derived from orders",
        created_by="scan",
    )

    mock_graph = MagicMock()
    mock_graph.connect = AsyncMock()
    mock_graph.close = AsyncMock()
    mock_graph.get_entity_by_id = AsyncMock(return_value=entity)
    mock_graph.get_latest_definition = AsyncMock(return_value=definition)
    mock_graph.get_entity_semantic_edges = AsyncMock(return_value=[edge])

    with patch("puxti.cli.describe.KnowledgeGraph", return_value=mock_graph):
        result = runner.invoke(app, ["describe", "--entity", "model.jaffle_shop.orders"])

    assert result.exit_code == 0
    assert "One row per settled order" in result.output
    assert "derived_from" in result.output
    assert "customers" in result.output
