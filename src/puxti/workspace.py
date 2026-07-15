from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# Connector names recognised under `connectors:` in .puxti.yml, in the order
# they are health-checked, scanned, and propagated. dbt stays first — it is
# the default producer and other CLI defaults (e.g. capture's PR repo) key
# off it.
KNOWN_CONNECTORS = ("dbt", "airflow", "prisma", "sql_views")


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
    prisma: ConnectorConfig | None = None
    sql_views: ConnectorConfig | None = None
    path: Path | None = None

    def get(self, name: str) -> ConnectorConfig | None:
        """Config for a connector by its .puxti.yml name, or None."""
        if name not in KNOWN_CONNECTORS:
            return None
        return getattr(self, name)

    def configured(self) -> list[tuple[str, ConnectorConfig]]:
        """All configured connectors as (name, config), in KNOWN_CONNECTORS order."""
        result = []
        for name in KNOWN_CONNECTORS:
            cfg = self.get(name)
            if cfg is not None:
                result.append((name, cfg))
        return result

    def connector_repos(self) -> list[tuple[str, str]]:
        """Return [(repo, connector_type)] for all connectors with a repo configured."""
        return [(cfg.repo, name) for name, cfg in self.configured() if cfg.repo]


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
    parsed = {
        name: _parse_connector(connectors.get(name))
        for name in KNOWN_CONNECTORS
        if name in connectors
    }

    return WorkspaceConfig(path=config_path, **parsed)


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
