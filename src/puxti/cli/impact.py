"""`puxti impact` — show blast radius of a change, straight from the graph."""

from typing import Optional

import typer
from rich.panel import Panel
from rich.table import Table

from puxti.cli._app import app
from puxti.cli._shared import _run, console, err_console
from puxti.core.graph import KnowledgeGraph


@app.command()
def impact(
    entity: str = typer.Argument(
        ..., help="Entity ID to analyze (e.g. model.jaffle_shop.orders)"
    ),
    change_type: Optional[str] = typer.Option(
        None, "--change-type",
        help="Type of change to evaluate: rename, redefine, drop, type_change",
    ),
    as_json: bool = typer.Option(
        False, "--json",
        help="Output as JSON (same shape as the MCP impact_of_change tool).",
    ),
) -> None:
    """Show what depends on an entity and what would be affected by a change.

    Queries the Knowledge Graph for semantic and structural dependents without
    making any LLM calls. Run `puxti scan` first to populate the graph.

    Use --change-type to see which dependents are primary risk for a specific
    type of change: rename (structural), redefine (semantic), drop or
    type_change (both).
    """
    _VALID_CHANGE_TYPES = {"rename", "redefine", "drop", "type_change"}
    if change_type and change_type not in _VALID_CHANGE_TYPES:
        err_console.print(
            f"[red]Error:[/red] Invalid --change-type '{change_type}'. "
            f"Valid values: {', '.join(sorted(_VALID_CHANGE_TYPES))}"
        )
        raise typer.Exit(1)
    _run(_run_impact(entity=entity, change_type=change_type, as_json=as_json), command="impact")


async def _run_impact(entity: str, change_type: str | None, as_json: bool) -> None:
    import json as _json

    graph = KnowledgeGraph()
    try:
        await graph.connect()

        entity_obj = await graph.get_entity_by_id(entity)
        if not entity_obj:
            err_console.print(
                f"[red]Error:[/red] Entity '{entity}' not found in the Knowledge Graph.\n"
                "  Run [bold]puxti describe[/bold] to see available entity IDs, "
                "or [bold]puxti scan[/bold] to populate the graph."
            )
            raise typer.Exit(1)

        definition = await graph.get_latest_definition(entity)
        semantic_deps = await graph.get_semantic_dependents_with_depth(entity)
        structural_deps = await graph.get_structural_dependents(entity)

        # Merge into a single map keyed by entity ID.
        # An entity can appear in both semantic and structural — track both.
        dep_map: dict[str, dict] = {}
        for dep, hop in semantic_deps:
            entry = dep_map.setdefault(dep.id, {"entity": dep, "hop": hop, "rels": set()})
            entry["rels"].add("semantic")
            entry["hop"] = min(entry["hop"], hop)
        for dep in structural_deps:
            entry = dep_map.setdefault(dep.id, {"entity": dep, "hop": 1, "rels": set()})
            entry["rels"].add("structural")

        rows = sorted(dep_map.values(), key=lambda r: (r["hop"], r["entity"].name))

        if as_json:
            payload = {
                "entity_id": entity,
                "change_type": change_type,
                "dependents": [
                    {
                        "entity_id": r["entity"].id,
                        "name": r["entity"].name,
                        "type": r["entity"].type.value,
                        "hop": r["hop"],
                        "relationship": "+".join(sorted(r["rels"])),
                    }
                    for r in rows
                ],
                "total_count": len(rows),
            }
            console.print_json(_json.dumps(payload))
            return

        def_text = (
            definition.description
            if definition
            else "[dim]no definition — run puxti scan[/dim]"
        )
        header_lines = (
            f"[bold]Entity:[/bold]      {entity_obj.name}  [dim]({entity_obj.type.value})[/dim]\n"
            f"[bold]Definition:[/bold]  {def_text}"
        )
        if change_type:
            header_lines += f"\n[bold]Change type:[/bold] {change_type}"

        console.print(Panel(header_lines, title=f"[bold]Impact: {entity}[/bold]", border_style="blue"))

        if not rows:
            console.print("[yellow]No dependents found.[/yellow] Nothing in the graph depends on this entity.")
            return

        table = Table(show_lines=False)
        table.add_column("Entity", style="bold", no_wrap=True)
        table.add_column("Type", style="dim", no_wrap=True)
        table.add_column("Hop", justify="right", style="dim", no_wrap=True)
        table.add_column("Relationship", no_wrap=True)

        # Determine which relationships are primary risk for the given change type.
        primary: set[str] = set()
        if change_type == "rename":
            primary = {"structural"}
        elif change_type == "redefine":
            primary = {"semantic"}
        elif change_type in ("drop", "type_change"):
            primary = {"semantic", "structural"}

        for r in rows:
            rel_label = "+".join(sorted(r["rels"]))
            is_primary = bool(r["rels"] & primary) if primary else False
            if is_primary:
                rel_styled = f"[yellow bold]{rel_label}[/yellow bold]"
            elif "semantic" in r["rels"] and "structural" not in r["rels"]:
                rel_styled = f"[cyan]{rel_label}[/cyan]"
            elif "structural" in r["rels"] and "semantic" not in r["rels"]:
                rel_styled = f"[green]{rel_label}[/green]"
            else:
                rel_styled = f"[magenta]{rel_label}[/magenta]"
            table.add_row(r["entity"].name, r["entity"].type.value, str(r["hop"]), rel_styled)

        console.print(table)

        sem_count = sum(1 for r in rows if "semantic" in r["rels"])
        str_count = sum(1 for r in rows if "structural" in r["rels"])
        console.print(
            f"\n[dim]{len(rows)} dependent(s) — "
            f"{sem_count} semantic, {str_count} structural[/dim]"
        )

        if change_type in ("rename", "drop", "type_change") and str_count:
            console.print(
                f"[yellow]⚠  {str_count} structural dependent(s) will need updating for a {change_type}.[/yellow]"
            )
        if change_type in ("redefine", "drop", "type_change") and sem_count:
            console.print(
                f"[yellow]⚠  {sem_count} semantic dependent(s) may need review for a {change_type}.[/yellow]"
            )

    finally:
        await graph.close()
