"""`puxti scan` — bootstrap the Knowledge Graph from the configured producers."""

from typing import Optional

import typer
from rich.panel import Panel

from puxti.cli._app import app
from puxti.cli._shared import _load_workspace, _run, console, err_console
from puxti.connectors.registry import build_configured_connectors
from puxti.core.graph import KnowledgeGraph
from puxti.core.resolution import build_reference_index
from puxti.core.scanner import SemanticScanner
from puxti.llm import COST_UNKNOWN_HINT
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

    Scans every producer connector configured in .puxti.yml (dbt, prisma,
    sql_views — plus --dbt-project-dir/DBT_PROJECT_DIR for dbt), extracts all
    entities and lineage, infers starter definitions via LLM, and proposes
    semantic edges. Table references that cross connectors (e.g. a SQL view
    reading a Prisma-managed table) are resolved into lineage edges.

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
    ws = _load_workspace()
    connectors = build_configured_connectors(ws, dbt_project_dir=project_dir)

    if not connectors:
        err_console.print(
            "[red]Error:[/red] no producer connectors configured. "
            "Pass --dbt-project-dir, set DBT_PROJECT_DIR, or configure "
            "connectors in .puxti.yml."
        )
        raise typer.Exit(1)

    scanner = SemanticScanner()

    if dry_run:
        total_cost: float | None = 0.0
        for connector in connectors:
            status_text = f"[bold]Reading {connector.name} sources and counting tokens...[/bold]"
            with console.status(status_text):
                estimate = await scanner.estimate_scan_cost(connector)
            approx = "" if estimate["tokens_exact"] else " (approximate)"
            cost_line = (
                f"Est. cost:              ${estimate['estimated_cost_usd']:.4f} USD"
                if estimate["estimated_cost_usd"] is not None
                else f"Est. cost:              {COST_UNKNOWN_HINT}"
            )
            if estimate["estimated_cost_usd"] is None:
                total_cost = None
            elif total_cost is not None:
                total_cost += estimate["estimated_cost_usd"]
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
                    f"Total input tokens:     {estimate['total_input_tokens']:,}{approx}\n"
                    f"Total est. output:      {estimate['total_est_output_tokens']:,}\n"
                    f"{cost_line}",
                    title=f"[bold]Dry run — {connector.name} cost estimate[/bold]",
                    border_style="yellow",
                )
            )
        if len(connectors) > 1 and total_cost is not None:
            console.print(f"[bold]Total est. cost:[/bold] ${total_cost:.4f} USD")
        return

    # Cross-connector reference resolution — built from every producer's
    # entities so `sqlref.` placeholder edges resolve at write time.
    reference_index = None
    if len(connectors) > 1:
        all_entities = []
        for connector in connectors:
            all_entities.extend(await connector.extract_entities())
        reference_index = build_reference_index(all_entities)

    graph = KnowledgeGraph()
    try:
        await graph.connect()
        mode = "interactive" if interactive else "auto"
        for connector in connectors:
            console.print(f"\n[bold]Scanning {connector.name}[/bold] ({mode} mode)")
            result = await scanner.scan(
                connector, graph, interactive=interactive, console=console,
                reference_index=reference_index,
            )
            console.print(
                f"\n[green]✓[/green] {connector.name} scan complete: "
                f"{result.entities_upserted} entities, "
                f"{result.definitions_written} definitions, "
                f"{result.semantic_edges_written} semantic edges written."
            )
    finally:
        await graph.close()
