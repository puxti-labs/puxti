"""`puxti redefine` — propagate a semantic definition change as a PR."""

from typing import Optional

import typer
from rich.panel import Panel

from puxti.cli._app import app
from puxti.cli._shared import _load_workspace, _run, console, err_console
from puxti.connectors.dbt import DbtConnector
from puxti.connectors.github import GitHubConnector
from puxti.core.graph import KnowledgeGraph
from puxti.core.redefine import SemanticRedefiner
from puxti.llm import get_backend
from puxti.models import ChangeEvent, ChangeStatus, ChangeType
from puxti.settings import settings


@app.command()
def redefine(
    entity: str = typer.Option(
        ..., "--entity", "-e",
        help="Entity ID being redefined (e.g. model.demo_shop.orders.gross_revenue)"
    ),
    description: str = typer.Option(
        ..., "--description", "-d",
        help="New definition — what this entity means now and why it changed",
    ),
    repo: Optional[str] = typer.Option(
        None, help="GitHub repository to open PR against (owner/repo). Falls back to .puxti.yml connectors.dbt.repo. Not required for --dry-run."
    ),
    base_branch: Optional[str] = typer.Option(None, help="Base branch for the PR (default: main or from .puxti.yml)."),
    dbt_project_dir: Optional[str] = typer.Option(
        None, "--dbt-project-dir", help="Path to dbt project root (overrides .puxti.yml and DBT_PROJECT_DIR)"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show affected entities and cost estimate without generating diffs or opening a PR."
    ),
) -> None:
    """Propagate a semantic definition change safely as a reviewable PR.

    Use this when the business meaning of an entity changes — not a rename,
    but a conceptual shift (e.g. 'gross_revenue now excludes refunds').

    Puxti traverses the semantic graph to find downstream entities whose meaning
    is now affected, generates SQL diffs with depth-aware confidence annotations,
    and opens a PR for human review.

    Run `puxti scan` first to bootstrap the semantic graph.
    """
    ws = _load_workspace()
    resolved_repo = repo or (ws.dbt.repo if ws.dbt else None)
    resolved_project_dir = dbt_project_dir or (ws.dbt.project_dir if ws.dbt else None)
    resolved_base_branch = base_branch or (ws.dbt.base_branch if ws.dbt else "main")

    if not dry_run and not resolved_repo:
        err_console.print(
            "[red]Error:[/red] --repo is required unless using --dry-run.\n"
            "  Set it via flag or add [bold]connectors.dbt.repo[/bold] to .puxti.yml."
        )
        raise typer.Exit(1)
    _run(
        _run_redefine(
            entity=entity,
            description=description,
            repo=resolved_repo or "",
            base_branch=resolved_base_branch,
            dbt_project_dir=resolved_project_dir,
            dry_run=dry_run,
        ),
        command="redefine",
    )


async def _run_redefine(
    entity: str,
    description: str,
    repo: str,
    base_branch: str,
    dbt_project_dir: str | None,
    dry_run: bool = False,
) -> None:
    project_dir = dbt_project_dir or settings.dbt_project_dir
    if not project_dir:
        err_console.print(
            "[red]Error:[/red] dbt project directory not configured. "
            "Pass --dbt-project-dir or set DBT_PROJECT_DIR."
        )
        raise typer.Exit(1)

    if not dry_run and not settings.github_token:
        err_console.print(
            "[red]Error:[/red] GitHub token not configured. Set GITHUB_TOKEN."
        )
        raise typer.Exit(1)

    if not dry_run:
        gh_check = GitHubConnector(config={"repo": repo, "token": settings.github_token})
        if not await gh_check.health_check():
            err_console.print(
                f"[red]Error:[/red] GitHub token does not have write access to `{repo}`. "
                "Check token scope and repository permissions."
            )
            raise typer.Exit(1)

    graph = KnowledgeGraph()
    try:
        await graph.connect()

        # 1. Get existing definition and semantic dependents with depth
        existing_definition = await graph.get_latest_definition(entity)
        dependents_with_depth = await graph.get_semantic_dependents_with_depth(entity)

        # Fall back to structural dependents when semantic graph has no inbound edges.
        # These are models that structurally reference this entity but whose semantic
        # relationship hasn't been captured yet — flag them for manual review.
        structural_fallback: list = []
        if not dependents_with_depth:
            structural_dependents = await graph.get_structural_dependents(entity)
            model_structural = [e for e in structural_dependents if e.type.value == "model"]
            if model_structural:
                console.print(
                    f"[yellow]No semantic dependents found for `{entity}`.[/yellow]\n"
                    f"Found {len(model_structural)} structural dependent(s) via lineage — "
                    f"flagging for manual review (no semantic relationship captured yet):"
                )
                for e in model_structural:
                    console.print(f"  [dim]lineage[/dim]  {e.name}")
                structural_fallback = [(e, 99) for e in model_structural]
            else:
                console.print(
                    "[yellow]No dependents found.[/yellow] "
                    "Nothing in the graph depends on this entity structurally or semantically."
                )
                raise typer.Exit(0)
        else:
            console.print(
                f"[green]✓[/green] Found {len(dependents_with_depth)} semantic dependent(s):"
            )
            for dep_entity, depth in dependents_with_depth:
                console.print(f"  [dim]hop {depth}[/dim]  {dep_entity.name}")

        all_dependents = dependents_with_depth or structural_fallback

        if dry_run:
            # Count tokens for each LLM diff call (hop 1 and 2 only)
            dbt = DbtConnector(config={"project_dir": project_dir})
            sql_map = dbt.get_model_sql_map()
            backend = get_backend()
            from puxti.core.redefine import _REDEFINE_SYSTEM_PROMPT

            old_def_line = (
                f"Old definition: {existing_definition.description}"
                if existing_definition else "No previous definition."
            )
            total_input_tokens = 0
            llm_calls = 0
            skipped_deep = 0

            for dep_entity, depth in all_dependents:
                if depth > 2:
                    skipped_deep += 1
                    continue
                model_sql = sql_map.get(dep_entity.id, "")
                if not model_sql:
                    continue
                user_message = (
                    f"Entity redefined: {entity}\n"
                    f"{old_def_line}\n"
                    f"New definition: {description}\n\n"
                    f"Downstream model: {dep_entity.name} (semantic hop depth: {depth})\n\n"
                    f"Current SQL:\n{model_sql}"
                )
                count = await backend.count_input_tokens(
                    user_message, system=_REDEFINE_SYSTEM_PROMPT
                )
                total_input_tokens += count.tokens
                llm_calls += 1

            est_output_tokens = llm_calls * 512
            est_cost = (
                (total_input_tokens / 1_000_000) * backend.input_cost_per_mtok
                + (est_output_tokens / 1_000_000) * backend.output_cost_per_mtok
            )

            depth_summary = "\n".join(
                f"  hop {depth}  {dep_entity.name}"
                + (" [annotation only — no LLM]" if depth > 2 else "")
                for dep_entity, depth in all_dependents
            )
            console.print(
                Panel(
                    f"Entity:                 {entity}\n"
                    f"Dependents found:       {len(all_dependents)}\n"
                    f"\n"
                    f"{depth_summary}\n"
                    f"\n"
                    f"LLM diff calls:         {llm_calls}  (hop 1–2 only)\n"
                    f"Annotation-only:        {skipped_deep}  (hop 3+, no LLM)\n"
                    f"\n"
                    f"Input tokens:           {total_input_tokens:,}\n"
                    f"Est. output tokens:     {est_output_tokens:,}\n"
                    f"Est. cost:              ${est_cost:.4f} USD",
                    title="[bold]Dry run — cost estimate[/bold]",
                    border_style="yellow",
                )
            )
            return

        # 2. Generate diffs — upstream passthrough + downstream semantic
        dbt = DbtConnector(config={"project_dir": project_dir})
        redefiner = SemanticRedefiner()

        ancestors_with_depth = await graph.get_structural_ancestors(entity)
        model_ancestors = [(e, d) for e, d in ancestors_with_depth if e.type.value == "model"]

        if model_ancestors:
            console.print(
                f"[green]✓[/green] Found {len(model_ancestors)} upstream model(s) to update:"
            )
            for anc_entity, depth in model_ancestors:
                console.print(f"  [dim]upstream hop {depth}[/dim]  {anc_entity.name}")

        try:
            with console.status("[bold]Generating diffs...[/bold]"):
                # Extract new attribute name from description for passthrough prompt
                passthrough_diffs = await redefiner.generate_passthrough_diffs(
                    entity_id=entity,
                    new_attribute=description,
                    ancestors_with_depth=model_ancestors,
                    connector=dbt,
                    graph=graph,
                )
                semantic_diffs = await redefiner.generate_diffs(
                    entity_id=entity,
                    old_definition=existing_definition.description if existing_definition else None,
                    new_definition=description,
                    dependents_with_depth=all_dependents,
                    connector=dbt,
                )
        except RuntimeError as exc:
            err_console.print(f"[red]Error:[/red] {exc}")
            raise typer.Exit(1) from exc

        # Deduplicate: semantic diffs take precedence over passthrough diffs for
        # the same file (semantic diff already incorporates the upstream change).
        semantic_paths = {d.file_path for d in semantic_diffs}
        diffs = [d for d in passthrough_diffs if d.file_path not in semantic_paths] + semantic_diffs

        if not diffs:
            console.print(
                "[yellow]No diffs generated.[/yellow] "
                "Affected models may not have SQL files in the configured dbt project."
            )
            raise typer.Exit(0)

        console.print(
            f"[green]✓[/green] Generated {len(diffs)} diff(s): "
            f"{len(passthrough_diffs)} upstream passthrough, "
            f"{len(semantic_diffs)} downstream semantic."
        )

        # 3. Build event and propagation objects — IDs needed before PR creation.
        # Writes to the graph are deferred until after the PR succeeds so a failed
        # open_pr() does not leave orphaned definitions or change events behind.
        from puxti.models import Definition, PropagationResult, SemanticChangeEvent
        new_definition = Definition(
            entity_id=entity,
            description=description,
            version=(existing_definition.version + 1) if existing_definition else 1,
            created_by="user",
        )

        event = ChangeEvent(
            type=ChangeType.SEMANTIC,
            source_entity_id=entity,
            change={"description": description},
            semantic_context=description,
            declared_by="user",
        )

        result = PropagationResult(
            change_event_id=event.id,
            connector="dbt",
            target_entity_id=entity,
            diffs=diffs,
        )
        sem_event = SemanticChangeEvent(
            change_event_id=event.id,
            entity_id=entity,
            change_type=ChangeType.SEMANTIC,
            semantic_context=description,
            affected_entity_ids=[e.id for e, _ in all_dependents],
            reasoning="Semantic graph traversal — see per-file annotations for details.",
            change={"description": description},
        )

        # 4. Open PR first — only persist to graph on success
        gh = GitHubConnector(
            config={
                "repo": repo,
                "token": settings.github_token,
                "base_branch": base_branch,
            }
        )
        with console.status("[bold]Opening GitHub PR...[/bold]"):
            updated = await gh.open_pr(result, sem_event)

        # 5. PR succeeded — now safe to write definition and change event
        await graph.upsert_definition(new_definition)
        event.status = ChangeStatus.CAPTURED
        await graph.save_change_event(event)

        file_list = ", ".join(f"`{d.file_path}`" for d in updated.diffs)
        console.print(
            Panel(
                f"[bold green]PR opened:[/bold green] {updated.pr_url}\n"
                f"Files: {file_list}",
                title="[bold]Puxti[/bold]",
                border_style="green",
            )
        )

    finally:
        await graph.close()
