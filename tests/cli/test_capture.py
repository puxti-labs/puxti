"""`puxti capture` — argument validation, happy path, dry-run."""

from unittest.mock import AsyncMock, MagicMock, patch

from puxti.cli import app
from tests.cli._helpers import plain, runner


def test_capture_shows_help():
    result = runner.invoke(app, ["capture", "--help"])
    assert result.exit_code == 0
    assert "--entity" in plain(result.output)
    assert "--before" in plain(result.output)
    assert "--after" in plain(result.output)
    assert "--description" in plain(result.output)
    assert "--repo" in plain(result.output)


# ── capture — argument validation ─────────────────────────────────────────────

def test_capture_missing_required_args_exits_nonzero():
    result = runner.invoke(app, ["capture"])
    assert result.exit_code != 0


def test_capture_exits_when_no_dbt_project_dir(monkeypatch):
    """Without dbt project dir (env or flag), capture should exit 1 with an error."""
    monkeypatch.delenv("DBT_PROJECT_DIR", raising=False)

    # Reload settings so the env change takes effect
    with patch("puxti.cli.capture.settings") as mock_settings:
        mock_settings.dbt_project_dir = None
        mock_settings.github_token = "ghp_test"

        result = runner.invoke(app, [
            "capture",
            "--entity", "model.jaffle_shop.orders.order_date",
            "--before", "order_date",
            "--after", "recorded_date",
            "--description", "Renamed for clarity",
            "--repo", "acme/data",
        ])

    assert result.exit_code == 1
    assert "dbt project directory" in result.output.lower() or "dbt_project_dir" in result.output.lower()


def test_capture_exits_when_no_github_token(monkeypatch):
    """Without GITHUB_TOKEN, capture should exit 1 with an error."""
    with patch("puxti.cli.capture.settings") as mock_settings:
        mock_settings.dbt_project_dir = "/some/project"
        mock_settings.github_token = None

        result = runner.invoke(app, [
            "capture",
            "--entity", "model.jaffle_shop.orders.order_date",
            "--before", "order_date",
            "--after", "recorded_date",
            "--description", "Renamed for clarity",
            "--repo", "acme/data",
        ])

    assert result.exit_code == 1
    assert "github token" in result.output.lower() or "GITHUB_TOKEN" in result.output


# ── capture — happy path (all components mocked) ──────────────────────────────

def test_capture_happy_path_prints_pr_url():
    """Full capture flow with all external services mocked."""
    from puxti.models import (
        ChangeType,
        FileDiff,
        PropagationResult,
        SemanticChangeEvent,
    )

    semantic_event = SemanticChangeEvent(
        change_event_id="evt-001",
        entity_id="model.jaffle_shop.orders.order_date",
        change_type=ChangeType.STRUCTURAL,
        semantic_context="order_date renamed to recorded_date.",
        affected_entity_ids=["model.jaffle_shop.orders"],
        reasoning="Column rename propagates to all referencing models.",
        change={"before": {"name": "order_date"}, "after": {"name": "recorded_date"}},
    )

    diff = FileDiff(
        file_path="models/orders.sql",
        before="select order_date from stg",
        after="select recorded_date from stg",
        connector="dbt",
        description="Renamed order_date → recorded_date",
    )

    prop_result = PropagationResult(
        change_event_id="evt-001",
        connector="dbt",
        target_entity_id="model.jaffle_shop.orders.order_date",
        diffs=[diff],
        pr_url="https://github.com/acme/data/pull/42",
        status="opened",
    )

    mock_graph = MagicMock()
    mock_graph.connect = AsyncMock()
    mock_graph.close = AsyncMock()
    mock_graph.get_latest_definition = AsyncMock(return_value=None)
    mock_graph.get_semantic_dependents = AsyncMock(return_value=[])
    mock_graph.get_structural_dependents = AsyncMock(return_value=[])
    mock_graph.get_structural_ancestors = AsyncMock(return_value=[])
    mock_graph.get_entity_by_id = AsyncMock(return_value=MagicMock())
    mock_graph.upsert_definition = AsyncMock()
    mock_graph.upsert_semantic_edge = AsyncMock()
    mock_graph.save_change_event = AsyncMock()

    mock_commit = AsyncMock()
    mock_capture = MagicMock()
    mock_capture.capture = AsyncMock(return_value=(semantic_event, mock_commit))

    mock_engine = MagicMock()
    mock_engine.propagate = AsyncMock(return_value=[prop_result])

    mock_gh = MagicMock()
    mock_gh.health_check = AsyncMock(return_value=True)
    mock_gh.open_pr = AsyncMock(return_value=prop_result)

    with (
        patch("puxti.cli.capture.settings") as mock_settings,
        patch("puxti.cli.capture.KnowledgeGraph", return_value=mock_graph),
        patch("puxti.cli.capture.SemanticCapture", return_value=mock_capture),
        patch("puxti.cli.capture.PropagationEngine", return_value=mock_engine),
        patch("puxti.cli.capture.GitHubConnector", return_value=mock_gh),
    ):
        mock_settings.dbt_project_dir = "/some/dbt"
        mock_settings.github_token = "ghp_test"

        result = runner.invoke(app, [
            "capture",
            "--entity", "model.jaffle_shop.orders.order_date",
            "--before", "order_date",
            "--after", "recorded_date",
            "--description", "Renamed for clarity",
            "--repo", "acme/data",
        ], input="y\n")

    assert result.exit_code == 0, result.output
    assert "https://github.com/acme/data/pull/42" in result.output


def test_capture_no_diffs_exits_cleanly():
    """When propagation produces no diffs, capture exits 0 with an informational message."""
    from puxti.models import ChangeType, SemanticChangeEvent

    semantic_event = SemanticChangeEvent(
        change_event_id="evt-002",
        entity_id="model.jaffle_shop.orders.order_date",
        change_type=ChangeType.STRUCTURAL,
        semantic_context="order_date renamed to recorded_date.",
        affected_entity_ids=[],
        reasoning="No downstream references found.",
        change={"before": {"name": "order_date"}, "after": {"name": "recorded_date"}},
    )

    mock_graph = MagicMock()
    mock_graph.connect = AsyncMock()
    mock_graph.close = AsyncMock()
    mock_graph.get_latest_definition = AsyncMock(return_value=None)
    mock_graph.get_semantic_dependents = AsyncMock(return_value=[])
    mock_graph.get_structural_dependents = AsyncMock(return_value=[])
    mock_graph.get_structural_ancestors = AsyncMock(return_value=[])
    mock_graph.get_entity_by_id = AsyncMock(return_value=MagicMock())
    mock_graph.upsert_definition = AsyncMock()
    mock_graph.upsert_semantic_edge = AsyncMock()
    mock_graph.save_change_event = AsyncMock()

    mock_capture = MagicMock()
    mock_capture.capture = AsyncMock(return_value=(semantic_event, AsyncMock()))

    mock_engine = MagicMock()
    mock_engine.propagate = AsyncMock(return_value=[])

    mock_gh = MagicMock()
    mock_gh.health_check = AsyncMock(return_value=True)

    mock_dbt = MagicMock()
    mock_dbt.get_project_name = MagicMock(return_value="jaffle_shop")

    with (
        patch("puxti.cli.capture.settings") as mock_settings,
        patch("puxti.cli.capture.KnowledgeGraph", return_value=mock_graph),
        patch("puxti.cli.capture.SemanticCapture", return_value=mock_capture),
        patch("puxti.cli.capture.PropagationEngine", return_value=mock_engine),
        patch("puxti.cli.capture.GitHubConnector", return_value=mock_gh),
        patch("puxti.cli.capture.DbtConnector", return_value=mock_dbt),
    ):
        mock_settings.dbt_project_dir = "/some/dbt"
        mock_settings.github_token = "ghp_test"

        result = runner.invoke(app, [
            "capture",
            "--entity", "model.jaffle_shop.orders.order_date",
            "--before", "order_date",
            "--after", "recorded_date",
            "--description", "test",
            "--repo", "acme/data",
        ], input="y\n")

    assert result.exit_code == 0
    assert "no diffs" in result.output.lower()


def test_capture_warns_when_entity_not_in_graph():
    """When the entity ID isn't in the graph, capture should warn but continue."""
    from puxti.models import ChangeType, FileDiff, PropagationResult, SemanticChangeEvent

    semantic_event = SemanticChangeEvent(
        change_event_id="evt-warn",
        entity_id="bad.format.entity.type",
        change_type=ChangeType.STRUCTURAL,
        semantic_context="type renamed to result_type.",
        affected_entity_ids=[],
        reasoning="",
        change={"before": {"name": "type"}, "after": {"name": "result_type"}},
    )
    prop_result = PropagationResult(
        change_event_id="evt-warn",
        connector="dbt",
        target_entity_id="bad.format.entity.type",
        diffs=[],
        pr_url="https://github.com/acme/data/pull/99",
        status="opened",
    )

    mock_graph = MagicMock()
    mock_graph.connect = AsyncMock()
    mock_graph.close = AsyncMock()
    mock_graph.get_latest_definition = AsyncMock(return_value=None)
    mock_graph.get_semantic_dependents = AsyncMock(return_value=[])
    mock_graph.get_structural_dependents = AsyncMock(return_value=[])
    mock_graph.get_structural_ancestors = AsyncMock(return_value=[])
    mock_graph.get_entity_by_id = AsyncMock(return_value=None)  # entity not found
    mock_graph.upsert_definition = AsyncMock()
    mock_graph.upsert_semantic_edge = AsyncMock()
    mock_graph.save_change_event = AsyncMock()

    mock_capture = MagicMock()
    mock_capture.capture = AsyncMock(return_value=(semantic_event, AsyncMock()))

    mock_engine = MagicMock()
    mock_engine.propagate = AsyncMock(return_value=[prop_result])

    mock_gh = MagicMock()
    mock_gh.health_check = AsyncMock(return_value=True)
    mock_gh.open_pr = AsyncMock(return_value=prop_result)

    with (
        patch("puxti.cli.capture.settings") as mock_settings,
        patch("puxti.cli.capture.KnowledgeGraph", return_value=mock_graph),
        patch("puxti.cli.capture.SemanticCapture", return_value=mock_capture),
        patch("puxti.cli.capture.PropagationEngine", return_value=mock_engine),
        patch("puxti.cli.capture.GitHubConnector", return_value=mock_gh),
    ):
        mock_settings.dbt_project_dir = "/some/dbt"
        mock_settings.github_token = "ghp_test"

        result = runner.invoke(app, [
            "capture",
            "--entity", "bad.format.entity.type",
            "--before", "type",
            "--after", "result_type",
            "--description", "test",
            "--repo", "acme/data",
        ], input="y\n")

    assert result.exit_code == 0
    assert "not found in the Knowledge Graph" in result.output or "resolving lineage by model name" in result.output
    assert "puxti describe" in result.output


# ── capture --dry-run ─────────────────────────────────────────────────────────

def test_capture_dry_run_shows_cost_estimate():
    mock_graph = MagicMock()
    mock_graph.connect = AsyncMock()
    mock_graph.close = AsyncMock()
    mock_graph.get_latest_definition = AsyncMock(return_value=None)
    mock_graph.get_semantic_dependents = AsyncMock(return_value=[])
    mock_graph.get_structural_dependents = AsyncMock(return_value=[])
    mock_graph.get_structural_ancestors = AsyncMock(return_value=[])
    mock_graph.get_all_entity_ids = AsyncMock(return_value=[])
    mock_graph.get_entity_by_id = AsyncMock(return_value=MagicMock())

    mock_capture = MagicMock()
    mock_capture.estimate_cost = AsyncMock(return_value={
        "input_tokens": 450,
        "tokens_exact": True,
        "estimated_output_tokens": 614,
        "estimated_cost_usd": 0.0108,
    })

    with (
        patch("puxti.cli.capture.settings") as mock_settings,
        patch("puxti.cli.capture.KnowledgeGraph", return_value=mock_graph),
        patch("puxti.cli.capture.SemanticCapture", return_value=mock_capture),
    ):
        mock_settings.dbt_project_dir = "/some/dbt"
        mock_settings.github_token = "ghp_test"

        result = runner.invoke(app, [
            "capture",
            "--entity", "model.jaffle_shop.orders.order_date",
            "--before", "order_date",
            "--after", "recorded_date",
            "--description", "Renamed for clarity",
            "--repo", "acme/data",
            "--dry-run",
        ])

    assert result.exit_code == 0
    assert "450" in result.output       # input tokens
    assert "0.0108" in result.output    # cost
    # Must not have opened a PR
    assert "PR opened" not in result.output


def test_capture_dry_run_does_not_require_github_token():
    mock_graph = MagicMock()
    mock_graph.connect = AsyncMock()
    mock_graph.close = AsyncMock()
    mock_graph.get_latest_definition = AsyncMock(return_value=None)
    mock_graph.get_semantic_dependents = AsyncMock(return_value=[])
    mock_graph.get_structural_dependents = AsyncMock(return_value=[])
    mock_graph.get_structural_ancestors = AsyncMock(return_value=[])
    mock_graph.get_all_entity_ids = AsyncMock(return_value=[])
    mock_graph.get_entity_by_id = AsyncMock(return_value=MagicMock())

    mock_capture = MagicMock()
    mock_capture.estimate_cost = AsyncMock(return_value={
        "input_tokens": 300,
        "tokens_exact": True,
        "estimated_output_tokens": 614,
        "estimated_cost_usd": 0.0100,
    })

    with (
        patch("puxti.cli.capture.settings") as mock_settings,
        patch("puxti.cli.capture.KnowledgeGraph", return_value=mock_graph),
        patch("puxti.cli.capture.SemanticCapture", return_value=mock_capture),
    ):
        mock_settings.dbt_project_dir = "/some/dbt"
        mock_settings.github_token = None  # no token

        result = runner.invoke(app, [
            "capture",
            "--entity", "model.jaffle_shop.orders.order_date",
            "--before", "order_date",
            "--after", "recorded_date",
            "--description", "test",
            "--repo", "acme/data",
            "--dry-run",
        ])

    assert result.exit_code == 0  # should not fail without GitHub token


# ── regression: capture --dry-run succeeds without --repo ────────────────────

def test_capture_dry_run_succeeds_without_repo():
    """--dry-run must not require --repo — exits cleanly and shows cost estimate."""
    mock_graph = MagicMock()
    mock_graph.connect = AsyncMock()
    mock_graph.close = AsyncMock()
    mock_graph.get_latest_definition = AsyncMock(return_value=None)
    mock_graph.get_semantic_dependents = AsyncMock(return_value=[])
    mock_graph.get_structural_dependents = AsyncMock(return_value=[])
    mock_graph.get_structural_ancestors = AsyncMock(return_value=[])
    mock_graph.get_all_entity_ids = AsyncMock(return_value=[])
    mock_graph.get_entity_by_id = AsyncMock(return_value=MagicMock())

    mock_capture = MagicMock()
    mock_capture.estimate_cost = AsyncMock(return_value={
        "input_tokens": 350,
        "tokens_exact": True,
        "estimated_output_tokens": 614,
        "estimated_cost_usd": 0.0105,
    })

    with (
        patch("puxti.cli.capture.settings") as mock_settings,
        patch("puxti.cli.capture.KnowledgeGraph", return_value=mock_graph),
        patch("puxti.cli.capture.SemanticCapture", return_value=mock_capture),
    ):
        mock_settings.dbt_project_dir = "/some/dbt"
        mock_settings.github_token = None

        result = runner.invoke(app, [
            "capture",
            "--entity", "model.jaffle_shop.orders.order_date",
            "--before", "order_date",
            "--after", "recorded_date",
            "--description", "Renamed for clarity",
            "--dry-run",
            # no --repo
        ])

    assert result.exit_code == 0, result.output
    assert "350" in result.output
    assert "PR opened" not in result.output


def test_capture_dry_run_without_pricing_shows_hint_not_cost():
    """Unknown model pricing → dry-run shows tokens and the override hint,
    never a dollar figure."""
    mock_graph = MagicMock()
    mock_graph.connect = AsyncMock()
    mock_graph.close = AsyncMock()
    mock_graph.get_latest_definition = AsyncMock(return_value=None)
    mock_graph.get_semantic_dependents = AsyncMock(return_value=[])
    mock_graph.get_structural_dependents = AsyncMock(return_value=[])
    mock_graph.get_all_entity_ids = AsyncMock(return_value=[])
    mock_graph.get_entity_by_id = AsyncMock(return_value=MagicMock())

    mock_capture = MagicMock()
    mock_capture.estimate_cost = AsyncMock(return_value={
        "input_tokens": 900,
        "tokens_exact": False,
        "estimated_output_tokens": 614,
        "estimated_cost_usd": None,
    })

    with (
        patch("puxti.cli.capture.settings") as mock_settings,
        patch("puxti.cli.capture.KnowledgeGraph", return_value=mock_graph),
        patch("puxti.cli.capture.SemanticCapture", return_value=mock_capture),
    ):
        mock_settings.dbt_project_dir = "/some/dbt"
        mock_settings.github_token = None

        result = runner.invoke(app, [
            "capture",
            "--entity", "model.jaffle_shop.orders.order_date",
            "--before", "order_date",
            "--after", "recorded_date",
            "--description", "test",
            "--dry-run",
        ])

    assert result.exit_code == 0, result.output
    assert "900" in result.output
    assert "approximate" in result.output
    assert "LLM_INPUT_COST_PER_MTOK" in result.output
    assert "$" not in result.output.replace("$0", "")  # no dollar figure rendered


def test_capture_rebases_and_routes_sql_views_diffs_to_their_repo():
    """sql_views diffs get the connector's repo_subdir prefix and PR to the
    connector's own repo; dbt diffs keep the --repo destination."""
    from puxti.models import ChangeType, FileDiff, PropagationResult, SemanticChangeEvent
    from puxti.workspace import ConnectorConfig, WorkspaceConfig

    semantic_event = SemanticChangeEvent(
        change_event_id="evt-multi",
        entity_id="model.jaffle_shop.users.email",
        change_type=ChangeType.STRUCTURAL,
        semantic_context="email renamed to contact_email.",
        affected_entity_ids=["view.public.user_stats"],
        reasoning="",
        change={"before": {"name": "email"}, "after": {"name": "contact_email"}},
    )

    dbt_result = PropagationResult(
        change_event_id="evt-multi",
        connector="dbt",
        target_entity_id=semantic_event.entity_id,
        diffs=[FileDiff(
            file_path="models/users.sql", before="a", after="b",
            connector="dbt", description="dbt diff",
        )],
        pr_url="https://github.com/acme/data/pull/7",
        status="opened",
    )
    views_result = PropagationResult(
        change_event_id="evt-multi",
        connector="sql_views",
        target_entity_id=semantic_event.entity_id,
        diffs=[FileDiff(
            file_path="views/user_stats.sql", before="a", after="b",
            connector="sql_views", description="view diff",
        )],
        pr_url="https://github.com/acme/app/pull/8",
        status="opened",
    )

    ws = WorkspaceConfig(
        dbt=ConnectorConfig(project_dir="/some/dbt", repo="acme/data"),
        sql_views=ConnectorConfig(
            project_dir="/some/app", repo="acme/app", repo_subdir="backend",
            extras={"views_dir": "views"},
        ),
    )

    mock_graph = MagicMock()
    mock_graph.connect = AsyncMock()
    mock_graph.close = AsyncMock()
    mock_graph.get_latest_definition = AsyncMock(return_value=None)
    mock_graph.get_semantic_dependents = AsyncMock(return_value=[])
    mock_graph.get_structural_dependents = AsyncMock(return_value=[])
    mock_graph.get_entity_by_id = AsyncMock(return_value=MagicMock())

    mock_capture = MagicMock()
    mock_capture.capture = AsyncMock(return_value=(semantic_event, AsyncMock()))

    mock_engine = MagicMock()
    mock_engine.propagate = AsyncMock(return_value=[dbt_result, views_result])

    gh_configs: list[dict] = []

    def _make_gh(config):
        gh_configs.append(config)
        gh = MagicMock()
        gh.health_check = AsyncMock(return_value=True)
        gh.open_pr = AsyncMock(
            side_effect=lambda result, event, companions=None: result
        )
        gh.add_companion_note = AsyncMock()
        return gh

    with (
        patch("puxti.cli.capture.settings") as mock_settings,
        patch("puxti.cli.capture.KnowledgeGraph", return_value=mock_graph),
        patch("puxti.cli.capture.SemanticCapture", return_value=mock_capture),
        patch("puxti.cli.capture.PropagationEngine", return_value=mock_engine),
        patch("puxti.cli.capture.GitHubConnector", side_effect=_make_gh),
        patch("puxti.cli._shared.load_workspace", return_value=ws),
    ):
        mock_settings.dbt_project_dir = None
        mock_settings.github_token = "ghp_test"

        result = runner.invoke(app, [
            "capture",
            "--entity", "model.jaffle_shop.users.email",
            "--before", "email",
            "--after", "contact_email",
            "--description", "renamed",
        ], input="y\n")

    assert result.exit_code == 0, result.output
    # sql_views diff was rebased onto the connector's repo_subdir
    assert views_result.diffs[0].file_path == "backend/views/user_stats.sql"
    # dbt diff untouched (no repo_subdir configured)
    assert dbt_result.diffs[0].file_path == "models/users.sql"
    # one PR per connector, each to its own repo
    pr_repos = [c["repo"] for c in gh_configs if "base_branch" in c]
    assert pr_repos == ["acme/data", "acme/app"]
