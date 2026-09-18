"""`puxti bind`: bind a proposed metric to the real entity that implements it."""

import typer

from puxti.cli._app import app
from puxti.cli._shared import _run, console, err_console
from puxti.core.graph import KnowledgeGraph
from puxti.models import EntityStatus


@app.command()
def bind(
    proposed: str = typer.Option(
        ...,
        "--proposed",
        help="Proposed metric entity ID (from `puxti define` / `puxti describe`)",
    ),
    to: str = typer.Option(
        ...,
        "--to",
        help="Real entity ID that implements the metric (e.g. model.jaffle_shop.fct_nrr)",
    ),
) -> None:
    """Bind a proposed metric to the real entity that now implements it.

    Use this when `puxti scan` did not auto-suggest the bind. Scan only offers
    binds on exact model/view name matches. Binding carries the metric's
    definition onto the real entity as a new version, re-points its semantic
    edges, and removes the placeholder.

    \b
    puxti bind --proposed metric.proposed.net_revenue_retention --to model.jaffle_shop.fct_nrr
    """
    _run(_run_bind(proposed=proposed, to=to), command="bind")


async def _run_bind(proposed: str, to: str) -> None:
    kg = KnowledgeGraph()
    await kg.connect()
    try:
        source = await kg.get_entity_by_id(proposed)
        if source is None:
            err_console.print(
                f"[red]Error:[/red] proposed entity '{proposed}' not found. "
                "Run `puxti describe` to list entity IDs."
            )
            raise typer.Exit(1)
        if source.status != EntityStatus.PROPOSED:
            err_console.print(
                f"[red]Error:[/red] '{proposed}' is not a proposed metric "
                f"(status: {source.status.value}). Only proposed entities can be bound."
            )
            raise typer.Exit(1)

        target = await kg.get_entity_by_id(to)
        if target is None:
            err_console.print(
                f"[red]Error:[/red] target entity '{to}' not found. "
                "Run `puxti scan` to populate it, or `puxti describe` to list entity IDs."
            )
            raise typer.Exit(1)
        if target.status != EntityStatus.BOUND:
            err_console.print(
                f"[red]Error:[/red] target '{to}' is not a bound entity "
                f"(status: {target.status.value}). Bind to a real scanned entity."
            )
            raise typer.Exit(1)

        await kg.bind_entity(proposed, to)
    finally:
        await kg.close()

    console.print(f"[green]✓[/green]  Bound [bold]{source.name}[/bold] → {to}")
    console.print("[dim]Its definition and semantic edges now live on the real entity.[/dim]")
