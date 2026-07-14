"""`puxti describe` — display the current state of the Knowledge Graph."""

from typing import Optional

import typer
from rich.panel import Panel
from rich.table import Table

from puxti.cli._app import app
from puxti.cli._shared import _run, console, err_console
from puxti.core.graph import KnowledgeGraph


@app.command()
def describe(
    entity: Optional[str] = typer.Option(
        None, "--entity", "-e",
        help="Entity ID to inspect in detail. Omit to show the full Knowledge Graph."
    ),
    project: Optional[str] = typer.Option(
        None, "--project", "-p",
        help="Filter overview to a single project. Ignored when --entity is set."
    ),
) -> None:
    """Display the current state of the Knowledge Graph.

    Without --entity: shows all entities with their definitions and all
    semantic edges in a summary view. Use --project to filter by project.

    With --entity: shows full definition and all semantic edges
    (incoming and outgoing) for a single entity.
    """
    _run(_run_describe(entity=entity, project=project), command="describe")


async def _run_describe(entity: str | None, project: str | None = None) -> None:
    graph = KnowledgeGraph()
    try:
        await graph.connect()

        if entity:
            # ── Single entity detail ───────────────────────────────────────
            entity_obj = await graph.get_entity_by_id(entity)
            if not entity_obj:
                err_console.print(f"[red]Error:[/red] Entity '{entity}' not found in the Knowledge Graph.")
                raise typer.Exit(1)

            definition = await graph.get_latest_definition(entity)
            edges = await graph.get_entity_semantic_edges(entity)

            def_text = definition.description if definition else "[dim]No definition — run puxti scan[/dim]"
            def_meta = f"v{definition.version} · created by {definition.created_by}" if definition else ""

            project_line = f"[bold]Project:[/bold]    {entity_obj.project}\n" if entity_obj.project else ""
            console.print(Panel(
                f"[bold]Type:[/bold]       {entity_obj.type.value}\n"
                f"[bold]Connector:[/bold]  {entity_obj.source_connector}\n"
                f"{project_line}"
                f"[bold]Definition:[/bold] {def_text}\n"
                f"[dim]{def_meta}[/dim]",
                title=f"[bold]{entity_obj.name}[/bold]  [dim]{entity}[/dim]",
                border_style="blue",
            ))

            outgoing = [e for e in edges if e.from_entity_id == entity]
            incoming = [e for e in edges if e.to_entity_id == entity]

            if outgoing:
                console.print(f"\n[bold]Outgoing edges ({len(outgoing)}):[/bold]")
                for e in outgoing:
                    console.print(f"  ──({e.type.value})──▶ {e.to_entity_id}")
                    console.print(f"     [dim]{e.description}[/dim]")

            if incoming:
                console.print(f"\n[bold]Incoming edges ({len(incoming)}):[/bold]")
                for e in incoming:
                    console.print(f"  ◀──({e.type.value})── {e.from_entity_id}")
                    console.print(f"     [dim]{e.description}[/dim]")

            if not outgoing and not incoming:
                console.print("\n[dim]No semantic edges.[/dim]")

        else:
            # ── Full KG overview ───────────────────────────────────────────
            pairs = await graph.get_all_entities_with_definitions()
            semantic_edges = await graph.get_all_semantic_edges()

            if not pairs:
                console.print("[yellow]Knowledge Graph is empty. Run `puxti scan` to bootstrap it.[/yellow]")
                return

            # Apply --project filter if specified
            if project:
                available = sorted({e.project or "(untagged)" for e, _ in pairs})
                if project not in available:
                    err_console.print(
                        f"[red]Error:[/red] Project '{project}' not found.\n"
                        f"Available projects: {', '.join(available)}"
                    )
                    raise typer.Exit(1)
                pairs = [(e, d) for e, d in pairs if (e.project or "(untagged)") == project]
                entity_ids = {e.id for e, _ in pairs}
                semantic_edges = [
                    edge for edge in semantic_edges
                    if edge.from_entity_id in entity_ids or edge.to_entity_id in entity_ids
                ]

            # Group by project for display
            from itertools import groupby
            projects_in_graph = sorted({e.project or "(untagged)" for e, _ in pairs})
            defined = sum(1 for _, d in pairs if d)

            for proj in projects_in_graph:
                proj_pairs = [(e, d) for e, d in pairs if (e.project or "(untagged)") == proj]
                table = Table(
                    title=f"[bold]{proj}[/bold]  [dim]{len(proj_pairs)} entities[/dim]",
                    show_lines=False,
                )
                table.add_column("Name", style="bold", no_wrap=True)
                table.add_column("Type", style="dim", no_wrap=True)
                table.add_column("Ver", justify="right", style="dim", no_wrap=True)
                table.add_column("Definition")

                for entity_obj, definition in proj_pairs:
                    ver = str(definition.version) if definition else "–"
                    desc = definition.description if definition else "[dim]no definition[/dim]"
                    if len(desc) > 80:
                        desc = desc[:77] + "..."
                    table.add_row(entity_obj.name, entity_obj.type.value, ver, desc)

                console.print(table)
                console.print()

            console.print(
                f"[dim]{defined}/{len(pairs)} entities have definitions · "
                f"{len(semantic_edges)} semantic edge(s) · "
                f"{len(projects_in_graph)} project(s)[/dim]\n"
            )

            if semantic_edges:
                # Build name lookup for cleaner display
                name_map = {e.id: e.name for e, _ in pairs}
                console.print(f"[bold]Semantic Edges ({len(semantic_edges)}):[/bold]")
                for edge in semantic_edges:
                    from_name = name_map.get(edge.from_entity_id, edge.from_entity_id)
                    to_name = name_map.get(edge.to_entity_id, edge.to_entity_id)
                    console.print(f"  {from_name} ──({edge.type.value})──▶ {to_name}")
                    console.print(f"    [dim]{edge.description}[/dim]")
            else:
                console.print("[dim]No semantic edges. Run `puxti scan` to propose edges.[/dim]")

    finally:
        await graph.close()
