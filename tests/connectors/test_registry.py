"""Connector registry — building producers from workspace config."""

import pytest

from puxti.connectors.airflow import AirflowConnector
from puxti.connectors.dbt import DbtConnector
from puxti.connectors.prisma import PrismaConnector
from puxti.connectors.registry import build_configured_connectors, build_connector
from puxti.connectors.sql_views import SqlViewsConnector
from puxti.workspace import ConnectorConfig, WorkspaceConfig


def test_build_connector_dbt():
    connector = build_connector("dbt", ConnectorConfig(project_dir="/some/dbt"))
    assert isinstance(connector, DbtConnector)
    assert connector.name == "dbt"


def test_build_connector_airflow_joins_dags_dir():
    connector = build_connector(
        "airflow",
        ConnectorConfig(project_dir="/some/airflow", extras={"dags_dir": "pipelines"}),
    )
    assert isinstance(connector, AirflowConnector)
    assert str(connector.dags_dir) == "/some/airflow/pipelines"


def test_build_connector_prisma_defaults_schema_path():
    connector = build_connector("prisma", ConnectorConfig(project_dir="/app"))
    assert isinstance(connector, PrismaConnector)
    assert str(connector.schema_path) == "/app/prisma/schema.prisma"


def test_build_connector_sql_views_passes_extras():
    connector = build_connector(
        "sql_views",
        ConnectorConfig(
            project_dir="/app",
            extras={"views_dir": "db/views", "dialect": "postgres", "default_schema": "core"},
        ),
    )
    assert isinstance(connector, SqlViewsConnector)
    assert str(connector.views_dir) == "/app/db/views"
    assert connector.dialect == "postgres"
    assert connector.default_schema == "core"


def test_build_connector_returns_none_without_project_dir():
    assert build_connector("prisma", ConnectorConfig(repo="acme/app")) is None


def test_build_connector_unknown_name_raises():
    with pytest.raises(ValueError, match="Unknown connector"):
        build_connector("looker", ConnectorConfig(project_dir="/x"))


def test_build_configured_connectors_dbt_first():
    ws = WorkspaceConfig(
        sql_views=ConnectorConfig(project_dir="/app"),
        prisma=ConnectorConfig(project_dir="/app"),
        dbt=ConnectorConfig(project_dir="/dbt"),
    )
    connectors = build_configured_connectors(ws)
    assert [c.name for c in connectors] == ["dbt", "prisma", "sql_views"]


def test_build_configured_connectors_dbt_project_dir_override():
    ws = WorkspaceConfig(dbt=ConnectorConfig(project_dir="/ws/dbt", repo="acme/data"))
    connectors = build_configured_connectors(ws, dbt_project_dir="/flag/dbt")
    assert str(connectors[0].project_dir) == "/flag/dbt"


def test_build_configured_connectors_flag_alone_forces_dbt():
    connectors = build_configured_connectors(WorkspaceConfig(), dbt_project_dir="/flag/dbt")
    assert len(connectors) == 1
    assert connectors[0].name == "dbt"
    assert str(connectors[0].project_dir) == "/flag/dbt"


def test_build_configured_connectors_skips_unbuildable():
    ws = WorkspaceConfig(
        dbt=ConnectorConfig(project_dir="/dbt"),
        prisma=ConnectorConfig(repo="acme/app"),  # no project_dir
    )
    connectors = build_configured_connectors(ws)
    assert [c.name for c in connectors] == ["dbt"]


def test_build_configured_connectors_empty_workspace():
    assert build_configured_connectors(WorkspaceConfig()) == []
