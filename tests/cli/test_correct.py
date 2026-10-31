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
    mock_corrector.apply_assessments = MagicMock(
        return_value=(
            [edge],  # updated_edges (kept as-is)
            [],  # removed_pairs
            [],  # updated_pairs
        )
    )

    with (
        patch("puxti.cli.correct.KnowledgeGraph", return_value=mock_graph),
        patch("puxti.cli.correct.SemanticCorrector", return_value=mock_corrector),
    ):
        result = runner.invoke(
            app,
            ["correct", "--entity", "model.jaffle_shop.orders"],
            # new definition → blank keeps edge unchanged → classify as real change
            # → blank declines the run-now handoff
            input="Orders excludes refunds.\n\nr\n\n",
        )

    assert result.exit_code == 0, result.output
    # No KG writes — real change belongs in redefine, not in the correction audit trail
    mock_graph.upsert_definition.assert_not_called()
    mock_graph.write_correction.assert_not_called()
    assert "redefine" in result.output.lower()


def test_correct_real_change_runs_redefine_after_explicit_confirmation():
    from puxti.models import Definition

    definition = Definition(
        entity_id="model.jaffle_shop.orders",
        description="Orders includes all transactions.",
        version=2,
        created_by="scan",
    )

    mock_graph = MagicMock()
    mock_graph.connect = AsyncMock()
    mock_graph.close = AsyncMock()
    mock_graph.get_latest_definition = AsyncMock(return_value=definition)
    mock_graph.get_entity_semantic_edges = AsyncMock(return_value=[])
    mock_graph.upsert_definition = AsyncMock()
    mock_graph.write_correction = AsyncMock()

    workspace = MagicMock()
    workspace.dbt.repo = "example/jaffle-shop"
    workspace.dbt.project_dir = "/workspace/dbt"
    workspace.dbt.base_branch = "develop"

    mock_redefine = AsyncMock()

    with (
        patch("puxti.cli.correct.KnowledgeGraph", return_value=mock_graph),
        patch("puxti.cli.correct._load_workspace", return_value=workspace),
        patch("puxti.cli.correct._run_redefine", mock_redefine),
    ):
        result = runner.invoke(
            app,
            ["correct", "--entity", "model.jaffle_shop.orders"],
            input="Orders excludes refunds.\nr\ny\n",
        )

    assert result.exit_code == 0, result.output
    mock_graph.upsert_definition.assert_not_called()
    mock_graph.write_correction.assert_not_called()
    mock_redefine.assert_awaited_once_with(
        entity="model.jaffle_shop.orders",
        description="Orders excludes refunds.",
        repo="example/jaffle-shop",
        base_branch="develop",
        dbt_project_dir="/workspace/dbt",
        dry_run=False,
    )


def _run_correct_into_real_redefine(input_):
    """`correct` → "Run it now?" → the real `_run_redefine`, with only I/O mocked.

    Returns (cli_result, redefine_graph_mock, github_mock).
    """
    from puxti.models import Definition, FileDiff, PropagationResult

    definition = Definition(
        entity_id="model.jaffle_shop.orders",
        description="Orders includes all transactions.",
        version=2,
        created_by="scan",
    )
    correct_graph = MagicMock()
    correct_graph.connect = AsyncMock()
    correct_graph.close = AsyncMock()
    correct_graph.get_latest_definition = AsyncMock(return_value=definition)
    correct_graph.get_entity_semantic_edges = AsyncMock(return_value=[])
    correct_graph.upsert_definition = AsyncMock()
    correct_graph.write_correction = AsyncMock()

    dep = MagicMock(id="model.jaffle_shop.customers", type=MagicMock(value="model"))
    dep.name = "customers"
    redefine_graph = MagicMock()
    redefine_graph.connect = AsyncMock()
    redefine_graph.close = AsyncMock()
    redefine_graph.get_latest_definition = AsyncMock(return_value=definition)
    redefine_graph.get_semantic_dependents_with_depth = AsyncMock(return_value=[(dep, 1)])
    redefine_graph.get_structural_ancestors = AsyncMock(return_value=[])
    redefine_graph.upsert_definition = AsyncMock()
    redefine_graph.save_change_event = AsyncMock()

    diff = FileDiff(
        file_path="models/customers.sql",
        before="select * from orders",
        after="select * from orders where not is_refund",
        connector="dbt",
        description="Semantic redefinition",
    )
    redefiner = MagicMock()
    redefiner.generate_passthrough_diffs = AsyncMock(return_value=[])
    redefiner.generate_diffs = AsyncMock(return_value=[diff])

    mock_gh = MagicMock()
    mock_gh.health_check = AsyncMock(return_value=True)
    mock_gh.open_pr = AsyncMock(return_value=PropagationResult(
        change_event_id="evt-001",
        connector="dbt",
        target_entity_id="model.jaffle_shop.orders",
        diffs=[diff],
        pr_url="https://github.com/example/jaffle-shop/pull/3",
        status="opened",
    ))

    workspace = MagicMock()
    workspace.dbt.repo = "example/jaffle-shop"
    workspace.dbt.project_dir = "/workspace/dbt"
    workspace.dbt.base_branch = "develop"

    with (
        patch("puxti.cli.correct.KnowledgeGraph", return_value=correct_graph),
        patch("puxti.cli.correct._load_workspace", return_value=workspace),
        patch("puxti.cli.redefine.settings") as redefine_settings,
        patch("puxti.cli.redefine.KnowledgeGraph", return_value=redefine_graph),
        patch("puxti.cli.redefine.SemanticRedefiner", return_value=redefiner),
        patch("puxti.cli.redefine.DbtConnector"),
        patch("puxti.cli.redefine.GitHubConnector", return_value=mock_gh),
    ):
        redefine_settings.github_token = "ghp_test"
        result = runner.invoke(
            app, ["correct", "--entity", "model.jaffle_shop.orders"], input=input_,
        )

    return result, redefine_graph, mock_gh


def test_correct_handoff_previews_diffs_and_asks_again_before_the_pr():
    """"Run it now?" approves running redefine; the PR still needs its own `y`
    after the user has seen the diffs."""
    result, redefine_graph, mock_gh = _run_correct_into_real_redefine(
        "Orders excludes refunds.\nr\ny\ny\n"
    )

    assert result.exit_code == 0, result.output
    output = plain(result.output)
    assert output.index("+select * from orders where not is_refund") < output.index(
        "Open 1 pull request?"
    )
    mock_gh.open_pr.assert_awaited_once()
    redefine_graph.upsert_definition.assert_awaited_once()


def test_correct_handoff_with_closed_input_cancels_cleanly():
    """A script that answered only "Run it now?" reaches the PR prompt at end of
    input: that is a clean cancel, not a crash."""
    result, redefine_graph, mock_gh = _run_correct_into_real_redefine(
        "Orders excludes refunds.\nr\ny\n"
    )

    assert result.exit_code == 0, result.output
    assert "No PR opened" in plain(result.output)
    assert "Unexpected error" not in result.output
    mock_gh.open_pr.assert_not_called()
    redefine_graph.upsert_definition.assert_not_called()


def test_correct_real_change_requires_repo_to_run_redefine_now():
    from puxti.models import Definition

    definition = Definition(
        entity_id="model.jaffle_shop.orders",
        description="Orders includes all transactions.",
        version=2,
        created_by="scan",
    )

    mock_graph = MagicMock()
    mock_graph.connect = AsyncMock()
    mock_graph.close = AsyncMock()
    mock_graph.get_latest_definition = AsyncMock(return_value=definition)
    mock_graph.get_entity_semantic_edges = AsyncMock(return_value=[])
    mock_graph.upsert_definition = AsyncMock()
    mock_graph.write_correction = AsyncMock()

    workspace = MagicMock()
    workspace.dbt = None

    mock_redefine = AsyncMock()

    with (
        patch("puxti.cli.correct.KnowledgeGraph", return_value=mock_graph),
        patch("puxti.cli.correct._load_workspace", return_value=workspace),
        patch("puxti.cli.correct._run_redefine", mock_redefine),
    ):
        result = runner.invoke(
            app,
            ["correct", "--entity", "model.jaffle_shop.orders"],
            input="Orders excludes refunds.\nr\ny\n",
        )

    assert result.exit_code == 1, result.output
    mock_graph.upsert_definition.assert_not_called()
    mock_graph.write_correction.assert_not_called()
    mock_redefine.assert_not_awaited()
    assert "--repo" in result.output


def test_correct_cancels_at_final_confirm():
    """User enters a new definition and classifies it, but cancels at the final
    confirm → nothing written."""
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
