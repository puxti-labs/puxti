"""`puxti redefine` — validation, dependents, dry-run, PR-failure regression."""

from unittest.mock import AsyncMock, MagicMock, patch

from puxti.cli import app
from tests.cli._helpers import plain, runner

# ── redefine ──────────────────────────────────────────────────────────────────

def test_redefine_shows_help():
    result = runner.invoke(app, ["redefine", "--help"])
    assert result.exit_code == 0
    assert "--entity" in plain(result.output)
    assert "--description" in plain(result.output)
    assert "--repo" in plain(result.output)


def test_redefine_exits_when_no_dbt_project_dir():
    with patch("puxti.cli.redefine.settings") as mock_settings:
        mock_settings.dbt_project_dir = None
        mock_settings.github_token = "ghp_test"
        result = runner.invoke(app, [
            "redefine",
            "--entity", "model.demo_shop.orders.gross_revenue",
            "--description", "now excludes refunds",
            "--repo", "acme/data",
        ])
    assert result.exit_code == 1


def test_redefine_exits_when_no_github_token():
    with patch("puxti.cli.redefine.settings") as mock_settings:
        mock_settings.dbt_project_dir = "/some/dbt"
        mock_settings.github_token = None
        result = runner.invoke(app, [
            "redefine",
            "--entity", "model.demo_shop.orders.gross_revenue",
            "--description", "now excludes refunds",
            "--repo", "acme/data",
        ])
    assert result.exit_code == 1
    assert "github token" in result.output.lower() or "GITHUB_TOKEN" in result.output


def test_redefine_exits_cleanly_when_no_dependents_at_all():
    """No semantic and no structural dependents — should exit 0 with a message."""
    mock_graph = MagicMock()
    mock_graph.connect = AsyncMock()
    mock_graph.close = AsyncMock()
    mock_graph.get_latest_definition = AsyncMock(return_value=None)
    mock_graph.get_semantic_dependents_with_depth = AsyncMock(return_value=[])
    mock_graph.get_structural_dependents = AsyncMock(return_value=[])
    mock_graph.get_structural_ancestors = AsyncMock(return_value=[])

    mock_gh = MagicMock()
    mock_gh.health_check = AsyncMock(return_value=True)

    with (
        patch("puxti.cli.redefine.settings") as mock_settings,
        patch("puxti.cli.redefine.KnowledgeGraph", return_value=mock_graph),
        patch("puxti.cli.redefine.GitHubConnector", return_value=mock_gh),
    ):
        mock_settings.dbt_project_dir = "/some/dbt"
        mock_settings.github_token = "ghp_test"

        result = runner.invoke(app, [
            "redefine",
            "--entity", "model.demo_shop.orders.gross_revenue",
            "--description", "now excludes refunds",
            "--repo", "acme/data",
        ])

    assert result.exit_code == 0
    assert "nothing" in result.output.lower() or "no dependents" in result.output.lower()


def test_redefine_falls_back_to_structural_dependents():
    """No semantic dependents but structural ones exist — should surface them."""
    from puxti.models import EntityType

    structural_dep = MagicMock()
    structural_dep.id = "model.demo_shop.reports"
    structural_dep.name = "reports"
    structural_dep.type = MagicMock(value="model")

    mock_graph = MagicMock()
    mock_graph.connect = AsyncMock()
    mock_graph.close = AsyncMock()
    mock_graph.get_latest_definition = AsyncMock(return_value=None)
    mock_graph.get_semantic_dependents_with_depth = AsyncMock(return_value=[])
    mock_graph.get_structural_dependents = AsyncMock(return_value=[structural_dep])
    mock_graph.get_structural_ancestors = AsyncMock(return_value=[])
    mock_graph.upsert_definition = AsyncMock()
    mock_graph.save_change_event = AsyncMock()

    from puxti.models import FileDiff, PropagationResult

    real_diff = FileDiff(
        file_path="models/reports.sql",
        before="select * from customers",
        after="-- PUXTI [manual review required]\nselect * from customers",
        connector="dbt",
        description="manual review",
    )

    mock_redefiner = MagicMock()
    mock_redefiner.generate_passthrough_diffs = AsyncMock(return_value=[])
    mock_redefiner.generate_diffs = AsyncMock(return_value=[real_diff])

    pr_result = PropagationResult(
        change_event_id="evt-001",
        connector="dbt",
        target_entity_id="model.demo_shop.orders.gross_revenue",
        diffs=[real_diff],
        pr_url="https://github.com/acme/data/pull/9",
        status="opened",
    )

    mock_gh = MagicMock()
    mock_gh.health_check = AsyncMock(return_value=True)
    mock_gh.open_pr = AsyncMock(return_value=pr_result)

    with (
        patch("puxti.cli.redefine.settings") as mock_settings,
        patch("puxti.cli.redefine.KnowledgeGraph", return_value=mock_graph),
        patch("puxti.cli.redefine.SemanticRedefiner", return_value=mock_redefiner),
        patch("puxti.cli.redefine.GitHubConnector", return_value=mock_gh),
        patch("puxti.cli.redefine.DbtConnector"),
    ):
        mock_settings.dbt_project_dir = "/some/dbt"
        mock_settings.github_token = "ghp_test"

        result = runner.invoke(app, [
            "redefine",
            "--entity", "model.demo_shop.orders.gross_revenue",
            "--description", "now excludes refunds",
            "--repo", "acme/data",
        ])

    assert result.exit_code == 0, result.output
    assert "reports" in result.output
    assert "structural" in result.output.lower() or "lineage" in result.output.lower()


def test_redefine_happy_path_prints_pr_url():
    from puxti.models import EntityType, FileDiff, PropagationResult, SemanticChangeEvent, ChangeType

    mock_graph = MagicMock()
    mock_graph.connect = AsyncMock()
    mock_graph.close = AsyncMock()
    mock_graph.get_latest_definition = AsyncMock(return_value=None)
    mock_graph.get_semantic_dependents_with_depth = AsyncMock(return_value=[
        (MagicMock(id="model.demo_shop.customers", name="customers", type=MagicMock(value="model")), 1),
    ])
    mock_graph.get_structural_ancestors = AsyncMock(return_value=[])
    mock_graph.upsert_definition = AsyncMock()
    mock_graph.save_change_event = AsyncMock()

    diff = FileDiff(
        file_path="models/marts/customers.sql",
        before="select * from orders",
        after="-- PUXTI [high confidence]\nselect * from orders where not is_refund",
        connector="dbt",
        description="Semantic redefinition",
    )

    mock_redefiner = MagicMock()
    mock_redefiner.generate_passthrough_diffs = AsyncMock(return_value=[])
    mock_redefiner.generate_diffs = AsyncMock(return_value=[diff])

    pr_result = PropagationResult(
        change_event_id="evt-001",
        connector="dbt",
        target_entity_id="model.demo_shop.orders.gross_revenue",
        diffs=[diff],
        pr_url="https://github.com/acme/data/pull/7",
        status="opened",
    )
    mock_gh = MagicMock()
    mock_gh.health_check = AsyncMock(return_value=True)
    mock_gh.open_pr = AsyncMock(return_value=pr_result)

    with (
        patch("puxti.cli.redefine.settings") as mock_settings,
        patch("puxti.cli.redefine.KnowledgeGraph", return_value=mock_graph),
        patch("puxti.cli.redefine.SemanticRedefiner", return_value=mock_redefiner),
        patch("puxti.cli.redefine.GitHubConnector", return_value=mock_gh),
        patch("puxti.cli.redefine.DbtConnector"),
    ):
        mock_settings.dbt_project_dir = "/some/dbt"
        mock_settings.github_token = "ghp_test"

        result = runner.invoke(app, [
            "redefine",
            "--entity", "model.demo_shop.orders.gross_revenue",
            "--description", "now excludes refunds",
            "--repo", "acme/data",
        ])

    assert result.exit_code == 0, result.output
    assert "https://github.com/acme/data/pull/7" in result.output


def test_redefine_dry_run_shows_cost_estimate():
    from puxti.models import EntityType

    dep = MagicMock()
    dep.id = "model.demo_shop.customers"
    dep.name = "customers"
    dep.type = MagicMock(value="model")

    mock_graph = MagicMock()
    mock_graph.connect = AsyncMock()
    mock_graph.close = AsyncMock()
    mock_graph.get_latest_definition = AsyncMock(return_value=None)
    mock_graph.get_semantic_dependents_with_depth = AsyncMock(return_value=[(dep, 1)])

    mock_dbt = MagicMock()
    mock_dbt.get_model_sql_map = MagicMock(return_value={dep.id: "select * from stg_customers"})

    count_response = MagicMock()
    count_response.input_tokens = 300
    mock_anthropic_client = MagicMock()
    mock_anthropic_client.messages = MagicMock()
    mock_anthropic_client.messages.count_tokens = AsyncMock(return_value=count_response)

    with (
        patch("puxti.cli.redefine.settings") as mock_settings,
        patch("puxti.cli.redefine.KnowledgeGraph", return_value=mock_graph),
        patch("puxti.cli.redefine.DbtConnector", return_value=mock_dbt),
        patch("anthropic.AsyncAnthropic", return_value=mock_anthropic_client),
    ):
        mock_settings.dbt_project_dir = "/some/dbt"
        mock_settings.anthropic_api_key = "sk-ant-test"
        mock_settings.github_token = None

        result = runner.invoke(app, [
            "redefine",
            "--entity", "model.demo_shop.orders.gross_revenue",
            "--description", "now excludes refunds",
            "--dry-run",
        ])

    assert result.exit_code == 0, result.output
    assert "dry run" in result.output.lower()
    assert "customers" in result.output
    assert "PR opened" not in result.output


def test_redefine_dry_run_does_not_require_repo_or_github_token():
    dep = MagicMock()
    dep.id = "model.demo_shop.customers"
    dep.name = "customers"
    dep.type = MagicMock(value="model")

    mock_graph = MagicMock()
    mock_graph.connect = AsyncMock()
    mock_graph.close = AsyncMock()
    mock_graph.get_latest_definition = AsyncMock(return_value=None)
    mock_graph.get_semantic_dependents_with_depth = AsyncMock(return_value=[(dep, 1)])

    mock_dbt = MagicMock()
    mock_dbt.get_model_sql_map = MagicMock(return_value={dep.id: "select * from stg"})

    count_response = MagicMock()
    count_response.input_tokens = 200
    mock_anthropic_client = MagicMock()
    mock_anthropic_client.messages = MagicMock()
    mock_anthropic_client.messages.count_tokens = AsyncMock(return_value=count_response)

    with (
        patch("puxti.cli.redefine.settings") as mock_settings,
        patch("puxti.cli.redefine.KnowledgeGraph", return_value=mock_graph),
        patch("puxti.cli.redefine.DbtConnector", return_value=mock_dbt),
        patch("anthropic.AsyncAnthropic", return_value=mock_anthropic_client),
    ):
        mock_settings.dbt_project_dir = "/some/dbt"
        mock_settings.anthropic_api_key = "sk-ant-test"
        mock_settings.github_token = None  # no token

        result = runner.invoke(app, [
            "redefine",
            "--entity", "model.demo_shop.orders.gross_revenue",
            "--description", "now excludes refunds",
            "--dry-run",
            # no --repo
        ])

    # Should not fail due to missing token or repo
    assert result.exit_code == 0, result.output


# ── regression: redefine PR failure leaves graph unchanged ────────────────────

def test_redefine_pr_failure_leaves_graph_unchanged():
    """If open_pr() raises, upsert_definition and save_change_event must not have been called."""
    mock_graph = MagicMock()
    mock_graph.connect = AsyncMock()
    mock_graph.close = AsyncMock()
    mock_graph.get_latest_definition = AsyncMock(return_value=None)
    mock_graph.get_semantic_dependents_with_depth = AsyncMock(return_value=[
        (MagicMock(id="model.demo_shop.customers", name="customers", type=MagicMock(value="model")), 1),
    ])
    mock_graph.get_structural_ancestors = AsyncMock(return_value=[])
    mock_graph.upsert_definition = AsyncMock()
    mock_graph.save_change_event = AsyncMock()

    from puxti.models import FileDiff
    diff = FileDiff(
        file_path="models/customers.sql",
        before="select * from orders",
        after="-- PUXTI [high confidence]\nselect * from orders where not is_refund",
        connector="dbt",
        description="Semantic redefinition",
    )
    mock_redefiner = MagicMock()
    mock_redefiner.generate_passthrough_diffs = AsyncMock(return_value=[])
    mock_redefiner.generate_diffs = AsyncMock(return_value=[diff])

    mock_gh = MagicMock()
    mock_gh.health_check = AsyncMock(return_value=True)
    mock_gh.open_pr = AsyncMock(side_effect=RuntimeError("GitHub API unavailable"))

    with (
        patch("puxti.cli.redefine.settings") as mock_settings,
        patch("puxti.cli.redefine.KnowledgeGraph", return_value=mock_graph),
        patch("puxti.cli.redefine.SemanticRedefiner", return_value=mock_redefiner),
        patch("puxti.cli.redefine.GitHubConnector", return_value=mock_gh),
        patch("puxti.cli.redefine.DbtConnector"),
    ):
        mock_settings.dbt_project_dir = "/some/dbt"
        mock_settings.github_token = "ghp_test"

        result = runner.invoke(app, [
            "redefine",
            "--entity", "model.demo_shop.orders.gross_revenue",
            "--description", "now excludes refunds",
            "--repo", "acme/data",
        ])

    assert result.exit_code == 1
    mock_graph.upsert_definition.assert_not_called()
    mock_graph.save_change_event.assert_not_called()
