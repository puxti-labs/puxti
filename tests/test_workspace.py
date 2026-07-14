"""Tests for .puxti.yml workspace config loading."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from puxti.workspace import WorkspaceConfig, load_workspace


# ── load_workspace — file discovery ───────────────────────────────────────────

def test_returns_empty_config_when_no_file(tmp_path):
    result = load_workspace(start_dir=tmp_path)
    assert result.dbt is None
    assert result.airflow is None
    assert result.path is None


def test_finds_config_in_start_dir(tmp_path):
    config = tmp_path / ".puxti.yml"
    config.write_text("version: 1\n")
    result = load_workspace(start_dir=tmp_path)
    assert result.path == config


def test_walks_up_to_find_config(tmp_path):
    config = tmp_path / ".puxti.yml"
    config.write_text("version: 1\n")
    subdir = tmp_path / "a" / "b" / "c"
    subdir.mkdir(parents=True)
    result = load_workspace(start_dir=subdir)
    assert result.path == config


def test_stops_at_nearest_ancestor(tmp_path):
    parent_config = tmp_path / ".puxti.yml"
    parent_config.write_text("version: 1\nconnectors:\n  dbt:\n    repo: parent/repo\n")
    child_dir = tmp_path / "child"
    child_dir.mkdir()
    child_config = child_dir / ".puxti.yml"
    child_config.write_text("version: 1\nconnectors:\n  dbt:\n    repo: child/repo\n")
    result = load_workspace(start_dir=child_dir)
    assert result.dbt is not None
    assert result.dbt.repo == "child/repo"


# ── load_workspace — parsing ───────────────────────────────────────────────────

def test_parses_dbt_connector(tmp_path):
    (tmp_path / ".puxti.yml").write_text("""
version: 1
connectors:
  dbt:
    project_dir: ./transform
    repo: acme/data
    repo_subdir: transform/
    base_branch: develop
""")
    result = load_workspace(start_dir=tmp_path)
    assert result.dbt is not None
    assert result.dbt.project_dir == "./transform"
    assert result.dbt.repo == "acme/data"
    assert result.dbt.repo_subdir == "transform/"
    assert result.dbt.base_branch == "develop"


def test_parses_airflow_connector(tmp_path):
    (tmp_path / ".puxti.yml").write_text("""
version: 1
connectors:
  airflow:
    project_dir: ./orchestration
    repo: acme/airflow
    dags_dir: dags/
""")
    result = load_workspace(start_dir=tmp_path)
    assert result.airflow is not None
    assert result.airflow.repo == "acme/airflow"
    assert result.airflow.extras.get("dags_dir") == "dags/"


def test_parses_both_connectors(tmp_path):
    (tmp_path / ".puxti.yml").write_text("""
version: 1
connectors:
  dbt:
    repo: acme/data
  airflow:
    repo: acme/airflow
""")
    result = load_workspace(start_dir=tmp_path)
    assert result.dbt is not None
    assert result.airflow is not None


def test_missing_connector_is_none(tmp_path):
    (tmp_path / ".puxti.yml").write_text("version: 1\nconnectors:\n  dbt:\n    repo: acme/data\n")
    result = load_workspace(start_dir=tmp_path)
    assert result.dbt is not None
    assert result.airflow is None


def test_base_branch_defaults_to_main(tmp_path):
    (tmp_path / ".puxti.yml").write_text("version: 1\nconnectors:\n  dbt:\n    repo: acme/data\n")
    result = load_workspace(start_dir=tmp_path)
    assert result.dbt.base_branch == "main"


def test_empty_file_returns_empty_config(tmp_path):
    (tmp_path / ".puxti.yml").write_text("")
    result = load_workspace(start_dir=tmp_path)
    assert result.dbt is None
    assert result.path is not None


def test_version_field_is_optional(tmp_path):
    (tmp_path / ".puxti.yml").write_text("connectors:\n  dbt:\n    repo: acme/data\n")
    result = load_workspace(start_dir=tmp_path)
    assert result.dbt.repo == "acme/data"


# ── load_workspace — error cases ──────────────────────────────────────────────

def test_raises_on_invalid_yaml(tmp_path):
    (tmp_path / ".puxti.yml").write_text("version: 1\nconnectors: [invalid: yaml: here\n")
    with pytest.raises(ValueError, match="Invalid .puxti.yml"):
        load_workspace(start_dir=tmp_path)


def test_raises_on_unsupported_version(tmp_path):
    (tmp_path / ".puxti.yml").write_text("version: 99\n")
    with pytest.raises(ValueError, match="Unsupported .puxti.yml version"):
        load_workspace(start_dir=tmp_path)


# ── WorkspaceConfig.connector_repos ───────────────────────────────────────────

def test_connector_repos_returns_configured_repos(tmp_path):
    (tmp_path / ".puxti.yml").write_text("""
version: 1
connectors:
  dbt:
    repo: acme/data
  airflow:
    repo: acme/airflow
""")
    ws = load_workspace(start_dir=tmp_path)
    repos = ws.connector_repos()
    assert ("acme/data", "dbt") in repos
    assert ("acme/airflow", "airflow") in repos


def test_connector_repos_skips_connectors_without_repo(tmp_path):
    (tmp_path / ".puxti.yml").write_text("""
version: 1
connectors:
  dbt:
    project_dir: ./transform
""")
    ws = load_workspace(start_dir=tmp_path)
    assert ws.connector_repos() == []


def test_connector_repos_empty_when_no_workspace():
    ws = WorkspaceConfig()
    assert ws.connector_repos() == []


# ── CLI integration ────────────────────────────────────────────────────────────

def test_cli_capture_uses_workspace_repo(tmp_path):
    """capture reads --repo from .puxti.yml when flag is not passed."""
    from unittest.mock import AsyncMock
    from typer.testing import CliRunner
    from puxti.cli import app
    from puxti.models import ChangeType, FileDiff, PropagationResult, SemanticChangeEvent

    (tmp_path / ".puxti.yml").write_text(
        "version: 1\nconnectors:\n  dbt:\n    repo: acme/data\n    project_dir: /some/dbt\n"
    )

    semantic_event = SemanticChangeEvent(
        change_event_id="evt-ws",
        entity_id="model.shop.orders.date",
        change_type=ChangeType.STRUCTURAL,
        semantic_context="renamed",
        affected_entity_ids=[],
        reasoning="",
        change={"before": {"name": "date"}, "after": {"name": "recorded_date"}},
    )
    diff = FileDiff(file_path="models/orders.sql", before="x", after="y", connector="dbt", description="d")
    pr_result = PropagationResult(
        change_event_id="evt-ws", connector="dbt", target_entity_id="model.shop.orders.date",
        diffs=[diff], pr_url="https://github.com/acme/data/pull/1", status="opened",
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
    mock_engine.propagate = AsyncMock(return_value=[pr_result])
    mock_gh = MagicMock()
    mock_gh.health_check = AsyncMock(return_value=True)
    mock_gh.open_pr = AsyncMock(return_value=pr_result)

    runner = CliRunner()
    with (
        patch("puxti.cli.capture.settings") as mock_settings,
        patch("puxti.cli.capture.KnowledgeGraph", return_value=mock_graph),
        patch("puxti.cli.capture.SemanticCapture", return_value=mock_capture),
        patch("puxti.cli.capture.PropagationEngine", return_value=mock_engine),
        patch("puxti.cli.capture.GitHubConnector", return_value=mock_gh),
        patch("puxti.cli._shared.load_workspace", return_value=load_workspace(start_dir=tmp_path)),
    ):
        mock_settings.dbt_project_dir = None
        mock_settings.github_token = "ghp_test"

        result = runner.invoke(app, [
            "capture",
            "--entity", "model.shop.orders.date",
            "--before", "date",
            "--after", "recorded_date",
            "--description", "renamed",
            # no --repo — should come from workspace
        ], input="y\n")

    assert result.exit_code == 0, result.output
    assert "https://github.com/acme/data/pull/1" in result.output


def test_cli_capture_flag_overrides_workspace(tmp_path):
    """--repo flag beats .puxti.yml."""
    (tmp_path / ".puxti.yml").write_text(
        "version: 1\nconnectors:\n  dbt:\n    repo: workspace/repo\n"
    )

    from typer.testing import CliRunner
    from puxti.cli import app
    from puxti.workspace import ConnectorConfig

    ws = WorkspaceConfig(dbt=ConnectorConfig(repo="workspace/repo"))
    runner = CliRunner()

    with (
        patch("puxti.cli.capture.settings") as mock_settings,
        patch("puxti.cli._shared.load_workspace", return_value=ws),
    ):
        mock_settings.dbt_project_dir = "/some/dbt"
        mock_settings.github_token = "ghp_test"

        # Patch _run_capture to capture which repo it receives
        received = {}

        async def fake_run_capture(**kwargs):
            received["repo"] = kwargs["repo"]

        with patch("puxti.cli.capture._run_capture", side_effect=fake_run_capture):
            runner.invoke(app, [
                "capture",
                "--entity", "model.shop.orders.date",
                "--before", "date",
                "--after", "recorded_date",
                "--description", "renamed",
                "--repo", "flag/repo",
            ], input="y\n")

    assert received.get("repo") == "flag/repo"


def test_cli_capture_exits_when_no_repo_and_no_workspace():
    """No --repo and no .puxti.yml → exit 1 with guidance."""
    from typer.testing import CliRunner
    from puxti.cli import app

    runner = CliRunner()
    with (
        patch("puxti.cli._shared.load_workspace", return_value=WorkspaceConfig()),
    ):
        result = runner.invoke(app, [
            "capture",
            "--entity", "model.shop.orders.date",
            "--before", "date",
            "--after", "recorded_date",
            "--description", "renamed",
        ])

    assert result.exit_code == 1
    assert ".puxti.yml" in result.output or "repo" in result.output.lower()


def test_cli_health_checks_workspace_repos():
    """health command runs GitHub write-access check for each connector repo in workspace."""
    from unittest.mock import AsyncMock
    from typer.testing import CliRunner
    from puxti.cli import app
    from puxti.workspace import ConnectorConfig

    ws = WorkspaceConfig(
        dbt=ConnectorConfig(repo="acme/data"),
        airflow=ConnectorConfig(repo="acme/airflow"),
    )

    mock_graph = MagicMock()
    mock_graph.connect = AsyncMock()
    mock_graph.close = AsyncMock()

    mock_backend = MagicMock()
    mock_backend.auth_check = AsyncMock()

    mock_gh = MagicMock()
    mock_gh.health_check = AsyncMock(return_value=True)

    runner = CliRunner()
    with (
        patch("puxti.cli.health.settings") as mock_settings,
        # health never touches the graph class — patch it in its home module
        # so this stays a harmless no-op, as it always was.
        patch("puxti.core.graph.KnowledgeGraph", return_value=mock_graph),
        patch("puxti.cli.health.DbtConnector"),
        patch("puxti.cli.health.get_backend", return_value=mock_backend),
        patch("puxti.cli.health.GitHubConnector", return_value=mock_gh),
        patch("puxti.cli._shared.load_workspace", return_value=ws),
    ):
        mock_settings.dbt_project_dir = None
        mock_settings.anthropic_api_key = "sk-ant-test"
        mock_settings.github_token = "ghp_test"

        result = runner.invoke(app, ["health"])

    assert result.exit_code == 0, result.output
    assert "acme/data" in result.output
    assert "acme/airflow" in result.output
    assert "dbt" in result.output
    assert "airflow" in result.output
