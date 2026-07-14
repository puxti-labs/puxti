"""`puxti link` — declare a cross-system FEEDS edge between entities."""

import typer

from puxti.cli._app import app
from puxti.cli._shared import _parse_entity_id, _run, console, err_console
from puxti.core.graph import KnowledgeGraph
from puxti.models import EdgeType, Entity, SemanticEdge


@app.command()
def link(
    from_entity: str = typer.Option(
        ..., "--from",
        help="Entity producing data (e.g. task.airflow.salesforce_sync.extract_opportunities)",
    ),
    to_entity: str = typer.Option(
        ..., "--to",
        help="Entity receiving data (e.g. source.clariva.raw_opportunities)",
    ),
    description: str = typer.Option(
        ..., "--description", "-d",
        help="Semantic description of this cross-system relationship — what data flows and what it means",
    ),
) -> None:
    """Declare a cross-system semantic link between a data producer and a dbt entity.

    Creates a FEEDS edge in the Knowledge Graph from an upstream producer
    (e.g. an Airflow task) to a downstream entity (e.g. a dbt source or model).
    This edge is what lets puxti trace semantic changes across system boundaries.

    \b
    puxti link \\
      --from task.airflow.salesforce_sync.extract_opportunities \\
      --to source.clariva.raw_opportunities \\
      --description "Extracts Salesforce opportunities. amount is a roll-up of order line prices."
    """
    try:
        _parse_entity_id(from_entity)
        _parse_entity_id(to_entity)
    except ValueError as exc:
        err_console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1)
    _run(_run_link(from_entity=from_entity, to_entity=to_entity, description=description), command="link")


async def _run_link(from_entity: str, to_entity: str, description: str) -> None:
    from_type, from_connector, from_project = _parse_entity_id(from_entity)
    to_type, to_connector, to_project = _parse_entity_id(to_entity)

    kg = KnowledgeGraph()
    await kg.connect()
    try:
        # Entities are keyed by their canonical string ID — the same ID capture
        # later passes to get_feeds_producers(). Creating them under a random
        # UUID would make the FEEDS edge undiscoverable (and duplicate entities
        # that scan already registered under the canonical ID).
        for entity_id, entity_type, connector, project in (
            (from_entity, from_type, from_connector, from_project),
            (to_entity, to_type, to_connector, to_project),
        ):
            if not await kg.get_entity_by_id(entity_id):
                await kg.upsert_entity(Entity(
                    id=entity_id,
                    name=entity_id.rsplit(".", 1)[-1],
                    type=entity_type,
                    source_connector=connector,
                    project=project,
                ))
        await kg.upsert_semantic_edge(SemanticEdge(
            from_entity_id=from_entity,
            to_entity_id=to_entity,
            type=EdgeType.FEEDS,
            description=description,
            created_by="user",
        ))
    finally:
        await kg.close()

    console.print(f"[green]✓[/green]  {from_entity}")
    console.print(f"        ──FEEDS──▶  {to_entity}")
