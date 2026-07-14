"""`puxti scan` — bootstrap the Knowledge Graph from a dbt project."""

from typing import Optional

import typer
from rich.panel import Panel

from puxti.cli._app import app
from puxti.cli._shared import _load_workspace, _run, console, err_console
from puxti.connectors.dbt import DbtConnector
from puxti.core.graph import KnowledgeGraph
from puxti.core.scanner import SemanticScanner
from puxti.settings import settings


@app.command()
def scan(
    dbt_project_dir: Optional[str] = typer.Option(
        None, "--dbt-project-dir", help="Path to dbt project root (overrides .puxti.yml and DBT_PROJECT_DIR)"
    ),
    interactive: bool = typer.Option(
        False, "--interactive", "-i",
        help="Confirm each definition one by one (slower, higher accuracy).",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Estimate token count and cost without running the scan."
    ),
) -> None:
    """Populate the Knowledge Graph so puxti can propagate changes safely.

    Reads the dbt manifest, extracts all entities and lineage, infers starter
    definitions via LLM, and proposes semantic edges.

    Two modes:
    - Default (auto): generates all definitions in one pass, shows a summary,
      you confirm everything at once before anything is written.
    - Interactive (--interactive): walks through each model one by one,
      you confirm or edit each definition before moving to the next.

    Nothing is written to the Knowledge Graph without your explicit confirmation.
    Run this before using `puxti redefine`.
    """
    ws = _load_workspace()
    resolved_project_dir = dbt_project_dir or (ws.dbt.project_dir if ws.dbt else None)
    _run(_run_scan(dbt_project_dir=resolved_project_dir, interactive=interactive, dry_run=dry_run), command="scan")


async def _run_scan(dbt_project_dir: str | None, interactive: bool, dry_run: bool = False) -> None:
    project_dir = dbt_project_dir or settings.dbt_project_dir
    if not project_dir:
        err_console.print(
            "[red]Error:[/red] dbt project directory not configured. "
            "Pass --dbt-project-dir or set DBT_PROJECT_DIR."
        )
        raise typer.Exit(1)

    dbt = DbtConnector(config={"project_dir": project_dir})
    scanner = SemanticScanner()

    if dry_run:
        with console.status("[bold]Reading manifest and counting tokens...[/bold]"):
            estimate = await scanner.estimate_scan_cost(dbt)
        console.print(
            Panel(
                f"Models to define:       {estimate['models']}\n"
                f"Total entities:         {estimate['entities_total']}\n"
                f"\n"
                f"Definition calls\n"
                f"  Input tokens:         {estimate['def_input_tokens']:,}\n"
                f"  Est. output tokens:   {estimate['def_est_output_tokens']:,}\n"
                f"\n"
                f"Edge proposal call\n"
                f"  Input tokens:         {estimate['edges_input_tokens']:,}\n"
                f"  Est. output tokens:   {estimate['edges_est_output_tokens']:,}\n"
                f"\n"
                f"Total input tokens:     {estimate['total_input_tokens']:,}\n"
                f"Total est. output:      {estimate['total_est_output_tokens']:,}\n"
                f"Est. cost:              ${estimate['estimated_cost_usd']:.4f} USD",
                title="[bold]Dry run — cost estimate[/bold]",
                border_style="yellow",
            )
        )
        return

    graph = KnowledgeGraph()
    try:
        await graph.connect()
        mode = "interactive" if interactive else "auto"
        console.print(f"[bold]Scanning dbt project[/bold] ({mode} mode)")
        result = await scanner.scan(dbt, graph, interactive=interactive, console=console)
        console.print(
            f"\n[green]✓[/green] Scan complete: "
            f"{result.entities_upserted} entities, "
            f"{result.definitions_written} definitions, "
            f"{result.semantic_edges_written} semantic edges written."
        )
    finally:
        await graph.close()
