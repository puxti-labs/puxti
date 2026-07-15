"""`puxti capture` — propagate a structural change as a reviewable PR."""

from typing import Optional

import typer
from rich.panel import Panel

from puxti.cli._app import app
from puxti.cli._shared import _load_workspace, _run, console, err_console
from puxti.connectors.dbt import DbtConnector
from puxti.connectors.github import GitHubConnector
from puxti.connectors.registry import build_configured_connectors
from puxti.core.capture import SemanticCapture, _build_user_message
from puxti.core.graph import KnowledgeGraph
from puxti.llm import COST_UNKNOWN_HINT
from puxti.models import ChangeEvent, ChangeType
from puxti.propagation.engine import PropagationEngine
from puxti.settings import settings


@app.command()
def capture(
    entity: str = typer.Option(
        ..., "--entity", "-e",
        help="Entity ID being changed (e.g. model.jaffle_shop.orders.order_date)",
    ),
    before: str = typer.Option(..., help="Value before the change (e.g. old column name)"),
    after: str = typer.Option(..., help="Value after the change (e.g. new column name)"),
    description: str = typer.Option(
        ...,
        "--description",
        "-d",
        help="Human description of what this change means and why",
    ),
    repo: Optional[str] = typer.Option(None, help="GitHub repository to open PR against (owner/repo) — required unless --dry-run. Falls back to .puxti.yml connectors.dbt.repo."),
    base_branch: Optional[str] = typer.Option(None, help="Base branch for the PR (default: main or from .puxti.yml)."),
    dbt_project_dir: Optional[str] = typer.Option(
        None, "--dbt-project-dir", help="Path to dbt project root (overrides .puxti.yml and DBT_PROJECT_DIR)"
    ),
    repo_subdir: Optional[str] = typer.Option(
        None, "--repo-subdir",
        help="Subdirectory of the repo where the dbt project lives (e.g. 'sports_sims'). "
             "Use when the dbt project is not at the repo root."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Estimate token count and cost without running the capture."
    ),
) -> None:
    """Propagate a schema change safely across the stack as a reviewable PR.

    Captures the semantic meaning of a column rename or other structural change,
    reasons about downstream impact, and opens a GitHub PR with the generated diffs.

    Use --dry-run to estimate the API cost before committing. --repo is not
    required when --dry-run is set.
    """
    ws = _load_workspace()
    resolved_repo = repo or (ws.dbt.repo if ws.dbt else None)
    resolved_project_dir = dbt_project_dir or (ws.dbt.project_dir if ws.dbt else None)
    resolved_repo_subdir = repo_subdir or (ws.dbt.repo_subdir if ws.dbt else None)
    resolved_base_branch = base_branch or (ws.dbt.base_branch if ws.dbt else "main")

    if not dry_run and not resolved_repo:
        err_console.print(
            "[red]Error:[/red] --repo is required unless using --dry-run.\n"
            "  Set it via flag or add [bold]connectors.dbt.repo[/bold] to .puxti.yml."
        )
        raise typer.Exit(1)
    _run(
        _run_capture(
            entity=entity,
            before=before,
            after=after,
            description=description,
            repo=resolved_repo,
            base_branch=resolved_base_branch,
            dbt_project_dir=resolved_project_dir,
            repo_subdir=resolved_repo_subdir,
            dry_run=dry_run,
        ),
        command="capture",
    )


async def _run_capture(
    entity: str,
    before: str,
    after: str,
    description: str,
    repo: str | None,
    base_branch: str,
    dbt_project_dir: str | None,
    repo_subdir: str | None = None,
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

    event = ChangeEvent(
        type=ChangeType.STRUCTURAL,
        source_entity_id=entity,
        change={"before": {"name": before}, "after": {"name": after}},
    )

    graph = KnowledgeGraph()
    try:
        await graph.connect()

        # Validate entity exists in graph — warn if not found, but don't block.
        # get_structural_dependents will fall back to name-based resolution,
        # so the LLM still gets correct lineage context in most cases.
        entity_in_graph = await graph.get_entity_by_id(entity)
        if not entity_in_graph:
            # Try the parent model entity (strip column suffix) as a hint
            parent_id = entity.rsplit(".", 1)[0] if "." in entity else None
            parent_hint = ""
            if parent_id:
                parent_entity = await graph.get_entity_by_id(parent_id)
                if parent_entity:
                    parent_hint = (
                        f" For more precise results, use the model entity ID: "
                        f"[bold]{parent_id}[/bold]"
                    )
            console.print(
                f"[yellow]Note:[/yellow] Entity [bold]{entity}[/bold] not found in the "
                f"Knowledge Graph — resolving lineage by model name. "
                f"Run [bold]puxti describe[/bold] to see canonical entity IDs."
                + parent_hint
            )

        capture = SemanticCapture()

        # --dry-run: count tokens and show cost estimate, then exit.
        # Build the same prompt the real capture sends — including the full
        # known-entity-ID list — so the estimate matches what will be billed.
        if dry_run:
            existing_definition = await graph.get_latest_definition(event.source_entity_id)
            semantic_dependents = await graph.get_semantic_dependents(event.source_entity_id)
            structural_dependents = await graph.get_structural_dependents(event.source_entity_id)
            all_entity_ids = await graph.get_all_entity_ids()
            user_message = _build_user_message(
                event=event,
                user_input=description,
                existing_definition=existing_definition.description if existing_definition else None,
                semantic_dependent_names=[e.name for e in semantic_dependents],
                structural_dependent_names=[e.name for e in structural_dependents],
                known_entity_ids=all_entity_ids,
            )
            with console.status("[bold]Counting tokens...[/bold]"):
                estimate = await capture.estimate_cost(user_message)
            approx = "" if estimate["tokens_exact"] else " (approximate)"
            cost_line = (
                f"Est. cost:              ${estimate['estimated_cost_usd']:.4f} USD"
                if estimate["estimated_cost_usd"] is not None
                else f"Est. cost:              {COST_UNKNOWN_HINT}"
            )
            console.print(
                Panel(
                    f"Input tokens:           {estimate['input_tokens']:,}{approx}\n"
                    f"Est. output tokens:     {estimate['estimated_output_tokens']:,}\n"
                    f"{cost_line}",
                    title="[bold]Dry run — cost estimate[/bold]",
                    border_style="yellow",
                )
            )
            return

        # Step 1 — semantic capture
        try:
            with console.status("[bold]Capturing semantic meaning...[/bold]"):
                semantic_event, commit_to_graph = await capture.capture(event, description, graph)
        except RuntimeError as exc:
            err_console.print(f"[red]Error:[/red] {exc}")
            raise typer.Exit(1) from exc

        console.print(f"\n[bold]Enriched definition:[/bold]")
        console.print(f"  {semantic_event.semantic_context}")
        if semantic_event.affected_entity_ids:
            console.print(f"\n[bold]Affected entities:[/bold]")
            for eid in semantic_event.affected_entity_ids:
                console.print(f"  • {eid}")
        if semantic_event.reasoning:
            console.print(f"\n[bold]Reasoning:[/bold] {semantic_event.reasoning[:200]}"
                          + ("…" if len(semantic_event.reasoning) > 200 else ""))

        confirm_kg = console.input(
            "\nSave this enrichment to the Knowledge Graph? ([bold]y[/bold]=yes, n=skip) > "
        ).strip().lower()
        if confirm_kg == "y":
            await commit_to_graph()
            console.print("[green]✓[/green] Enrichment saved to Knowledge Graph.")
        else:
            console.print("[yellow]Skipping Knowledge Graph update.[/yellow]")

        # Step 2 — propagation. dbt is built here (its project_dir resolution
        # is flag/env-driven); every other configured producer comes from the
        # registry.
        with console.status("[bold]Generating diffs...[/bold]"):
            ws_for_propagation = _load_workspace()
            dbt = DbtConnector(config={"project_dir": project_dir})
            connectors = [dbt] + [
                c for c in build_configured_connectors(ws_for_propagation)
                if c.name != "dbt"
            ]
            results = await PropagationEngine(connectors=connectors).propagate(semantic_event)

        if not results:
            affected = semantic_event.affected_entity_ids or []
            # Detect cross-project case: affected dbt entities belong to a
            # different dbt project than the one currently configured. Only
            # dbt-shaped IDs carry a project segment in position 1.
            current_project = dbt.get_project_name()
            foreign_projects = {
                eid.split(".")[1]
                for eid in affected
                if eid.split(".")[0] in ("model", "source")
                and len(eid.split(".")) >= 2 and eid.split(".")[1] != current_project
            }
            if foreign_projects:
                projects_hint = ", ".join(f"`{p}`" for p in sorted(foreign_projects))
                console.print(
                    f"[yellow]No diffs generated.[/yellow] The affected models belong to "
                    f"a different dbt project ({projects_hint}). Re-run with "
                    f"[bold]--dbt-project-dir[/bold] pointing to that project's root."
                )
            else:
                console.print(
                    "[yellow]No diffs generated.[/yellow] "
                    "No files reference this entity in the configured dbt project."
                )
            return

        # Rebase diff paths onto each connector's repo layout. Diff paths come
        # back relative to the connector's own project root; the PR needs them
        # relative to the repo root.
        def _repo_prefix(connector_name: str) -> str:
            cfg = ws_for_propagation.get(connector_name)
            if connector_name == "dbt":
                # resolved at command level: flag beats .puxti.yml
                parts = [repo_subdir]
            elif connector_name == "airflow":
                # airflow diff paths are relative to the dags dir
                parts = [
                    cfg.repo_subdir if cfg else None,
                    cfg.extras.get("dags_dir", "dags") if cfg else "dags",
                ]
            else:
                parts = [cfg.repo_subdir if cfg else None]
            return "/".join(p.strip("/") for p in parts if p and p.strip("/"))

        for result in results:
            prefix = _repo_prefix(result.connector)
            if not prefix:
                continue
            for diff in result.diffs:
                diff.file_path = f"{prefix}/{diff.file_path}"

        total_files = sum(len(r.diffs) for r in results)
        console.print(
            f"[green]✓[/green] Generated {total_files} file diff(s) "
            f"across {len(results)} connector(s)."
        )

        all_unverified = [eid for r in results for eid in r.unverified_entity_ids]
        if all_unverified:
            unverified_list = "\n".join(f"  • {eid}" for eid in all_unverified)
            console.print(
                f"[yellow]⚠ {len(all_unverified)} model(s) were flagged as potentially "
                f"affected but could not be safely propagated.[/yellow]\n"
                f"  Puxti could not confirm the `{semantic_event.change.get('before', {}).get('name', '')}` "
                f"column in these models traces back to the renamed source — "
                f"this can happen with non-standard dbt layering, shared column names, "
                f"or table names that differ from their entity name (e.g. Prisma @@map).\n"
                f"  Review manually:\n{unverified_list}"
            )

        # Step 3 — open PRs (one per connector, each to its configured repo)
        # Track opened PRs so each can reference the others as companion PRs.
        opened: list[tuple[str, str, str, GitHubConnector]] = []  # (connector, repo, pr_url, gh)

        with console.status("[bold]Opening GitHub PR...[/bold]"):
            for result in results:
                # Each connector PRs to its own configured repo; anything
                # without one falls back to the dbt repo from --repo/.puxti.yml.
                result_cfg = ws_for_propagation.get(result.connector)
                if result.connector != "dbt" and result_cfg and result_cfg.repo:
                    pr_repo = result_cfg.repo
                    pr_base_branch = result_cfg.base_branch
                else:
                    pr_repo = repo
                    pr_base_branch = base_branch
                gh = GitHubConnector(
                    config={
                        "repo": pr_repo,
                        "token": settings.github_token,
                        "base_branch": pr_base_branch,
                    }
                )
                companions_so_far = [(c, r, u) for c, r, u, _ in opened]
                updated = await gh.open_pr(result, semantic_event, companions=companions_so_far)
                opened.append((result.connector, pr_repo, updated.pr_url, gh))
                file_list = ", ".join(f"`{d.file_path}`" for d in updated.diffs)
                console.print(
                    Panel(
                        f"[bold green]PR opened:[/bold green] {updated.pr_url}\n"
                        f"Files: {file_list}",
                        title="[bold]Puxti[/bold]",
                        border_style="green",
                    )
                )

            # Patch earlier PRs to add companion references to PRs opened after them
            if len(opened) > 1:
                for i, (connector, pr_repo, pr_url, gh) in enumerate(opened[:-1]):
                    later = [(c, r, u) for c, r, u, _ in opened[i + 1:]]
                    await gh.add_companion_note(pr_url, later, this_connector=connector)

    finally:
        await graph.close()
