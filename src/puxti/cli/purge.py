"""`puxti purge` — delete entities from the Knowledge Graph."""

from typing import Optional

import typer
from rich.panel import Panel

from puxti.cli._app import app
from puxti.cli._shared import _run, console, err_console
from puxti.core.graph import KnowledgeGraph


@app.command()
def purge(
    project: Optional[str] = typer.Option(
        None, "--project", "-p",
        help="Project name to purge (removes all its entities, definitions, and edges).",
    ),
    all_projects: bool = typer.Option(
        False, "--all", help="Purge the entire Knowledge Graph — all projects and metadata."
    ),
) -> None:
    """Delete entities from the Knowledge Graph.

    Use --project <name> to remove a single project's data.
    Use --all to wipe the entire graph.

    Always asks for confirmation before deleting anything.
    """
    if not project and not all_projects:
        err_console.print(
            "[red]Error:[/red] Specify --project <name> or --all.\n"
            "Run `puxti describe` to see available projects."
        )
        raise typer.Exit(1)
    if project and all_projects:
        err_console.print("[red]Error:[/red] --project and --all are mutually exclusive.")
        raise typer.Exit(1)
    _run(_run_purge(project=project, all_projects=all_projects), command="purge")


async def _run_purge(project: str | None, all_projects: bool) -> None:
    graph = KnowledgeGraph()
    try:
        await graph.connect()

        if all_projects:
            projects = await graph.get_projects()
            project_list = ", ".join(projects) if projects else "(none tagged)"
            console.print(Panel(
                f"This will delete [bold]everything[/bold] in the Knowledge Graph:\n"
                f"all entities, definitions, semantic edges, and audit records.\n\n"
                f"Projects currently in graph: {project_list}",
                title="[bold red]Purge entire Knowledge Graph[/bold red]",
                border_style="red",
            ))
            confirm = console.input("Type [bold]yes[/bold] to confirm > ").strip().lower()
            if confirm != "yes":
                console.print("[yellow]Cancelled — nothing deleted.[/yellow]")
                return
            deleted = await graph.purge_all()
            console.print(f"[green]✓[/green] Purged entire graph ({deleted} nodes deleted).")

        else:
            projects = await graph.get_projects()
            if project not in projects:
                err_console.print(
                    f"[red]Error:[/red] Project '{project}' not found in the Knowledge Graph.\n"
                    f"Available projects: {', '.join(projects) if projects else '(none)'}"
                )
                raise typer.Exit(1)

            console.print(Panel(
                f"This will delete all entities, definitions, and semantic edges\n"
                f"for project [bold]{project}[/bold].",
                title="[bold red]Purge project[/bold red]",
                border_style="red",
            ))
            confirm = console.input("Type [bold]yes[/bold] to confirm > ").strip().lower()
            if confirm != "yes":
                console.print("[yellow]Cancelled — nothing deleted.[/yellow]")
                return
            deleted = await graph.purge_project(project)
            console.print(f"[green]✓[/green] Purged project '{project}' ({deleted} entities deleted).")

    finally:
        await graph.close()
