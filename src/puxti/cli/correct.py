"""`puxti correct` — fix an inaccurate definition without code propagation."""

from typing import Optional

import typer
from rich.panel import Panel

from puxti.cli._app import app
from puxti.cli._shared import _load_workspace, _run, console, err_console
from puxti.cli.redefine import _run_redefine
from puxti.core.corrector import SemanticCorrector
from puxti.core.graph import KnowledgeGraph
from puxti.models import CorrectionEvent, Definition


@app.command()
def correct(
    entity: str = typer.Option(
        ..., "--entity", "-e", help="Entity ID to correct (e.g. model.jaffle_shop.orders)"
    ),
    project: Optional[str] = typer.Option(
        None,
        "--project",
        "-p",
        help="Scope to a project — validates the entity belongs to it before proceeding.",
    ),
) -> None:
    """Correct an inaccurate entity definition in the Knowledge Graph.

    Use this when the LLM inferred a wrong definition during scan, or when
    you want to refine a definition without triggering code propagation.

    This is distinct from `redefine`: correct fixes what puxti got wrong
    about something that was always true. redefine propagates a real change
    in business meaning to your code stack as a PR.

    Puxti will show you the current definition, let you correct it, re-evaluate
    all affected semantic edges via LLM, and ask you to confirm each disposition
    before writing anything to the Knowledge Graph.
    """
    _run(_run_correct(entity=entity, project=project), command="correct")


async def _run_correct(entity: str, project: str | None = None) -> None:
    graph = KnowledgeGraph()
    corrector = SemanticCorrector()
    try:
        await graph.connect()

        # 1. Fetch current definition
        current = await graph.get_latest_definition(entity)
        if not current:
            err_console.print(
                f"[red]Error:[/red] No definition found for '{entity}'. "
                "Run `puxti scan` first to bootstrap the Knowledge Graph."
            )
            raise typer.Exit(1)

        # 1b. Validate project scope if specified
        if project:
            entity_obj = await graph.get_entity_by_id(entity)
            if not entity_obj:
                err_console.print(
                    f"[red]Error:[/red] Entity '{entity}' not found in the Knowledge Graph."
                )
                raise typer.Exit(1)
            if entity_obj.project != project:
                err_console.print(
                    f"[red]Error:[/red] Entity '{entity}' belongs to project "
                    f"'{entity_obj.project or '(untagged)'}', not '{project}'."
                )
                raise typer.Exit(1)

        # 2. Show current state
        console.print(f"\n[bold]Entity:[/bold] {entity}")
        console.print(f"[bold]Current definition (v{current.version}):[/bold]")
        console.print(f"  {current.description}\n")

        edges = await graph.get_entity_semantic_edges(entity)
        if edges:
            console.print(f"[bold]Semantic edges involving this entity ({len(edges)}):[/bold]")
            for e in edges:
                direction = "→" if e.from_entity_id == entity else "←"
                other = e.to_entity_id if e.from_entity_id == entity else e.from_entity_id
                console.print(f"  {direction} ({e.type.value}) {other}")
                console.print(f"    {e.description}")
            console.print()
        else:
            console.print("[dim]No semantic edges found for this entity.[/dim]\n")

        # 3. Collect corrected definition
        corrected = console.input(
            "[bold]Enter corrected definition[/bold] (blank to cancel) > "
        ).strip()
        if not corrected:
            console.print("[yellow]Cancelled.[/yellow]")
            return

        if corrected == current.description:
            console.print("[yellow]Definition unchanged — nothing to correct.[/yellow]")
            return

        console.print(f"\n[bold]Old:[/bold] {current.description}")
        console.print(f"[bold]New:[/bold] {corrected}\n")

        # 4. LLM re-evaluates edges
        confirmed_assessments = []
        if edges:
            with console.status("[bold]Re-evaluating semantic edges...[/bold]"):
                assessments = await corrector.reassess_edges(
                    entity_id=entity,
                    old_definition=current.description,
                    new_definition=corrected,
                    edges=edges,
                )

            console.print("[bold]Edge re-assessment (confirm each):[/bold]\n")
            for assessment in assessments:
                key = (assessment.from_entity_id, assessment.to_entity_id)
                direction = "→" if assessment.from_entity_id == entity else "←"
                other = (
                    assessment.to_entity_id
                    if assessment.from_entity_id == entity
                    else assessment.from_entity_id
                )
                edge_obj = next(e for e in edges if (e.from_entity_id, e.to_entity_id) == key)
                console.print(f"  {direction} ({edge_obj.type.value}) {other}")
                console.print(
                    f"  LLM suggests: [bold]{assessment.action.upper()}[/bold] — "
                    f"{assessment.reasoning}"
                )
                if assessment.action == "update":
                    console.print(f"  New description: {assessment.updated_description}")

                choice = (
                    console.input("  Accept? ([bold]y[/bold]=yes, k=keep, r=remove) > ")
                    .strip()
                    .lower()
                )

                # Only an explicit "y" applies the LLM suggestion — blank or
                # unrecognized input keeps the edge unchanged, never accepts.
                if choice == "y":
                    confirmed_assessments.append(assessment)
                elif choice == "r":
                    from puxti.models import EdgeAssessment

                    confirmed_assessments.append(
                        EdgeAssessment(
                            from_entity_id=assessment.from_entity_id,
                            to_entity_id=assessment.to_entity_id,
                            action="remove",
                            reasoning="User overrode to remove",
                        )
                    )
                else:
                    from puxti.models import EdgeAssessment

                    reasoning = (
                        "User overrode to keep"
                        if choice == "k"
                        else "No explicit choice — edge kept unchanged"
                    )
                    confirmed_assessments.append(
                        EdgeAssessment(
                            from_entity_id=assessment.from_entity_id,
                            to_entity_id=assessment.to_entity_id,
                            action="keep",
                            reasoning=reasoning,
                        )
                    )
                console.print()
        else:
            confirmed_assessments = []

        # 5. Classify: correction vs real change
        console.print("[bold]Is this a correction or a real change?[/bold]")
        console.print("  c = correction (the KG was wrong — fix KG only, no PR)")
        console.print("  r = real change (business meaning changed — hand off to redefine)")
        classification_input = console.input("  > ").strip().lower()

        if classification_input not in ("c", "r"):
            console.print("[yellow]Cancelled.[/yellow]")
            return

        classified_as = "correction" if classification_input == "c" else "real_change"

        # 7. Real change — hand off immediately, write nothing to the KG.
        # The user has told us the KG state is wrong about a real business change,
        # not a past inaccuracy. Writing a correction event here would pollute the
        # audit trail with a "correct" record for something that belongs in redefine.
        if classified_as == "real_change":
            console.print(
                "\n[bold]This is a real change — nothing written to the Knowledge Graph.[/bold]\n"
                "Run the following command to propagate it to your code stack:\n"
            )
            console.print(
                f"  puxti redefine --entity {entity!r} "
                f"--description {corrected!r} --repo <your-repo>"
            )
            run_now = console.input("Run it now? ([bold]y[/bold]/N) > ").strip().lower()
            if run_now == "y":
                ws = _load_workspace()
                resolved_repo = ws.dbt.repo if ws.dbt else None
                resolved_project_dir = ws.dbt.project_dir if ws.dbt else None
                resolved_base_branch = ws.dbt.base_branch if ws.dbt else "main"
                if not resolved_repo:
                    err_console.print(
                        "[red]Error:[/red] connectors.dbt.repo is required to run redefine now.\n"
                        "  Add it to .puxti.yml or run the printed command with --repo."
                    )
                    raise typer.Exit(1)
                await _run_redefine(
                    entity=entity,
                    description=corrected,
                    repo=resolved_repo,
                    base_branch=resolved_base_branch,
                    dbt_project_dir=resolved_project_dir,
                    dry_run=False,
                )
            return

        # 6. Final summary + confirmation before any write
        updated_edges, removed_pairs, updated_pairs = corrector.apply_assessments(
            edges, confirmed_assessments
        )

        kept_count = len(updated_edges) - len(updated_pairs)
        console.print(
            Panel(
                f"Definition:    v{current.version} → v{current.version + 1}\n"
                f"  Old: {current.description}\n"
                f"  New: {corrected}\n"
                f"\n"
                f"Edges kept:    {kept_count}\n"
                f"Edges updated: {len(updated_pairs)}\n"
                f"Edges removed: {len(removed_pairs)}\n"
                f"\n"
                f"Classified as: correction",
                title="[bold]Confirm changes to Knowledge Graph[/bold]",
                border_style="yellow",
            )
        )
        confirm = (
            console.input("Write these changes? ([bold]y[/bold]=yes, n=cancel) > ").strip().lower()
        )
        if confirm != "y":
            console.print("[yellow]Cancelled — nothing written.[/yellow]")
            return

        new_def = Definition(
            entity_id=entity,
            description=corrected,
            version=current.version + 1,
            created_by="correct",
        )
        await graph.upsert_definition(new_def)

        correction_event = CorrectionEvent(
            entity_id=entity,
            old_definition_id=current.id,
            new_definition_id=new_def.id,
            edges_kept=[
                (e.from_entity_id, e.to_entity_id)
                for e in updated_edges
                if (e.from_entity_id, e.to_entity_id) not in updated_pairs
            ],
            edges_updated=updated_pairs,
            edges_removed=removed_pairs,
            classified_as=classified_as,
        )
        await graph.write_correction(correction_event, updated_edges)

        console.print(
            f"[green]✓[/green] Correction written: "
            f"definition v{new_def.version}, "
            f"{len(removed_pairs)} edge(s) removed, "
            f"{len(updated_pairs)} edge(s) updated."
        )

    finally:
        await graph.close()
