"""`puxti health` — connectivity checks for all configured services."""

from unittest.mock import AsyncMock, MagicMock, patch

from puxti.cli import app
from puxti.llm import LLMAuthError, LLMBillingError
from tests.cli._helpers import plain, runner


def test_health_shows_help():
    result = runner.invoke(app, ["health", "--help"])
    assert result.exit_code == 0


# ── health ────────────────────────────────────────────────────────────────────

def _make_ok_backend() -> MagicMock:
    backend = MagicMock()
    backend.provider = "anthropic"
    backend.key_configured = True
    backend.auth_check = AsyncMock()
    return backend


def test_health_all_ok():
    mock_graph = MagicMock()
    mock_graph.connect = AsyncMock()
    mock_graph.close = AsyncMock()

    mock_dbt = MagicMock()
    mock_dbt.health_check = AsyncMock(return_value=True)

    mock_backend = _make_ok_backend()

    with (
        patch("puxti.cli.health.settings") as mock_settings,
        patch("puxti.core.graph.KnowledgeGraph", return_value=mock_graph),
        patch("puxti.cli.health.DbtConnector", return_value=mock_dbt),
        patch("puxti.cli.health.get_backend", return_value=mock_backend),
    ):
        mock_settings.dbt_project_dir = "/some/dbt"
        mock_settings.anthropic_api_key = "sk-ant-test"

        result = runner.invoke(app, ["health"])

    assert result.exit_code == 0
    assert "Knowledge Graph" in result.output
    assert "Anthropic" in result.output
    assert "dbt" in result.output


def test_health_anthropic_invalid_key_exits_nonzero():
    mock_graph = MagicMock()
    mock_graph.connect = AsyncMock()
    mock_graph.close = AsyncMock()

    mock_backend = _make_ok_backend()
    mock_backend.auth_check = AsyncMock(side_effect=LLMAuthError(
        "Anthropic API key is invalid or expired. Check ANTHROPIC_API_KEY."
    ))

    with (
        patch("puxti.cli.health.settings") as mock_settings,
        patch("puxti.core.graph.KnowledgeGraph", return_value=mock_graph),
        patch("puxti.cli.health.get_backend", return_value=mock_backend),
    ):
        mock_settings.dbt_project_dir = None
        mock_settings.anthropic_api_key = "sk-ant-bad"

        result = runner.invoke(app, ["health"])

    assert result.exit_code == 1
    assert "invalid" in result.output.lower()


def test_health_anthropic_credit_error_exits_nonzero():
    mock_graph = MagicMock()
    mock_graph.connect = AsyncMock()
    mock_graph.close = AsyncMock()

    mock_backend = _make_ok_backend()
    mock_backend.auth_check = AsyncMock(side_effect=LLMBillingError(
        "Anthropic API credit balance is too low."
    ))

    with (
        patch("puxti.cli.health.settings") as mock_settings,
        patch("puxti.core.graph.KnowledgeGraph", return_value=mock_graph),
        patch("puxti.cli.health.get_backend", return_value=mock_backend),
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

    mock_backend = _make_ok_backend()
    mock_backend.key_configured = False

    with (
        patch("puxti.cli.health.settings") as mock_settings,
        patch("puxti.core.graph.KnowledgeGraph", return_value=mock_graph),
        patch("puxti.cli.health.get_backend", return_value=mock_backend),
    ):
        mock_settings.dbt_project_dir = None

        result = runner.invoke(app, ["health"])

    assert result.exit_code == 1
    assert "ANTHROPIC_API_KEY" in result.output


def test_health_graph_not_initialised_shows_dash():
    mock_backend = _make_ok_backend()
    mock_backend.key_configured = False

    with (
        patch("puxti.cli.health.settings") as mock_settings,
        patch("puxti.core.graph.DEFAULT_DB_PATH") as mock_path,
        patch("puxti.cli.health.get_backend", return_value=mock_backend),
    ):
        mock_path.exists.return_value = False
        mock_settings.dbt_project_dir = None

        result = runner.invoke(app, ["health"])

    assert "Knowledge Graph" in result.output


def test_health_dbt_not_configured_shows_dash():
    mock_graph = MagicMock()
    mock_graph.connect = AsyncMock()
    mock_graph.close = AsyncMock()

    mock_backend = _make_ok_backend()

    with (
        patch("puxti.cli.health.settings") as mock_settings,
        patch("puxti.core.graph.KnowledgeGraph", return_value=mock_graph),
        patch("puxti.cli.health.get_backend", return_value=mock_backend),
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

    mock_backend = _make_ok_backend()

    ws = WorkspaceConfig(
        dbt=ConnectorConfig(project_dir="/some/dbt"),
        airflow=ConnectorConfig(project_dir=str(tmp_path), extras={"dags_dir": "dags"}),
    )

    with (
        patch("puxti.cli.health.settings") as mock_settings,
        patch("puxti.core.graph.KnowledgeGraph", return_value=mock_graph),
        patch("puxti.cli.health.DbtConnector", return_value=mock_dbt),
        patch("puxti.cli.health.AirflowConnector", return_value=mock_airflow),
        patch("puxti.cli.health.get_backend", return_value=mock_backend),
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

    mock_backend = _make_ok_backend()

    ws = WorkspaceConfig(
        dbt=ConnectorConfig(project_dir="/some/dbt"),
        airflow=ConnectorConfig(project_dir=str(tmp_path), extras={"dags_dir": "dags"}),
    )

    with (
        patch("puxti.cli.health.settings") as mock_settings,
        patch("puxti.core.graph.KnowledgeGraph", return_value=mock_graph),
        patch("puxti.cli.health.DbtConnector", return_value=mock_dbt),
        patch("puxti.cli.health.AirflowConnector", return_value=mock_airflow),
        patch("puxti.cli.health.get_backend", return_value=mock_backend),
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

    mock_backend = _make_ok_backend()

    ws = WorkspaceConfig(dbt=ConnectorConfig(project_dir="/some/dbt"))

    with (
        patch("puxti.cli.health.settings") as mock_settings,
        patch("puxti.core.graph.KnowledgeGraph", return_value=mock_graph),
        patch("puxti.cli.health.DbtConnector", return_value=mock_dbt),
        patch("puxti.cli.health.get_backend", return_value=mock_backend),
        patch("puxti.cli.health._load_workspace", return_value=ws),
    ):
        mock_settings.dbt_project_dir = "/some/dbt"
        mock_settings.anthropic_api_key = "sk-ant-test"
        mock_settings.github_token = None

        result = runner.invoke(app, ["health"])

    assert "not configured" in result.output


def test_health_reports_llm_config_error():
    """Incomplete provider config surfaces as a failing health line."""
    from puxti.llm import LLMConfigError

    with (
        patch("puxti.cli.health.settings") as mock_settings,
        patch("puxti.cli.health.get_backend",
              side_effect=LLMConfigError("LLM_MODEL is required when LLM_PROVIDER='mistral'")),
    ):
        mock_settings.dbt_project_dir = None

        result = runner.invoke(app, ["health"])

    assert result.exit_code == 1
    assert "LLM_MODEL" in result.output


def test_health_labels_non_anthropic_provider():
    mock_backend = _make_ok_backend()
    mock_backend.provider = "mistral"

    with (
        patch("puxti.cli.health.settings") as mock_settings,
        patch("puxti.cli.health.get_backend", return_value=mock_backend),
    ):
        mock_settings.dbt_project_dir = None

        result = runner.invoke(app, ["health"])

    # plain() — Rich bolds parentheses when FORCE_COLOR is present (CI)
    assert "LLM API key (mistral)" in plain(result.output)


def test_health_checks_prisma_and_sql_views_when_configured(tmp_path):
    """Configured prisma/sql_views connectors get their own health lines."""
    from puxti.workspace import ConnectorConfig, WorkspaceConfig

    schema = tmp_path / "prisma" / "schema.prisma"
    schema.parent.mkdir()
    schema.write_text("model User { id Int @id }\n")
    views = tmp_path / "db" / "views"
    views.mkdir(parents=True)

    ws = WorkspaceConfig(
        prisma=ConnectorConfig(project_dir=str(tmp_path)),
        sql_views=ConnectorConfig(project_dir=str(tmp_path), extras={"views_dir": "db/views"}),
    )

    mock_backend = _make_ok_backend()

    with (
        patch("puxti.cli.health.settings") as mock_settings,
        patch("puxti.cli.health.get_backend", return_value=mock_backend),
        patch("puxti.cli._shared.load_workspace", return_value=ws),
    ):
        mock_settings.dbt_project_dir = None
        mock_settings.github_token = None

        result = runner.invoke(app, ["health"])

    assert "Prisma schema" in plain(result.output)
    assert "SQL views dir" in plain(result.output)


def test_health_fails_when_prisma_schema_missing(tmp_path):
    from puxti.workspace import ConnectorConfig, WorkspaceConfig

    ws = WorkspaceConfig(prisma=ConnectorConfig(project_dir=str(tmp_path)))
    mock_backend = _make_ok_backend()

    with (
        patch("puxti.cli.health.settings") as mock_settings,
        patch("puxti.cli.health.get_backend", return_value=mock_backend),
        patch("puxti.cli._shared.load_workspace", return_value=ws),
    ):
        mock_settings.dbt_project_dir = None
        mock_settings.github_token = None

        result = runner.invoke(app, ["health"])

    assert result.exit_code == 1
    assert "schema.prisma not found" in plain(result.output)


def test_health_silent_for_unconfigured_prisma_and_sql_views():
    """No prisma/sql_views section in .puxti.yml → no lines about them."""
    from puxti.workspace import WorkspaceConfig

    mock_backend = _make_ok_backend()

    with (
        patch("puxti.cli.health.settings") as mock_settings,
        patch("puxti.cli.health.get_backend", return_value=mock_backend),
        patch("puxti.cli._shared.load_workspace", return_value=WorkspaceConfig()),
    ):
        mock_settings.dbt_project_dir = None
        mock_settings.github_token = None

        result = runner.invoke(app, ["health"])

    assert "Prisma" not in result.output
    assert "SQL views" not in result.output
