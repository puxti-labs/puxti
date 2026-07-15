"""Connector registry — builds producer connectors from workspace config.

The CLI never constructs a producer connector class directly; it asks the
registry to build whatever `.puxti.yml` configures. Adding a producer means
adding its class here and (if it has non-obvious config) a build function —
no CLI changes.
"""

from pathlib import Path

from puxti.connectors.airflow import AirflowConnector
from puxti.connectors.base import BaseConnector
from puxti.connectors.dbt import DbtConnector
from puxti.connectors.prisma import PrismaConnector
from puxti.connectors.sql_views import SqlViewsConnector
from puxti.workspace import ConnectorConfig, WorkspaceConfig


def build_connector(name: str, cfg: ConnectorConfig) -> BaseConnector | None:
    """Build one producer connector from its workspace config.

    Returns None when the config lacks what the connector needs to run
    (currently: a project_dir). Raises ValueError for unknown names.
    """
    if cfg.project_dir is None:
        return None

    if name == "dbt":
        return DbtConnector(config={"project_dir": cfg.project_dir})
    if name == "airflow":
        dags_subdir = cfg.extras.get("dags_dir", "dags")
        return AirflowConnector(
            config={"dags_dir": str(Path(cfg.project_dir) / dags_subdir)}
        )
    if name == "prisma":
        config: dict = {"project_dir": cfg.project_dir}
        if "schema_path" in cfg.extras:
            config["schema_path"] = cfg.extras["schema_path"]
        return PrismaConnector(config=config)
    if name == "sql_views":
        return SqlViewsConnector(
            config={
                "project_dir": cfg.project_dir,
                "views_dir": cfg.extras.get("views_dir", "."),
                "dialect": cfg.extras.get("dialect"),
                "default_schema": cfg.extras.get("default_schema", "public"),
            }
        )
    raise ValueError(f"Unknown connector: {name!r}")


def build_configured_connectors(
    ws: WorkspaceConfig, dbt_project_dir: str | None = None
) -> list[BaseConnector]:
    """Build every connector configured in the workspace, dbt first.

    dbt_project_dir overrides the workspace's dbt project_dir (flag/env
    precedence is the caller's concern) and forces a DbtConnector even when
    .puxti.yml has no dbt section — matching the CLI's historic behaviour
    where --dbt-project-dir alone is enough to run.
    """
    connectors: list[BaseConnector] = []
    seen_dbt = False

    for name, cfg in ws.configured():
        if name == "dbt" and dbt_project_dir:
            cfg = ConnectorConfig(
                project_dir=dbt_project_dir,
                repo=cfg.repo,
                repo_subdir=cfg.repo_subdir,
                base_branch=cfg.base_branch,
                extras=cfg.extras,
            )
        connector = build_connector(name, cfg)
        if connector is not None:
            connectors.append(connector)
            seen_dbt = seen_dbt or name == "dbt"

    if not seen_dbt and dbt_project_dir:
        connectors.insert(0, DbtConnector(config={"project_dir": dbt_project_dir}))

    return connectors
