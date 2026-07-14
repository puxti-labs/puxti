"""`puxti correct` — definition correction flow and classification."""

from unittest.mock import AsyncMock, MagicMock, patch

from puxti.cli import app
from tests.cli._helpers import plain, runner

# ── correct — help and error paths ───────────────────────────────────────────

def test_correct_shows_help():
    result = runner.invoke(app, ["correct", "--help"])
    assert result.exit_code == 0
    assert "--entity" in plain(result.output)


def test_correct_exits_when_entity_not_found():
    mock_graph = MagicMock()
    mock_graph.connect = AsyncMock()
    mock_graph.close = AsyncMock()
    mock_graph.get_latest_definition = AsyncMock(return_value=None)

    with patch("puxti.cli.correct.KnowledgeGraph", return_value=mock_graph):
        result = runner.invoke(app, ["correct", "--entity", "model.jaffle_shop.missing"])

    assert result.exit_code == 1
    assert "no definition" in result.output.lower() or "not found" in result.output.lower()


def test_correct_cancels_on_blank_definition():
    from puxti.models import Definition

    definition = Definition(
        entity_id="model.jaffle_shop.orders",
        description="Old definition.",
        version=1,
        created_by="scan",
    )

    mock_graph = MagicMock()
    mock_graph.connect = AsyncMock()
    mock_graph.close = AsyncMock()
    mock_graph.get_latest_definition = AsyncMock(return_value=definition)
    mock_graph.get_entity_semantic_edges = AsyncMock(return_value=[])

    with patch("puxti.cli.correct.KnowledgeGraph", return_value=mock_graph):
        # Blank input → cancel
        result = runner.invoke(app, ["correct", "--entity", "model.jaffle_shop.orders"], input="\n")

    assert result.exit_code == 0
    assert "cancelled" in result.output.lower()
    mock_graph.upsert_definition.assert_not_called()


def test_correct_cancels_when_definition_unchanged():
    from puxti.models import Definition

    definition = Definition(
        entity_id="model.jaffle_shop.orders",
        description="Exactly this definition.",
        version=1,
        created_by="scan",
    )

    mock_graph = MagicMock()
    mock_graph.connect = AsyncMock()
    mock_graph.close = AsyncMock()
    mock_graph.get_latest_definition = AsyncMock(return_value=definition)
    mock_graph.get_entity_semantic_edges = AsyncMock(return_value=[])

    with patch("puxti.cli.correct.KnowledgeGraph", return_value=mock_graph):
        result = runner.invoke(
            app,
            ["correct", "--entity", "model.jaffle_shop.orders"],
            input="Exactly this definition.\n",
        )

    assert result.exit_code == 0
    assert "unchanged" in result.output.lower()
    mock_graph.upsert_definition.assert_not_called()


def test_correct_project_flag_rejects_wrong_project():
    from puxti.models import Definition, Entity, EntityType

    definition = Definition(
        entity_id="model.jaffle_shop.orders",
        description="Orders model.",
        version=1,
        created_by="scan",
    )
    entity_obj = Entity(
        id="model.jaffle_shop.orders",
        name="orders",
        type=EntityType.MODEL,
        project="jaffle_shop",
        source_connector="dbt",
    )

    mock_graph = MagicMock()
    mock_graph.connect = AsyncMock()
    mock_graph.close = AsyncMock()
    mock_graph.get_latest_definition = AsyncMock(return_value=definition)
    mock_graph.get_entity_by_id = AsyncMock(return_value=entity_obj)

    with patch("puxti.cli.correct.KnowledgeGraph", return_value=mock_graph):
        result = runner.invoke(
            app,
            ["correct", "--entity", "model.jaffle_shop.orders", "--project", "other_project"],
        )

    assert result.exit_code == 1
    assert "other_project" in result.output or "jaffle_shop" in result.output


def test_correct_project_flag_passes_matching_project():
    from puxti.models import Definition, Entity, EntityType

    definition = Definition(
        entity_id="model.jaffle_shop.orders",
        description="Orders model.",
        version=1,
        created_by="scan",
    )
    entity_obj = Entity(
        id="model.jaffle_shop.orders",
        name="orders",
        type=EntityType.MODEL,
        project="jaffle_shop",
        source_connector="dbt",
    )

    mock_graph = MagicMock()
    mock_graph.connect = AsyncMock()
    mock_graph.close = AsyncMock()
    mock_graph.get_latest_definition = AsyncMock(return_value=definition)
    mock_graph.get_entity_by_id = AsyncMock(return_value=entity_obj)
    mock_graph.get_entity_semantic_edges = AsyncMock(return_value=[])

    with patch("puxti.cli.correct.KnowledgeGraph", return_value=mock_graph):
        # Blank input to cancel after project validation passes
        result = runner.invoke(
            app,
            ["correct", "--entity", "model.jaffle_shop.orders", "--project", "jaffle_shop"],
            input="\n",
        )

    assert result.exit_code == 0
    assert "cancelled" in result.output.lower()


def test_correct_happy_path_no_edges_classified_as_correction():
    """New definition, no edges, classified as correction → writes definition + correction event."""
    from puxti.models import Definition

    definition = Definition(
        entity_id="model.jaffle_shop.orders",
        description="Old inaccurate definition.",
        version=1,
        created_by="scan",
    )

    mock_graph = MagicMock()
    mock_graph.connect = AsyncMock()
    mock_graph.close = AsyncMock()
    mock_graph.get_latest_definition = AsyncMock(return_value=definition)
    mock_graph.get_entity_semantic_edges = AsyncMock(return_value=[])
    mock_graph.upsert_definition = AsyncMock()
    mock_graph.write_correction = AsyncMock()

    mock_corrector = MagicMock()
    mock_corrector.apply_assessments = MagicMock(return_value=([], [], []))

    with (
        patch("puxti.cli.correct.KnowledgeGraph", return_value=mock_graph),
        patch("puxti.cli.correct.SemanticCorrector", return_value=mock_corrector),
    ):
        result = runner.invoke(
            app,
            ["correct", "--entity", "model.jaffle_shop.orders"],
            # new definition → classify as correction → confirm write
            input="Corrected accurate definition.\nc\ny\n",
        )

    assert result.exit_code == 0, result.output
    mock_graph.upsert_definition.assert_called_once()
    written_def = mock_graph.upsert_definition.call_args[0][0]
    assert written_def.description == "Corrected accurate definition."
    assert written_def.version == 2
    assert written_def.created_by == "correct"

    mock_graph.write_correction.assert_called_once()
    correction_event = mock_graph.write_correction.call_args[0][0]
    assert correction_event.classified_as == "correction"
    assert correction_event.entity_id == "model.jaffle_shop.orders"
    assert "Correction written" in result.output


def test_correct_real_change_does_not_write_to_kg():
    """classified as real_change → no KG writes at all, prints redefine handoff command."""
    from puxti.models import Definition, EdgeAssessment, EdgeType, SemanticEdge

    definition = Definition(
        entity_id="model.jaffle_shop.orders",
        description="Orders includes all transactions.",
        version=2,
        created_by="scan",
    )
    edge = SemanticEdge(
        from_entity_id="model.jaffle_shop.revenue",
        to_entity_id="model.jaffle_shop.orders",
        type=EdgeType.DERIVED_FROM,
        description="revenue derived from orders",
        created_by="scan",
    )

    mock_graph = MagicMock()
    mock_graph.connect = AsyncMock()
    mock_graph.close = AsyncMock()
    mock_graph.get_latest_definition = AsyncMock(return_value=definition)
    mock_graph.get_entity_semantic_edges = AsyncMock(return_value=[edge])
    mock_graph.upsert_definition = AsyncMock()
    mock_graph.write_correction = AsyncMock()

    assessment = EdgeAssessment(
        from_entity_id="model.jaffle_shop.revenue",
        to_entity_id="model.jaffle_shop.orders",
        action="keep",
        reasoning="Still valid.",
    )
    mock_corrector = MagicMock()
    mock_corrector.reassess_edges = AsyncMock(return_value=[assessment])
    mock_corrector.apply_assessments = MagicMock(return_value=(
        [edge],   # updated_edges (kept as-is)
        [],       # removed_pairs
        [],       # updated_pairs
    ))

    with (
        patch("puxti.cli.correct.KnowledgeGraph", return_value=mock_graph),
        patch("puxti.cli.correct.SemanticCorrector", return_value=mock_corrector),
    ):
        result = runner.invoke(
            app,
            ["correct", "--entity", "model.jaffle_shop.orders"],
            # new definition → accept edge assessment (blank=accept) → classify as real change
            input="Orders excludes refunds.\n\nr\n",
        )

    assert result.exit_code == 0, result.output
    # No KG writes — real change belongs in redefine, not in the correction audit trail
    mock_graph.upsert_definition.assert_not_called()
    mock_graph.write_correction.assert_not_called()
    assert "redefine" in result.output.lower()


def test_correct_cancels_at_final_confirm():
    """User enters a new definition and classifies it, but cancels at the final confirm → nothing written."""
    from puxti.models import Definition

    definition = Definition(
        entity_id="model.jaffle_shop.orders",
        description="Old definition.",
        version=1,
        created_by="scan",
    )

    mock_graph = MagicMock()
    mock_graph.connect = AsyncMock()
    mock_graph.close = AsyncMock()
    mock_graph.get_latest_definition = AsyncMock(return_value=definition)
    mock_graph.get_entity_semantic_edges = AsyncMock(return_value=[])
    mock_graph.upsert_definition = AsyncMock()
    mock_graph.write_correction = AsyncMock()

    mock_corrector = MagicMock()
    mock_corrector.apply_assessments = MagicMock(return_value=([], [], []))

    with (
        patch("puxti.cli.correct.KnowledgeGraph", return_value=mock_graph),
        patch("puxti.cli.correct.SemanticCorrector", return_value=mock_corrector),
    ):
        result = runner.invoke(
            app,
            ["correct", "--entity", "model.jaffle_shop.orders"],
            # new definition → classify as correction → cancel at final confirm
            input="New definition here.\nc\nn\n",
        )

    assert result.exit_code == 0, result.output
    mock_graph.upsert_definition.assert_not_called()
    mock_graph.write_correction.assert_not_called()
    assert "cancelled" in result.output.lower()


def test_correct_blank_at_edge_assessment_keeps_edge_unchanged():
    """Blank input at the edge-assessment prompt keeps the edge — it must not
    silently apply the LLM's suggestion."""
    from puxti.models import Definition, EdgeAssessment, EdgeType, SemanticEdge

    definition = Definition(
        entity_id="model.jaffle_shop.orders",
        description="Old definition.",
        version=1,
        created_by="scan",
    )
    edge = SemanticEdge(
        from_entity_id="model.jaffle_shop.revenue",
        to_entity_id="model.jaffle_shop.orders",
        type=EdgeType.DERIVED_FROM,
        description="revenue derived from orders",
        created_by="scan",
    )

    mock_graph = MagicMock()
    mock_graph.connect = AsyncMock()
    mock_graph.close = AsyncMock()
    mock_graph.get_latest_definition = AsyncMock(return_value=definition)
    mock_graph.get_entity_semantic_edges = AsyncMock(return_value=[edge])
    mock_graph.upsert_definition = AsyncMock()
    mock_graph.write_correction = AsyncMock()

    # LLM suggests removing the edge — a destructive action
    suggestion = EdgeAssessment(
        from_entity_id="model.jaffle_shop.revenue",
        to_entity_id="model.jaffle_shop.orders",
        action="remove",
        reasoning="No longer related.",
    )
    mock_corrector = MagicMock()
    mock_corrector.reassess_edges = AsyncMock(return_value=[suggestion])
    mock_corrector.apply_assessments = MagicMock(return_value=([edge], [], []))

    with (
        patch("puxti.cli.correct.KnowledgeGraph", return_value=mock_graph),
        patch("puxti.cli.correct.SemanticCorrector", return_value=mock_corrector),
    ):
        result = runner.invoke(
            app,
            ["correct", "--entity", "model.jaffle_shop.orders"],
            # new definition → BLANK at assessment → correction → confirm write
            input="New definition.\n\nc\ny\n",
        )

    assert result.exit_code == 0, result.output
    confirmed = mock_corrector.apply_assessments.call_args[0][1]
    assert len(confirmed) == 1
    assert confirmed[0].action == "keep"  # not the suggested "remove"
    assert "kept unchanged" in confirmed[0].reasoning
