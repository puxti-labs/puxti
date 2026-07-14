"""`puxti config` — show current configuration and where it is loaded from."""

from rich.table import Table

from puxti.cli._app import app
from puxti.cli._shared import _load_workspace, console
from puxti.settings import settings


@app.command()
def config() -> None:
    """Show the current puxti configuration and where it is loaded from."""
    import os
    from pathlib import Path

    def _mask(value: str, secret: bool = False) -> str:
        if not value:
            return "[dim]not set[/dim]"
        if not secret:
            return value
        if len(value) <= 8:
            return "********"
        return value[:8] + "..." + value[-4:]

    env_file = Path(os.getcwd()) / ".env"

    table = Table(title="puxti config", show_header=False, box=None)
    table.add_column("Key", style="bold", no_wrap=True)
    table.add_column("Value")

    from puxti.core.graph import DEFAULT_DB_PATH
    from puxti.llm import LLM_MODEL
    resolved_model = settings.llm_model or (
        LLM_MODEL if settings.llm_provider == "anthropic" else "[dim]not set[/dim]"
    )
    table.add_row("graph_db",          str(DEFAULT_DB_PATH))
    table.add_row("llm_provider",      settings.llm_provider)
    table.add_row("llm_model",         resolved_model)
    table.add_row("llm_api_key",       _mask(settings.llm_api_key, secret=True))
    table.add_row("llm_base_url",      _mask(settings.llm_base_url))
    table.add_row("anthropic_api_key", _mask(settings.anthropic_api_key, secret=True))
    table.add_row("github_token",      _mask(settings.github_token, secret=True))
    table.add_row("dbt_project_dir",   _mask(settings.dbt_project_dir))
    table.add_row("dbt_profiles_dir",  _mask(settings.dbt_profiles_dir))

    console.print(table)
    console.print()

    env_status = "[green]found[/green]" if env_file.exists() else "[dim]not found[/dim]"
    console.print(f"[bold].env file:[/bold]  {env_file}  {env_status}")

    ws = _load_workspace()
    if ws.path:
        parts = []
        if ws.dbt:
            parts.append(f"dbt ({ws.dbt.repo or '(no repo)'} · {ws.dbt.project_dir or '(no dir)'})")
        if ws.airflow:
            parts.append(f"airflow ({ws.airflow.repo or '(no repo)'} · {ws.airflow.project_dir or '(no dir)'})")
        connectors_summary = ", ".join(parts) if parts else "(no connectors configured)"
        console.print(f"[bold].puxti.yml:[/bold]  {ws.path}  [green]found[/green]")
        console.print(f"  connectors: {connectors_summary}")
    else:
        console.print("[bold].puxti.yml:[/bold]  [dim]not found — using flags and env vars[/dim]")
