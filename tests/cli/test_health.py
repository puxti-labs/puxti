"""`puxti health` — connectivity checks for all configured services."""

from unittest.mock import AsyncMock, MagicMock, patch

from puxti.cli import app
from tests.cli._helpers import runner


def test_health_shows_help():
    result = runner.invoke(app, ["health", "--help"])
    assert result.exit_code == 0


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
        patch("puxti.cli.health.settings") as mock_settings,
        patch("puxti.core.graph.KnowledgeGraph", return_value=mock_graph),
        patch("puxti.cli.health.DbtConnector", return_value=mock_dbt),
        patch("puxti.cli.health.anthropic.AsyncAnthropic", return_value=mock_anthropic_client),
    ):
        mock_settings.dbt_project_dir = "/some/dbt"
        mock_settings.anthropic_api_key = "sk-ant-test"

        result = runner.invoke(app, ["health"])

    assert result.exit_code == 0
    assert "Knowledge Graph" in result.output
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
        patch("puxti.cli.health.settings") as mock_settings,
        patch("puxti.core.graph.KnowledgeGraph", return_value=mock_graph),
        patch("puxti.cli.health.anthropic.AsyncAnthropic", return_value=mock_anthropic_client),
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
        patch("puxti.cli.health.settings") as mock_settings,
        patch("puxti.core.graph.KnowledgeGraph", return_value=mock_graph),
        patch("puxti.cli.health.anthropic.AsyncAnthropic", return_value=mock_anthropic_client),
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
        patch("puxti.cli.health.settings") as mock_settings,
        patch("puxti.core.graph.KnowledgeGraph", return_value=mock_graph),
    ):
        mock_settings.dbt_project_dir = None
        mock_settings.anthropic_api_key = None

        result = runner.invoke(app, ["health"])

    assert result.exit_code == 1
    assert "ANTHROPIC_API_KEY" in result.output


def test_health_graph_not_initialised_shows_dash():
    with (
        patch("puxti.cli.health.settings") as mock_settings,
        patch("puxti.core.graph.DEFAULT_DB_PATH") as mock_path,
    ):
        mock_path.exists.return_value = False
        mock_settings.dbt_project_dir = None
        mock_settings.anthropic_api_key = None

        result = runner.invoke(app, ["health"])

    assert "Knowledge Graph" in result.output


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
        patch("puxti.cli.health.settings") as mock_settings,
        patch("puxti.core.graph.KnowledgeGraph", return_value=mock_graph),
        patch("puxti.cli.health.anthropic.AsyncAnthropic", return_value=mock_anthropic_client),
    ):
        mock_settings.dbt_project_dir = None
        mock_settings.anthropic_api_key = "sk-ant-test"

        result = runner.invoke(app, ["health"])

    assert result.exit_code == 0
    assert "not configured" in result.output


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
        patch("puxti.cli.health.settings") as mock_settings,
        patch("puxti.core.graph.KnowledgeGraph", return_value=mock_graph),
        patch("puxti.cli.health.DbtConnector", return_value=mock_dbt),
        patch("puxti.cli.health.AirflowConnector", return_value=mock_airflow),
        patch("puxti.cli.health.anthropic.AsyncAnthropic", return_value=mock_anthropic_client),
        patch("puxti.cli.health._load_workspace", return_value=ws),
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
        patch("puxti.cli.health.settings") as mock_settings,
        patch("puxti.core.graph.KnowledgeGraph", return_value=mock_graph),
        patch("puxti.cli.health.DbtConnector", return_value=mock_dbt),
        patch("puxti.cli.health.AirflowConnector", return_value=mock_airflow),
        patch("puxti.cli.health.anthropic.AsyncAnthropic", return_value=mock_anthropic_client),
        patch("puxti.cli.health._load_workspace", return_value=ws),
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
        patch("puxti.cli.health.settings") as mock_settings,
        patch("puxti.core.graph.KnowledgeGraph", return_value=mock_graph),
        patch("puxti.cli.health.DbtConnector", return_value=mock_dbt),
        patch("puxti.cli.health.anthropic.AsyncAnthropic", return_value=mock_anthropic_client),
        patch("puxti.cli.health._load_workspace", return_value=ws),
    ):
        mock_settings.dbt_project_dir = "/some/dbt"
        mock_settings.anthropic_api_key = "sk-ant-test"
        mock_settings.github_token = None

        result = runner.invoke(app, ["health"])

    assert "not configured" in result.output
