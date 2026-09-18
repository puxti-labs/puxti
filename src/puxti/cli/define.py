"""`puxti define`: author a metric's meaning before the code that implements it."""

import typer

from puxti.cli._app import app
from puxti.cli._shared import _run, console, err_console
from puxti.core.graph import KnowledgeGraph
from puxti.models import Definition, EdgeType, Entity, EntityStatus, EntityType, SemanticEdge


def _metric_id(project: str, name: str) -> str:
    """Canonical, stable ID for a proposed metric, discoverable by `bind`,
    `describe`, and the MCP tools (a random UUID would not be)."""
    slug = name.strip().lower().replace(" ", "_")
    return f"metric.{project or 'proposed'}.{slug}"


@app.command()
def define(
    name: str = typer.Option(
        ...,
        "--name",
        "-n",
        help="Metric name, e.g. 'net_revenue_retention'",
    ),
    description: str = typer.Option(
        ...,
        "--description",
        "-d",
        help="What the metric means",
    ),
    project: str = typer.Option(
        "",
        "--project",
        "-p",
        help="Project to namespace the metric under (optional)",
    ),
    derived_from: str = typer.Option(
        None,
        "--derived-from",
        help="Existing entity ID this metric is derived from; "
        "anchors it into impact analysis (optional)",
    ),
) -> None:
    """Define a metric's meaning before a model computes it.

    Writes a proposed metric entity and its definition into the Knowledge Graph.
    No LLM call, no dbt manifest, no PR. The definition is intent, not code. A
    proposed metric is never reported as a fact by the MCP tools until it is
    bound to a real entity (via `puxti scan` reconcile, or `puxti bind`).

    \b
    puxti define \\
      --name net_revenue_retention \\
      --description "Revenue retained from existing customers, net of churn and contraction." \\
      --derived-from model.jaffle_shop.subscriptions
    """
    _run(
        _run_define(name=name, description=description, project=project, derived_from=derived_from),
        command="define",
    )


async def _run_define(name: str, description: str, project: str, derived_from: str | None) -> None:
    entity_id = _metric_id(project, name)

    kg = KnowledgeGraph()
    await kg.connect()
    try:
        if derived_from and not await kg.get_entity_by_id(derived_from):
            err_console.print(
                f"[red]Error:[/red] --derived-from entity '{derived_from}' not found. "
                "Run `puxti describe` to list entity IDs."
            )
            raise typer.Exit(1)

        await kg.upsert_entity(
            Entity(
                id=entity_id,
                name=name,
                type=EntityType.METRIC,
                source_connector="proposed",
                project=project,
                status=EntityStatus.PROPOSED,
            )
        )

        existing = await kg.get_latest_definition(entity_id)
        await kg.upsert_definition(
            Definition(
                entity_id=entity_id,
                description=description,
                version=(existing.version + 1) if existing else 1,
                created_by="user",
            )
        )

        if derived_from:
            await kg.upsert_semantic_edge(
                SemanticEdge(
                    from_entity_id=entity_id,
                    to_entity_id=derived_from,
                    type=EdgeType.DERIVED_FROM,
                    description=f"{name} is derived from {derived_from}",
                    created_by="user",
                )
            )
    finally:
        await kg.close()

    console.print(f"[green]✓[/green]  Proposed metric [bold]{name}[/bold]  ({entity_id})")
    if derived_from:
        console.print(f"        ──DERIVED_FROM──▶  {derived_from}")
    console.print(
        "[dim]Unbound until a real model implements it. "
        "Run `puxti scan` to reconcile, or `puxti bind` to bind it explicitly.[/dim]"
    )
