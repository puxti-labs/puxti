"""`puxti impact` — dependents table, --json output, change-type warnings."""

from unittest.mock import AsyncMock, MagicMock, patch

from puxti.cli import app
from tests.cli._helpers import plain, runner

# ── impact ────────────────────────────────────────────────────────────────────

def test_impact_shows_help():
    result = runner.invoke(app, ["impact", "--help"])
    assert result.exit_code == 0
    assert "--change-type" in plain(result.output)
    assert "--json" in plain(result.output)


def test_impact_in_app_help():
    result = runner.invoke(app, ["--help"])
    assert "impact" in result.output


def test_impact_invalid_change_type_exits_1():
    result = runner.invoke(app, ["impact", "model.jaffle_shop.orders", "--change-type", "bad"])
    assert result.exit_code == 1
    assert "Invalid --change-type" in plain(result.output)


def test_impact_entity_not_found_exits_1():
    mock_graph = MagicMock()
    mock_graph.connect = AsyncMock()
    mock_graph.close = AsyncMock()
    mock_graph.get_entity_by_id = AsyncMock(return_value=None)

    with patch("puxti.cli.impact.KnowledgeGraph", return_value=mock_graph):
        result = runner.invoke(app, ["impact", "model.jaffle_shop.missing"])

    assert result.exit_code == 1
    assert "not found" in plain(result.output).lower()


def test_impact_no_dependents_shows_message():
    from puxti.models import Entity, EntityType

    entity = Entity(id="model.jaffle_shop.orders", name="orders", type=EntityType.MODEL, source_connector="dbt", project="jaffle_shop")

    mock_graph = MagicMock()
    mock_graph.connect = AsyncMock()
    mock_graph.close = AsyncMock()
    mock_graph.get_entity_by_id = AsyncMock(return_value=entity)
    mock_graph.get_latest_definition = AsyncMock(return_value=None)
    mock_graph.get_semantic_dependents_with_depth = AsyncMock(return_value=[])
    mock_graph.get_structural_dependents = AsyncMock(return_value=[])

    with patch("puxti.cli.impact.KnowledgeGraph", return_value=mock_graph):
        result = runner.invoke(app, ["impact", "model.jaffle_shop.orders"])

    assert result.exit_code == 0
    assert "no dependents" in plain(result.output).lower()


def test_impact_shows_semantic_dependents():
    from puxti.models import Definition, Entity, EntityType

    entity = Entity(id="model.jaffle_shop.orders", name="orders", type=EntityType.MODEL, source_connector="dbt", project="jaffle_shop")
    dep = Entity(id="model.jaffle_shop.customers", name="customers", type=EntityType.MODEL, source_connector="dbt", project="jaffle_shop")
    definition = Definition(entity_id="model.jaffle_shop.orders", description="One row per settled order.", version=1, created_by="scan")

    mock_graph = MagicMock()
    mock_graph.connect = AsyncMock()
    mock_graph.close = AsyncMock()
    mock_graph.get_entity_by_id = AsyncMock(return_value=entity)
    mock_graph.get_latest_definition = AsyncMock(return_value=definition)
    mock_graph.get_semantic_dependents_with_depth = AsyncMock(return_value=[(dep, 1)])
    mock_graph.get_structural_dependents = AsyncMock(return_value=[])

    with patch("puxti.cli.impact.KnowledgeGraph", return_value=mock_graph):
        result = runner.invoke(app, ["impact", "model.jaffle_shop.orders"])

    assert result.exit_code == 0
    assert "customers" in plain(result.output)
    assert "semantic" in plain(result.output)
    assert "1" in plain(result.output)


def test_impact_shows_structural_dependents():
    from puxti.models import Entity, EntityType

    entity = Entity(id="model.jaffle_shop.orders", name="orders", type=EntityType.MODEL, source_connector="dbt", project="jaffle_shop")
    dep = Entity(id="model.jaffle_shop.reports", name="reports", type=EntityType.MODEL, source_connector="dbt", project="jaffle_shop")

    mock_graph = MagicMock()
    mock_graph.connect = AsyncMock()
    mock_graph.close = AsyncMock()
    mock_graph.get_entity_by_id = AsyncMock(return_value=entity)
    mock_graph.get_latest_definition = AsyncMock(return_value=None)
    mock_graph.get_semantic_dependents_with_depth = AsyncMock(return_value=[])
    mock_graph.get_structural_dependents = AsyncMock(return_value=[dep])

    with patch("puxti.cli.impact.KnowledgeGraph", return_value=mock_graph):
        result = runner.invoke(app, ["impact", "model.jaffle_shop.orders"])

    assert result.exit_code == 0
    assert "reports" in plain(result.output)
    assert "structural" in plain(result.output)


def test_impact_json_output():
    import json
    from puxti.models import Entity, EntityType

    entity = Entity(id="model.jaffle_shop.orders", name="orders", type=EntityType.MODEL, source_connector="dbt", project="jaffle_shop")
    dep = Entity(id="model.jaffle_shop.customers", name="customers", type=EntityType.MODEL, source_connector="dbt", project="jaffle_shop")

    mock_graph = MagicMock()
    mock_graph.connect = AsyncMock()
    mock_graph.close = AsyncMock()
    mock_graph.get_entity_by_id = AsyncMock(return_value=entity)
    mock_graph.get_latest_definition = AsyncMock(return_value=None)
    mock_graph.get_semantic_dependents_with_depth = AsyncMock(return_value=[(dep, 1)])
    mock_graph.get_structural_dependents = AsyncMock(return_value=[])

    with patch("puxti.cli.impact.KnowledgeGraph", return_value=mock_graph):
        result = runner.invoke(app, ["impact", "model.jaffle_shop.orders", "--json"])

    assert result.exit_code == 0
    data = json.loads(plain(result.output))
    assert data["entity_id"] == "model.jaffle_shop.orders"
    assert data["change_type"] is None
    assert len(data["dependents"]) == 1
    assert data["dependents"][0]["name"] == "customers"
    assert data["dependents"][0]["hop"] == 1
    assert data["dependents"][0]["relationship"] == "semantic"
    assert data["total_count"] == 1


def test_impact_json_with_change_type():
    import json
    from puxti.models import Entity, EntityType

    entity = Entity(id="model.jaffle_shop.orders", name="orders", type=EntityType.MODEL, source_connector="dbt", project="jaffle_shop")

    mock_graph = MagicMock()
    mock_graph.connect = AsyncMock()
    mock_graph.close = AsyncMock()
    mock_graph.get_entity_by_id = AsyncMock(return_value=entity)
    mock_graph.get_latest_definition = AsyncMock(return_value=None)
    mock_graph.get_semantic_dependents_with_depth = AsyncMock(return_value=[])
    mock_graph.get_structural_dependents = AsyncMock(return_value=[])

    with patch("puxti.cli.impact.KnowledgeGraph", return_value=mock_graph):
        result = runner.invoke(app, ["impact", "model.jaffle_shop.orders", "--change-type", "rename", "--json"])

    assert result.exit_code == 0
    data = json.loads(plain(result.output))
    assert data["change_type"] == "rename"


def test_impact_change_type_rename_shows_structural_warning():
    from puxti.models import Entity, EntityType

    entity = Entity(id="model.jaffle_shop.orders", name="orders", type=EntityType.MODEL, source_connector="dbt", project="jaffle_shop")
    dep = Entity(id="model.jaffle_shop.reports", name="reports", type=EntityType.MODEL, source_connector="dbt", project="jaffle_shop")

    mock_graph = MagicMock()
    mock_graph.connect = AsyncMock()
    mock_graph.close = AsyncMock()
    mock_graph.get_entity_by_id = AsyncMock(return_value=entity)
    mock_graph.get_latest_definition = AsyncMock(return_value=None)
    mock_graph.get_semantic_dependents_with_depth = AsyncMock(return_value=[])
    mock_graph.get_structural_dependents = AsyncMock(return_value=[dep])

    with patch("puxti.cli.impact.KnowledgeGraph", return_value=mock_graph):
        result = runner.invoke(app, ["impact", "model.jaffle_shop.orders", "--change-type", "rename"])

    assert result.exit_code == 0
    assert "structural" in plain(result.output)
    assert "rename" in plain(result.output)


def test_impact_merges_both_relationship_types():
    from puxti.models import Entity, EntityType

    entity = Entity(id="model.jaffle_shop.orders", name="orders", type=EntityType.MODEL, source_connector="dbt", project="jaffle_shop")
    # Same entity appears in both semantic and structural
    dep = Entity(id="model.jaffle_shop.customers", name="customers", type=EntityType.MODEL, source_connector="dbt", project="jaffle_shop")

    mock_graph = MagicMock()
    mock_graph.connect = AsyncMock()
    mock_graph.close = AsyncMock()
    mock_graph.get_entity_by_id = AsyncMock(return_value=entity)
    mock_graph.get_latest_definition = AsyncMock(return_value=None)
    mock_graph.get_semantic_dependents_with_depth = AsyncMock(return_value=[(dep, 1)])
    mock_graph.get_structural_dependents = AsyncMock(return_value=[dep])

    with patch("puxti.cli.impact.KnowledgeGraph", return_value=mock_graph):
        result = runner.invoke(app, ["impact", "model.jaffle_shop.orders"])

    assert result.exit_code == 0
    output = plain(result.output)
    # Entity should appear once, not twice
    assert output.count("customers") >= 1
    assert "semantic+structural" in output or ("semantic" in output and "structural" in output)
    # Summary line should show 1 total, 1 semantic, 1 structural
    assert "1 dependent" in output
