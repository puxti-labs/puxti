"""`puxti graph` — render the Knowledge Graph as a self-contained interactive HTML page."""

import webbrowser
from pathlib import Path
from typing import Optional

import typer

from puxti.cli._app import app
from puxti.cli._shared import _run, console
from puxti.core.graph import KnowledgeGraph
from puxti.graph_template import render_graph_html


@app.command()
def graph(
    output: str = typer.Option(
        "puxti-graph.html", "--output", "-o",
        help="Path to write the HTML file to.",
    ),
    project: Optional[str] = typer.Option(
        None, "--project", "-p",
        help="Only include entities from this project.",
    ),
    open_browser: bool = typer.Option(
        False, "--open",
        help="Open the generated file in your browser.",
    ),
) -> None:
    """Render the Knowledge Graph as a self-contained, interactive HTML page.

    Reads the local graph (~/.puxti/graph.db) and writes a single HTML file with a
    force-directed view of entities and their lineage and semantic relationships.
    Click a node to see its definition, relationships, and definition history. No
    server and no external dependencies; open the file in any browser. Proposed
    metrics (from `puxti define`) are shown with a distinct style.
    """
    _run(
        _run_graph(output=output, project=project, open_browser=open_browser),
        command="graph",
    )


async def _run_graph(output: str, project: str | None, open_browser: bool) -> None:
    kg = KnowledgeGraph()
    await kg.connect()
    try:
        pairs = await kg.get_all_entities_with_definitions()
        if project:
            pairs = [(e, d) for e, d in pairs if e.project == project]
        included = {e.id for e, _ in pairs}

        nodes = []
        for entity, _ in pairs:
            history = await kg.get_definition_history(entity.id)
            current = history[-1] if history else None
            nodes.append({
                "id": entity.id,
                "name": entity.name,
                "type": entity.type.value,
                "project": entity.project,
                "status": entity.status.value,
                "definition": {
                    "description": current.description,
                    "version": current.version,
                    "created_by": current.created_by,
                    "created_at": current.created_at.isoformat(),
                } if current else None,
                "history": [
                    {
                        "version": h.version,
                        "description": h.description,
                        "created_by": h.created_by,
                        "created_at": h.created_at.isoformat(),
                    }
                    for h in history
                ],
            })

        edges = []
        for ledge in await kg.get_all_lineage_edges():
            if ledge.from_entity_id in included and ledge.to_entity_id in included:
                edges.append({
                    "source": ledge.from_entity_id,
                    "target": ledge.to_entity_id,
                    "kind": "lineage",
                    "type": ledge.type.value,
                    "description": "",
                })
        for sedge in await kg.get_all_semantic_edges():
            if sedge.from_entity_id in included and sedge.to_entity_id in included:
                edges.append({
                    "source": sedge.from_entity_id,
                    "target": sedge.to_entity_id,
                    "kind": "semantic",
                    "type": sedge.type.value,
                    "description": sedge.description,
                })
    finally:
        await kg.close()

    payload = {"project": project, "nodes": nodes, "edges": edges}
    out_path = Path(output).expanduser()
    out_path.write_text(render_graph_html(payload), encoding="utf-8")

    console.print(
        f"[green]✓[/green] Wrote {len(nodes)} entities and "
        f"{len(edges)} relationships to [bold]{out_path}[/bold]"
    )
    if not nodes:
        console.print("[dim]Graph is empty. Run `puxti scan` to populate it first.[/dim]")
    if open_browser:
        webbrowser.open(out_path.resolve().as_uri())
