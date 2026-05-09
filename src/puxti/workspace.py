from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class ConnectorConfig:
    project_dir: str | None = None
    repo: str | None = None
    repo_subdir: str | None = None
    base_branch: str = "main"
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkspaceConfig:
    dbt: ConnectorConfig | None = None
    airflow: ConnectorConfig | None = None
    path: Path | None = None

    def connector_repos(self) -> list[tuple[str, str]]:
        """Return [(repo, connector_type)] for all connectors with a repo configured."""
        result = []
        if self.dbt and self.dbt.repo:
            result.append((self.dbt.repo, "dbt"))
        if self.airflow and self.airflow.repo:
            result.append((self.airflow.repo, "airflow"))
        return result


def load_workspace(start_dir: Path | None = None) -> WorkspaceConfig:
    """Walk up from start_dir (default: CWD) looking for .puxti.yml.

    Returns an empty WorkspaceConfig if no file is found.
    Raises ValueError on parse errors or unsupported version.
    """
    config_path = _find_config_file(start_dir or Path.cwd())
    if config_path is None:
        return WorkspaceConfig()

    try:
        with open(config_path) as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid .puxti.yml at {config_path}: {exc}") from exc

    if not data:
        return WorkspaceConfig(path=config_path)

    version = data.get("version")
    if version is not None and version != 1:
        raise ValueError(f"Unsupported .puxti.yml version. Expected: 1, got: {version}")

    connectors = data.get("connectors") or {}
    dbt = _parse_connector(connectors.get("dbt")) if "dbt" in connectors else None
    airflow = _parse_connector(connectors.get("airflow")) if "airflow" in connectors else None

    return WorkspaceConfig(dbt=dbt, airflow=airflow, path=config_path)


def _find_config_file(start: Path) -> Path | None:
    current = start.resolve()
    while True:
        candidate = current / ".puxti.yml"
        if candidate.exists():
            return candidate
        parent = current.parent
        if parent == current:
            return None
        current = parent


def _parse_connector(data: dict | None) -> ConnectorConfig:
    if not data:
        return ConnectorConfig()
    known = {"project_dir", "repo", "repo_subdir", "base_branch"}
    extras = {k: v for k, v in data.items() if k not in known}
    return ConnectorConfig(
        project_dir=data.get("project_dir"),
        repo=data.get("repo"),
        repo_subdir=data.get("repo_subdir"),
        base_branch=data.get("base_branch", "main"),
        extras=extras,
    )
