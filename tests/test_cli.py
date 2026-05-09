"""CLI smoke tests — covers argument validation and error paths.

Integration paths (Neo4j, Anthropic, GitHub) are tested via their
respective unit tests; the CLI wires them together and is tested here
only for the thin orchestration layer.
"""

import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from puxti.cli import app

runner = CliRunner()


def plain(text: str) -> str:
    """Strip ANSI escape codes from output for portable assertions."""
    return re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", text)


# ── help / command registration ───────────────────────────────────────────────

def test_app_shows_help():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "capture" in result.output
    assert "scan" in result.output
    assert "redefine" in result.output
    assert "health" in result.output
    assert "describe" in result.output
    assert "purge" in result.output
    assert "correct" in result.output


def test_capture_shows_help():
    result = runner.invoke(app, ["capture", "--help"])
    assert result.exit_code == 0
    assert "--entity" in plain(result.output)
    assert "--before" in plain(result.output)
    assert "--after" in plain(result.output)
    assert "--description" in plain(result.output)
    assert "--repo" in plain(result.output)


def test_health_shows_help():
    result = runner.invoke(app, ["health", "--help"])
    assert result.exit_code == 0


# ── capture — argument validation ─────────────────────────────────────────────

def test_capture_missing_required_args_exits_nonzero():
    result = runner.invoke(app, ["capture"])
    assert result.exit_code != 0


def test_capture_exits_when_no_dbt_project_dir(monkeypatch):
    """Without dbt project dir (env or flag), capture should exit 1 with an error."""
    monkeypatch.delenv("DBT_PROJECT_DIR", raising=False)

    # Reload settings so the env change takes effect
    with patch("puxti.cli.settings") as mock_settings:
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
    with patch("puxti.cli.settings") as mock_settings:
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
        patch("puxti.cli.settings") as mock_settings,
        patch("puxti.cli.KnowledgeGraph", return_value=mock_graph),
        patch("puxti.cli.SemanticCapture", return_value=mock_capture),
        patch("puxti.cli.PropagationEngine", return_value=mock_engine),
        patch("puxti.cli.GitHubConnector", return_value=mock_gh),
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
        patch("puxti.cli.settings") as mock_settings,
        patch("puxti.cli.KnowledgeGraph", return_value=mock_graph),
        patch("puxti.cli.SemanticCapture", return_value=mock_capture),
        patch("puxti.cli.PropagationEngine", return_value=mock_engine),
        patch("puxti.cli.GitHubConnector", return_value=mock_gh),
        patch("puxti.cli.DbtConnector", return_value=mock_dbt),
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
        patch("puxti.cli.settings") as mock_settings,
        patch("puxti.cli.KnowledgeGraph", return_value=mock_graph),
        patch("puxti.cli.SemanticCapture", return_value=mock_capture),
        patch("puxti.cli.PropagationEngine", return_value=mock_engine),
        patch("puxti.cli.GitHubConnector", return_value=mock_gh),
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
    mock_graph.get_entity_by_id = AsyncMock(return_value=MagicMock())

    mock_capture = MagicMock()
    mock_capture.estimate_cost = AsyncMock(return_value={
        "input_tokens": 450,
        "estimated_output_tokens": 614,
        "estimated_cost_usd": 0.0108,
    })

    with (
        patch("puxti.cli.settings") as mock_settings,
        patch("puxti.cli.KnowledgeGraph", return_value=mock_graph),
        patch("puxti.cli.SemanticCapture", return_value=mock_capture),
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
    mock_graph.get_entity_by_id = AsyncMock(return_value=MagicMock())

    mock_capture = MagicMock()
    mock_capture.estimate_cost = AsyncMock(return_value={
        "input_tokens": 300,
        "estimated_output_tokens": 614,
        "estimated_cost_usd": 0.0100,
    })

    with (
        patch("puxti.cli.settings") as mock_settings,
        patch("puxti.cli.KnowledgeGraph", return_value=mock_graph),
        patch("puxti.cli.SemanticCapture", return_value=mock_capture),
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


# ── health ────────────────────────────────────────────────────────────────────

def _make_count_tokens_response(input_tokens: int = 10) -> MagicMock:
    r = MagicMock()
    r.input_tokens = input_tokens
    return r


def test_health_all_ok():
    mock_graph = MagicMock()
    mock_graph.connect = AsyncMock()
    mock_graph.close = AsyncMock()

    mock_dbt = MagicMock()
    mock_dbt.health_check = AsyncMock(return_value=True)

    mock_anthropic_client = MagicMock()
    mock_anthropic_client.messages = MagicMock()
    mock_anthropic_client.messages.count_tokens = AsyncMock(
        return_value=_make_count_tokens_response()
    )

    with (
        patch("puxti.cli.settings") as mock_settings,
        patch("puxti.cli.KnowledgeGraph", return_value=mock_graph),
        patch("puxti.cli.DbtConnector", return_value=mock_dbt),
        patch("puxti.cli.anthropic.AsyncAnthropic", return_value=mock_anthropic_client),
    ):
        mock_settings.dbt_project_dir = "/some/dbt"
        mock_settings.anthropic_api_key = "sk-ant-test"

        result = runner.invoke(app, ["health"])

    assert result.exit_code == 0
    assert "Neo4j" in result.output
    assert "Anthropic" in result.output
    assert "dbt" in result.output


def test_health_anthropic_invalid_key_exits_nonzero():
    import anthropic as anthropic_lib

    mock_graph = MagicMock()
    mock_graph.connect = AsyncMock()
    mock_graph.close = AsyncMock()

    mock_anthropic_client = MagicMock()
    mock_anthropic_client.messages = MagicMock()
    mock_anthropic_client.messages.count_tokens = AsyncMock(
        side_effect=anthropic_lib.AuthenticationError(
            message="Invalid API key",
            response=MagicMock(status_code=401),
            body={},
        )
    )

    with (
        patch("puxti.cli.settings") as mock_settings,
        patch("puxti.cli.KnowledgeGraph", return_value=mock_graph),
        patch("puxti.cli.anthropic.AsyncAnthropic", return_value=mock_anthropic_client),
    ):
        mock_settings.dbt_project_dir = None
        mock_settings.anthropic_api_key = "sk-ant-bad"

        result = runner.invoke(app, ["health"])

    assert result.exit_code == 1
    assert "invalid" in result.output.lower()


def test_health_anthropic_credit_error_exits_nonzero():
    import anthropic as anthropic_lib

    mock_graph = MagicMock()
    mock_graph.connect = AsyncMock()
    mock_graph.close = AsyncMock()

    mock_anthropic_client = MagicMock()
    mock_anthropic_client.messages = MagicMock()
    mock_anthropic_client.messages.count_tokens = AsyncMock(
        side_effect=anthropic_lib.BadRequestError(
            message="Your credit balance is too low to access the Anthropic API.",
            response=MagicMock(status_code=400),
            body={"type": "invalid_request_error"},
        )
    )

    with (
        patch("puxti.cli.settings") as mock_settings,
        patch("puxti.cli.KnowledgeGraph", return_value=mock_graph),
        patch("puxti.cli.anthropic.AsyncAnthropic", return_value=mock_anthropic_client),
    ):
        mock_settings.dbt_project_dir = None
        mock_settings.anthropic_api_key = "sk-ant-real-but-broke"

        result = runner.invoke(app, ["health"])

    assert result.exit_code == 1
    assert "credit balance" in result.output.lower()


def test_health_anthropic_not_configured_exits_nonzero():
    mock_graph = MagicMock()
    mock_graph.connect = AsyncMock()
    mock_graph.close = AsyncMock()

    with (
        patch("puxti.cli.settings") as mock_settings,
        patch("puxti.cli.KnowledgeGraph", return_value=mock_graph),
    ):
        mock_settings.dbt_project_dir = None
        mock_settings.anthropic_api_key = None

        result = runner.invoke(app, ["health"])

    assert result.exit_code == 1
    assert "ANTHROPIC_API_KEY" in result.output


def test_health_neo4j_down_exits_nonzero():
    mock_graph = MagicMock()
    mock_graph.connect = AsyncMock(side_effect=Exception("Connection refused"))
    mock_graph.close = AsyncMock()

    with (
        patch("puxti.cli.settings") as mock_settings,
        patch("puxti.cli.KnowledgeGraph", return_value=mock_graph),
    ):
        mock_settings.dbt_project_dir = None
        mock_settings.anthropic_api_key = None

        result = runner.invoke(app, ["health"])

    assert result.exit_code == 1
    assert "Neo4j" in result.output


def test_health_dbt_not_configured_shows_dash():
    mock_graph = MagicMock()
    mock_graph.connect = AsyncMock()
    mock_graph.close = AsyncMock()

    mock_anthropic_client = MagicMock()
    mock_anthropic_client.messages = MagicMock()
    mock_anthropic_client.messages.count_tokens = AsyncMock(
        return_value=_make_count_tokens_response()
    )

    with (
        patch("puxti.cli.settings") as mock_settings,
        patch("puxti.cli.KnowledgeGraph", return_value=mock_graph),
        patch("puxti.cli.anthropic.AsyncAnthropic", return_value=mock_anthropic_client),
    ):
        mock_settings.dbt_project_dir = None
        mock_settings.anthropic_api_key = "sk-ant-test"

        result = runner.invoke(app, ["health"])

    assert result.exit_code == 0
    assert "not configured" in result.output


# ── health — Airflow connector ────────────────────────────────────────────────

def _health_base_patches(mock_settings, mock_graph, mock_anthropic_client, mock_dbt):
    mock_settings.dbt_project_dir = "/some/dbt"
    mock_settings.anthropic_api_key = "sk-ant-test"
    mock_settings.github_token = None


def test_health_airflow_dags_dir_ok(tmp_path):
    from puxti.workspace import WorkspaceConfig, ConnectorConfig

    mock_graph = MagicMock()
    mock_graph.connect = AsyncMock()
    mock_graph.close = AsyncMock()

    mock_dbt = MagicMock()
    mock_dbt.health_check = AsyncMock(return_value=True)

    mock_airflow = MagicMock()
    mock_airflow.health_check = AsyncMock(return_value=True)

    mock_anthropic_client = MagicMock()
    mock_anthropic_client.messages = MagicMock()
    mock_anthropic_client.messages.count_tokens = AsyncMock(return_value=_make_count_tokens_response())

    ws = WorkspaceConfig(
        dbt=ConnectorConfig(project_dir="/some/dbt"),
        airflow=ConnectorConfig(project_dir=str(tmp_path), extras={"dags_dir": "dags"}),
    )

    with (
        patch("puxti.cli.settings") as mock_settings,
        patch("puxti.cli.KnowledgeGraph", return_value=mock_graph),
        patch("puxti.cli.DbtConnector", return_value=mock_dbt),
        patch("puxti.cli.AirflowConnector", return_value=mock_airflow),
        patch("puxti.cli.anthropic.AsyncAnthropic", return_value=mock_anthropic_client),
        patch("puxti.cli._load_workspace", return_value=ws),
    ):
        mock_settings.dbt_project_dir = "/some/dbt"
        mock_settings.anthropic_api_key = "sk-ant-test"
        mock_settings.github_token = None

        result = runner.invoke(app, ["health"])

    assert "Airflow dags dir" in result.output
    assert "✓" in result.output or "Airflow dags dir" in result.output


def test_health_airflow_dags_dir_missing_exits_nonzero(tmp_path):
    from puxti.workspace import WorkspaceConfig, ConnectorConfig

    mock_graph = MagicMock()
    mock_graph.connect = AsyncMock()
    mock_graph.close = AsyncMock()

    mock_dbt = MagicMock()
    mock_dbt.health_check = AsyncMock(return_value=True)

    mock_airflow = MagicMock()
    mock_airflow.health_check = AsyncMock(return_value=False)

    mock_anthropic_client = MagicMock()
    mock_anthropic_client.messages = MagicMock()
    mock_anthropic_client.messages.count_tokens = AsyncMock(return_value=_make_count_tokens_response())

    ws = WorkspaceConfig(
        dbt=ConnectorConfig(project_dir="/some/dbt"),
        airflow=ConnectorConfig(project_dir=str(tmp_path), extras={"dags_dir": "dags"}),
    )

    with (
        patch("puxti.cli.settings") as mock_settings,
        patch("puxti.cli.KnowledgeGraph", return_value=mock_graph),
        patch("puxti.cli.DbtConnector", return_value=mock_dbt),
        patch("puxti.cli.AirflowConnector", return_value=mock_airflow),
        patch("puxti.cli.anthropic.AsyncAnthropic", return_value=mock_anthropic_client),
        patch("puxti.cli._load_workspace", return_value=ws),
    ):
        mock_settings.dbt_project_dir = "/some/dbt"
        mock_settings.anthropic_api_key = "sk-ant-test"
        mock_settings.github_token = None

        result = runner.invoke(app, ["health"])

    assert result.exit_code == 1
    assert "not found" in result.output


def test_health_airflow_not_configured_shows_dash():
    from puxti.workspace import WorkspaceConfig, ConnectorConfig

    mock_graph = MagicMock()
    mock_graph.connect = AsyncMock()
    mock_graph.close = AsyncMock()

    mock_dbt = MagicMock()
    mock_dbt.health_check = AsyncMock(return_value=True)

    mock_anthropic_client = MagicMock()
    mock_anthropic_client.messages = MagicMock()
    mock_anthropic_client.messages.count_tokens = AsyncMock(return_value=_make_count_tokens_response())

    ws = WorkspaceConfig(dbt=ConnectorConfig(project_dir="/some/dbt"))

    with (
        patch("puxti.cli.settings") as mock_settings,
        patch("puxti.cli.KnowledgeGraph", return_value=mock_graph),
        patch("puxti.cli.DbtConnector", return_value=mock_dbt),
        patch("puxti.cli.anthropic.AsyncAnthropic", return_value=mock_anthropic_client),
        patch("puxti.cli._load_workspace", return_value=ws),
    ):
        mock_settings.dbt_project_dir = "/some/dbt"
        mock_settings.anthropic_api_key = "sk-ant-test"
        mock_settings.github_token = None

        result = runner.invoke(app, ["health"])

    assert "not configured" in result.output


# ── scan ──────────────────────────────────────────────────────────────────────

def test_scan_shows_help():
    result = runner.invoke(app, ["scan", "--help"])
    assert result.exit_code == 0
    assert "--interactive" in plain(result.output)
    assert "--dbt-project-dir" in plain(result.output)


def test_scan_exits_when_no_dbt_project_dir():
    with patch("puxti.cli.settings") as mock_settings:
        mock_settings.dbt_project_dir = None
        result = runner.invoke(app, ["scan"])
    assert result.exit_code == 1
    assert "dbt project directory" in result.output.lower() or "dbt_project_dir" in result.output.lower()


def test_scan_happy_path_prints_summary():
    from puxti.core.scanner import ScanResult

    mock_graph = MagicMock()
    mock_graph.connect = AsyncMock()
    mock_graph.close = AsyncMock()

    mock_scanner = MagicMock()
    mock_scanner.scan = AsyncMock(return_value=ScanResult(
        entities_upserted=12,
        lineage_edges_upserted=8,
        definitions_written=5,
        semantic_edges_written=3,
    ))

    with (
        patch("puxti.cli.settings") as mock_settings,
        patch("puxti.cli.KnowledgeGraph", return_value=mock_graph),
        patch("puxti.cli.SemanticScanner", return_value=mock_scanner),
        patch("puxti.cli.DbtConnector"),
    ):
        mock_settings.dbt_project_dir = "/some/dbt"

        result = runner.invoke(app, ["scan"])

    assert result.exit_code == 0
    assert "12" in result.output   # entities
    assert "5" in result.output    # definitions
    assert "3" in result.output    # semantic edges


# ── redefine ──────────────────────────────────────────────────────────────────

def test_redefine_shows_help():
    result = runner.invoke(app, ["redefine", "--help"])
    assert result.exit_code == 0
    assert "--entity" in plain(result.output)
    assert "--description" in plain(result.output)
    assert "--repo" in plain(result.output)


def test_redefine_exits_when_no_dbt_project_dir():
    with patch("puxti.cli.settings") as mock_settings:
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
    with patch("puxti.cli.settings") as mock_settings:
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
        patch("puxti.cli.settings") as mock_settings,
        patch("puxti.cli.KnowledgeGraph", return_value=mock_graph),
        patch("puxti.cli.GitHubConnector", return_value=mock_gh),
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
        patch("puxti.cli.settings") as mock_settings,
        patch("puxti.cli.KnowledgeGraph", return_value=mock_graph),
        patch("puxti.cli.SemanticRedefiner", return_value=mock_redefiner),
        patch("puxti.cli.GitHubConnector", return_value=mock_gh),
        patch("puxti.cli.DbtConnector"),
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
        patch("puxti.cli.settings") as mock_settings,
        patch("puxti.cli.KnowledgeGraph", return_value=mock_graph),
        patch("puxti.cli.SemanticRedefiner", return_value=mock_redefiner),
        patch("puxti.cli.GitHubConnector", return_value=mock_gh),
        patch("puxti.cli.DbtConnector"),
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
        patch("puxti.cli.settings") as mock_settings,
        patch("puxti.cli.KnowledgeGraph", return_value=mock_graph),
        patch("puxti.cli.DbtConnector", return_value=mock_dbt),
        patch("puxti.cli._anthropic_client_for_redefine_dry_run", create=True),
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
        patch("puxti.cli.settings") as mock_settings,
        patch("puxti.cli.KnowledgeGraph", return_value=mock_graph),
        patch("puxti.cli.DbtConnector", return_value=mock_dbt),
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

    with patch("puxti.cli.KnowledgeGraph", return_value=mock_graph):
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

    with patch("puxti.cli.KnowledgeGraph", return_value=mock_graph):
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

    with patch("puxti.cli.KnowledgeGraph", return_value=mock_graph):
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

    with patch("puxti.cli.KnowledgeGraph", return_value=mock_graph):
        result = runner.invoke(app, ["describe", "--project", "nonexistent"])

    assert result.exit_code == 1


def test_describe_single_entity_not_found_exits_nonzero():
    mock_graph = MagicMock()
    mock_graph.connect = AsyncMock()
    mock_graph.close = AsyncMock()
    mock_graph.get_entity_by_id = AsyncMock(return_value=None)

    with patch("puxti.cli.KnowledgeGraph", return_value=mock_graph):
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

    with patch("puxti.cli.KnowledgeGraph", return_value=mock_graph):
        result = runner.invoke(app, ["describe", "--entity", "model.jaffle_shop.orders"])

    assert result.exit_code == 0
    assert "One row per settled order" in result.output
    assert "derived_from" in result.output
    assert "customers" in result.output


# ── purge — argument validation and confirmation ──────────────────────────────

def test_purge_shows_help():
    result = runner.invoke(app, ["purge", "--help"])
    assert result.exit_code == 0
    assert "--project" in plain(result.output)
    assert "--all" in plain(result.output)


def test_purge_exits_when_no_flags():
    result = runner.invoke(app, ["purge"])
    assert result.exit_code == 1


def test_purge_exits_when_both_flags():
    result = runner.invoke(app, ["purge", "--project", "jaffle_shop", "--all"])
    assert result.exit_code == 1


def test_purge_project_cancels_on_non_yes():
    mock_graph = MagicMock()
    mock_graph.connect = AsyncMock()
    mock_graph.close = AsyncMock()
    mock_graph.get_projects = AsyncMock(return_value=["jaffle_shop"])
    mock_graph.purge_project = AsyncMock(return_value=10)

    with patch("puxti.cli.KnowledgeGraph", return_value=mock_graph):
        result = runner.invoke(app, ["purge", "--project", "jaffle_shop"], input="n\n")

    assert result.exit_code == 0
    assert "cancelled" in result.output.lower()
    mock_graph.purge_project.assert_not_called()


def test_purge_project_deletes_on_yes():
    mock_graph = MagicMock()
    mock_graph.connect = AsyncMock()
    mock_graph.close = AsyncMock()
    mock_graph.get_projects = AsyncMock(return_value=["jaffle_shop"])
    mock_graph.purge_project = AsyncMock(return_value=10)

    with patch("puxti.cli.KnowledgeGraph", return_value=mock_graph):
        result = runner.invoke(app, ["purge", "--project", "jaffle_shop"], input="yes\n")

    assert result.exit_code == 0
    mock_graph.purge_project.assert_called_once_with("jaffle_shop")
    assert "jaffle_shop" in result.output


def test_purge_project_exits_when_project_not_in_graph():
    mock_graph = MagicMock()
    mock_graph.connect = AsyncMock()
    mock_graph.close = AsyncMock()
    mock_graph.get_projects = AsyncMock(return_value=["other_project"])

    with patch("puxti.cli.KnowledgeGraph", return_value=mock_graph):
        result = runner.invoke(app, ["purge", "--project", "jaffle_shop"])

    assert result.exit_code == 1


def test_purge_all_cancels_on_non_yes():
    mock_graph = MagicMock()
    mock_graph.connect = AsyncMock()
    mock_graph.close = AsyncMock()
    mock_graph.get_projects = AsyncMock(return_value=["jaffle_shop"])
    mock_graph.purge_all = AsyncMock(return_value=42)

    with patch("puxti.cli.KnowledgeGraph", return_value=mock_graph):
        result = runner.invoke(app, ["purge", "--all"], input="no\n")

    assert result.exit_code == 0
    assert "cancelled" in result.output.lower()
    mock_graph.purge_all.assert_not_called()


def test_purge_all_deletes_on_yes():
    mock_graph = MagicMock()
    mock_graph.connect = AsyncMock()
    mock_graph.close = AsyncMock()
    mock_graph.get_projects = AsyncMock(return_value=["jaffle_shop", "sports_sims"])
    mock_graph.purge_all = AsyncMock(return_value=42)

    with patch("puxti.cli.KnowledgeGraph", return_value=mock_graph):
        result = runner.invoke(app, ["purge", "--all"], input="yes\n")

    assert result.exit_code == 0
    mock_graph.purge_all.assert_called_once()
    assert "42" in result.output


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

    with patch("puxti.cli.KnowledgeGraph", return_value=mock_graph):
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

    with patch("puxti.cli.KnowledgeGraph", return_value=mock_graph):
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

    with patch("puxti.cli.KnowledgeGraph", return_value=mock_graph):
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

    with patch("puxti.cli.KnowledgeGraph", return_value=mock_graph):
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

    with patch("puxti.cli.KnowledgeGraph", return_value=mock_graph):
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
        patch("puxti.cli.KnowledgeGraph", return_value=mock_graph),
        patch("puxti.cli.SemanticCorrector", return_value=mock_corrector),
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
        patch("puxti.cli.KnowledgeGraph", return_value=mock_graph),
        patch("puxti.cli.SemanticCorrector", return_value=mock_corrector),
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
        patch("puxti.cli.KnowledgeGraph", return_value=mock_graph),
        patch("puxti.cli.SemanticCorrector", return_value=mock_corrector),
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
        patch("puxti.cli.settings") as mock_settings,
        patch("puxti.cli.KnowledgeGraph", return_value=mock_graph),
        patch("puxti.cli.SemanticRedefiner", return_value=mock_redefiner),
        patch("puxti.cli.GitHubConnector", return_value=mock_gh),
        patch("puxti.cli.DbtConnector"),
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
    mock_graph.get_entity_by_id = AsyncMock(return_value=MagicMock())

    mock_capture = MagicMock()
    mock_capture.estimate_cost = AsyncMock(return_value={
        "input_tokens": 350,
        "estimated_output_tokens": 614,
        "estimated_cost_usd": 0.0105,
    })

    with (
        patch("puxti.cli.settings") as mock_settings,
        patch("puxti.cli.KnowledgeGraph", return_value=mock_graph),
        patch("puxti.cli.SemanticCapture", return_value=mock_capture),
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


# ── puxti link ────────────────────────────────────────────────────────────────

def test_link_shows_help():
    result = runner.invoke(app, ["link", "--help"])
    assert result.exit_code == 0
    plain = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
    assert "--from" in plain
    assert "--to" in plain
    assert "--description" in plain


def test_link_missing_args_exits_nonzero():
    result = runner.invoke(app, ["link"])
    assert result.exit_code != 0


def test_link_invalid_from_entity_exits_1():
    result = runner.invoke(app, [
        "link",
        "--from", "invalid.entity.id",
        "--to", "source.clariva.raw_opportunities",
        "--description", "test",
    ])
    assert result.exit_code == 1
    assert "Unrecognized entity ID" in plain(result.output)


def test_link_invalid_to_entity_exits_1():
    result = runner.invoke(app, [
        "link",
        "--from", "task.airflow.salesforce_sync.extract_opportunities",
        "--to", "bad_prefix.table",
        "--description", "test",
    ])
    assert result.exit_code == 1
    assert "Unrecognized entity ID" in plain(result.output)


def test_link_happy_path_writes_feeds_edge():
    """link creates both entities and a FEEDS semantic edge in the graph."""
    from puxti.models import Entity, EntityType

    stored_from = Entity(
        id="id-from",
        name="task.airflow.salesforce_sync.extract_opportunities",
        type=EntityType.TASK,
        source_connector="airflow",
        project="salesforce_sync",
    )
    stored_to = Entity(
        id="id-to",
        name="source.clariva.raw_opportunities",
        type=EntityType.TABLE,
        source_connector="dbt",
        project="clariva",
    )

    mock_graph = MagicMock()
    mock_graph.connect = AsyncMock()
    mock_graph.close = AsyncMock()
    mock_graph.upsert_entity_by_name = AsyncMock(side_effect=[stored_from, stored_to])
    mock_graph.upsert_semantic_edge = AsyncMock()

    with patch("puxti.cli.KnowledgeGraph", return_value=mock_graph):
        result = runner.invoke(app, [
            "link",
            "--from", "task.airflow.salesforce_sync.extract_opportunities",
            "--to", "source.clariva.raw_opportunities",
            "--description", "Extracts Salesforce opportunities. amount is a roll-up.",
        ])

    assert result.exit_code == 0, result.output
    assert "FEEDS" in result.output
    assert "task.airflow.salesforce_sync.extract_opportunities" in result.output
    assert "source.clariva.raw_opportunities" in result.output
    mock_graph.upsert_entity_by_name.assert_awaited()
    mock_graph.upsert_semantic_edge.assert_awaited_once()

    edge_call = mock_graph.upsert_semantic_edge.call_args[0][0]
    from puxti.models import EdgeType
    assert edge_call.type == EdgeType.FEEDS
    assert edge_call.from_entity_id == "id-from"
    assert edge_call.to_entity_id == "id-to"
    assert edge_call.created_by == "user"


def test_parse_entity_id_task():
    from puxti.cli import _parse_entity_id
    from puxti.models import EntityType
    entity_type, connector, project = _parse_entity_id("task.airflow.salesforce_sync.extract_opportunities")
    assert entity_type == EntityType.TASK
    assert connector == "airflow"
    assert project == "salesforce_sync"


def test_parse_entity_id_source():
    from puxti.cli import _parse_entity_id
    from puxti.models import EntityType
    entity_type, connector, project = _parse_entity_id("source.clariva.raw_opportunities")
    assert entity_type == EntityType.TABLE
    assert connector == "dbt"
    assert project == "clariva"


def test_parse_entity_id_model():
    from puxti.cli import _parse_entity_id
    from puxti.models import EntityType
    entity_type, connector, project = _parse_entity_id("model.clariva.stg_opportunities")
    assert entity_type == EntityType.MODEL
    assert connector == "dbt"
    assert project == "clariva"


def test_parse_entity_id_unknown_raises():
    from puxti.cli import _parse_entity_id
    import pytest
    with pytest.raises(ValueError, match="Unrecognized entity ID"):
        _parse_entity_id("unknown.prefix.thing")
