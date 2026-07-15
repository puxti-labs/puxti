"""`puxti health` — check connectivity to all configured services."""

from typing import Optional

import typer

from puxti.cli._app import app
from puxti.cli._shared import _load_workspace, _run, console
from puxti.connectors.airflow import AirflowConnector
from puxti.connectors.dbt import DbtConnector
from puxti.connectors.github import GitHubConnector
from puxti.connectors.registry import build_connector
from puxti.llm import LLMAuthError, LLMBillingError, LLMConfigError, get_backend
from puxti.settings import settings
from puxti.workspace import WorkspaceConfig


@app.command()
def health(
    dbt_project_dir: Optional[str] = typer.Option(
        None, "--dbt-project-dir", help="Path to dbt project root"
    ),
) -> None:
    """Check connectivity to all configured services."""
    ws = _load_workspace()
    resolved_project_dir = dbt_project_dir or (ws.dbt.project_dir if ws.dbt else None)
    _run(_run_health(dbt_project_dir=resolved_project_dir, workspace=ws), command="health")


async def _run_health(dbt_project_dir: str | None, workspace: WorkspaceConfig | None = None) -> None:
    all_ok = True

    # Knowledge Graph (SQLite)
    from puxti.core.graph import DEFAULT_DB_PATH
    if DEFAULT_DB_PATH.exists():
        console.print(f"[green]✓[/green] Knowledge Graph  ({DEFAULT_DB_PATH})")
    else:
        console.print(f"[yellow]–[/yellow] Knowledge Graph  (not initialised — run [bold]puxti scan[/bold])")

    # LLM API — the backend's auth check consumes no credits
    backend = None
    try:
        backend = get_backend()
    except LLMConfigError as exc:
        console.print(f"[red]✗[/red] LLM provider: {exc}")
        all_ok = False

    if backend is not None:
        if backend.provider == "anthropic":
            label = "Anthropic API key"
            key_hint = "ANTHROPIC_API_KEY"
        else:
            label = f"LLM API key ({backend.provider})"
            key_hint = "LLM_API_KEY"
        if not backend.key_configured:
            console.print(f"[yellow]–[/yellow] {label} ({key_hint} not configured)")
            all_ok = False
        else:
            try:
                await backend.auth_check()
                console.print(f"[green]✓[/green] {label}")
            except LLMAuthError:
                console.print(f"[red]✗[/red] {label}: invalid or expired")
                all_ok = False
            except LLMBillingError:
                console.print(f"[red]✗[/red] {label}: valid but credit balance is too low")
                all_ok = False
            except Exception as exc:
                console.print(f"[red]✗[/red] LLM API ({backend.provider}): {exc}")
                all_ok = False

    # dbt connector
    project_dir = dbt_project_dir or settings.dbt_project_dir
    if project_dir:
        try:
            ok = await DbtConnector(config={"project_dir": project_dir}).health_check()
            if ok:
                console.print("[green]✓[/green] dbt manifest")
            else:
                console.print("[red]✗[/red] dbt manifest not found — run `dbt compile`")
                all_ok = False
        except Exception as exc:
            console.print(f"[red]✗[/red] dbt: {exc}")
            all_ok = False
    else:
        console.print("[yellow]–[/yellow] dbt (DBT_PROJECT_DIR not configured)")

    # Airflow connector
    if workspace and workspace.airflow and workspace.airflow.project_dir:
        from pathlib import Path as _Path
        _dags_subdir = workspace.airflow.extras.get("dags_dir", "dags")
        _dags_dir = str(_Path(workspace.airflow.project_dir) / _dags_subdir)
        try:
            ok = await AirflowConnector(config={"dags_dir": _dags_dir}).health_check()
            if ok:
                console.print(f"[green]✓[/green] Airflow dags dir — {_dags_dir}")
            else:
                console.print(f"[red]✗[/red] Airflow dags dir not found — {_dags_dir}")
                all_ok = False
        except Exception as exc:
            console.print(f"[red]✗[/red] Airflow: {exc}")
            all_ok = False
    elif workspace and workspace.airflow:
        console.print("[yellow]–[/yellow] Airflow (project_dir not set in .puxti.yml)")
    else:
        console.print("[yellow]–[/yellow] Airflow (not configured in .puxti.yml)")

    # Prisma and SQL views connectors — configured via .puxti.yml only
    for conn_name, label, missing_hint in (
        ("prisma", "Prisma schema", "schema.prisma not found"),
        ("sql_views", "SQL views dir", "views directory not found"),
    ):
        cfg = workspace.get(conn_name) if workspace else None
        if cfg and cfg.project_dir:
            try:
                connector = build_connector(conn_name, cfg)
                ok = connector is not None and await connector.health_check()
                if ok:
                    console.print(f"[green]✓[/green] {label}")
                else:
                    console.print(f"[red]✗[/red] {label}: {missing_hint}")
                    all_ok = False
            except Exception as exc:
                console.print(f"[red]✗[/red] {label}: {exc}")
                all_ok = False
        elif cfg:
            console.print(f"[yellow]–[/yellow] {label} (project_dir not set in .puxti.yml)")

    # GitHub write access — one check per connector with a repo configured
    if workspace:
        for repo, connector_type in workspace.connector_repos():
            if not settings.github_token:
                console.print(f"[yellow]–[/yellow] GitHub write access — {repo} ({connector_type}): GITHUB_TOKEN not configured")
                all_ok = False
                continue
            try:
                gh = GitHubConnector(config={"repo": repo, "token": settings.github_token})
                ok = await gh.health_check()
                if ok:
                    console.print(f"[green]✓[/green] GitHub write access — {repo} ({connector_type})")
                else:
                    console.print(f"[red]✗[/red] GitHub write access — {repo} ({connector_type}): no write permission")
                    all_ok = False
            except Exception as exc:
                console.print(f"[red]✗[/red] GitHub write access — {repo} ({connector_type}): {exc}")
                all_ok = False

    if not all_ok:
        raise typer.Exit(1)
