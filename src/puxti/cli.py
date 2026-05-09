import asyncio
import threading
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Optional

import anthropic
import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from puxti import __version__
from puxti.connectors.airflow import AirflowConnector
from puxti.connectors.dbt import DbtConnector
from puxti.connectors.github import GitHubConnector, _companion_section
from puxti.core.capture import SemanticCapture, _build_user_message
from puxti.core.corrector import SemanticCorrector
from puxti.core.graph import KnowledgeGraph
from puxti.core.redefine import SemanticRedefiner
from puxti.core.scanner import SemanticScanner
from puxti.models import ChangeEvent, ChangeStatus, ChangeType, CorrectionEvent, Definition, EdgeType, Entity, EntityType, SemanticEdge
from puxti.propagation.engine import PropagationEngine
from puxti.settings import settings
from puxti.workspace import WorkspaceConfig, load_workspace

app = typer.Typer(
    name="puxti",
    help="Safe schema and semantic change propagation for data teams.",
    no_args_is_help=True,
)

console = Console()
err_console = Console(stderr=True)

_UPDATE_CHECK_INTERVAL = timedelta(hours=24)
_PYPI_URL = "https://pypi.org/pypi/puxti/json"
_update_notice: list[str] = []  # populated by background thread, read after command


def _check_for_update() -> None:
    """Fetch latest version from PyPI and queue a notice if newer than installed."""
    try:
        from pathlib import Path as _Path
        import json
        import tomli_w

        try:
            import tomllib
        except ImportError:
            import tomli as tomllib  # type: ignore[no-redef]

        config_path = _Path.home() / ".puxti" / "config.toml"
        data: dict = {}
        if config_path.exists():
            with open(config_path, "rb") as f:
                data = tomllib.load(f)

        update = data.get("update", {})
        last_checked_str = update.get("last_checked")
        if last_checked_str:
            last_checked = datetime.fromisoformat(last_checked_str)
            if datetime.now(timezone.utc) - last_checked < _UPDATE_CHECK_INTERVAL:
                return

        with urllib.request.urlopen(_PYPI_URL, timeout=3) as resp:
            latest = json.loads(resp.read())["info"]["version"]

        # Record check time
        data.setdefault("update", {})["last_checked"] = datetime.now(timezone.utc).isoformat()
        config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(config_path, "wb") as f:
            tomli_w.dump(data, f)

        if __version__ == "dev" or latest == __version__:
            return

        def _parse(v: str) -> tuple:
            try:
                return tuple(int(x) for x in v.split("."))
            except ValueError:
                return (0,)

        if _parse(latest) > _parse(__version__):
            _update_notice.append(latest)

    except Exception:
        pass  # never break the CLI over an update check


def _run(coro) -> None:
    """Run an async command, catching unexpected errors with actionable guidance."""
    updater = threading.Thread(target=_check_for_update, daemon=True)
    updater.start()
    try:
        asyncio.run(coro)
    except (typer.Exit, KeyboardInterrupt):
        raise
    except Exception as exc:
        err_console.print(f"\n[red bold]Unexpected error:[/red bold] {exc}")
        err_console.print(
            f"\n[yellow]Something went wrong. To report this bug:[/yellow]\n"
            f"  1. Copy the full error above and the traceback below\n"
            f"  2. Note the exact command you ran\n"
            f"  3. Email [bold]puxti@okolico.com[/bold] with:\n"
            f"     - puxti version (shown below)\n"
            f"     - the exact command you ran\n"
            f"     - the error message and traceback\n"
            f"\n[dim]puxti {__version__}[/dim]\n"
        )
        err_console.print_exception(show_locals=False)
        raise typer.Exit(1)
    finally:
        updater.join(timeout=4)
        if _update_notice:
            console.print(
                f"\n[yellow]Update available:[/yellow] {__version__} → {_update_notice[0]}\n"
                f"  Run: [bold]pip install --upgrade puxti[/bold]"
            )


def _load_workspace() -> WorkspaceConfig:
    """Load .puxti.yml walking up from CWD. Exit with error on parse/version failure."""
    try:
        return load_workspace()
    except ValueError as exc:
        err_console.print(f"[red bold]Config error:[/red bold] {exc}")
        raise typer.Exit(1)


def _parse_entity_id(entity_id: str) -> tuple[EntityType, str, str]:
    """Return (EntityType, source_connector, project) from a puxti entity ID.

    Supported prefixes:
      task.airflow.<dag_id>.<task_id>  → TASK,  airflow, dag_id
      source.<project>.<table>         → TABLE, dbt,     project
      model.<project>.<name>           → MODEL, dbt,     project
    """
    parts = entity_id.split(".")
    if parts[0] == "task" and len(parts) >= 4 and parts[1] == "airflow":
        return EntityType.TASK, "airflow", parts[2]
    if parts[0] == "source" and len(parts) >= 3:
        return EntityType.TABLE, "dbt", parts[1]
    if parts[0] == "model" and len(parts) >= 3:
        return EntityType.MODEL, "dbt", parts[1]
    raise ValueError(
        f"Unrecognized entity ID: {entity_id!r}\n"
        "  Expected: task.airflow.<dag>.<task>  or  source.<project>.<table>  or  model.<project>.<name>"
    )


async def _run_link(from_entity: str, to_entity: str, description: str) -> None:
    from_type, from_connector, from_project = _parse_entity_id(from_entity)
    to_type, to_connector, to_project = _parse_entity_id(to_entity)

    kg = KnowledgeGraph()
    await kg.connect()
    try:
        from_ent = await kg.upsert_entity_by_name(
            Entity(name=from_entity, type=from_type, source_connector=from_connector, project=from_project)
        )
        to_ent = await kg.upsert_entity_by_name(
            Entity(name=to_entity, type=to_type, source_connector=to_connector, project=to_project)
        )
        await kg.upsert_semantic_edge(SemanticEdge(
            from_entity_id=from_ent.id,
            to_entity_id=to_ent.id,
            type=EdgeType.FEEDS,
            description=description,
            created_by="user",
        ))
    finally:
        await kg.close()

    console.print(f"[green]✓[/green]  {from_entity}")
    console.print(f"        ──FEEDS──▶  {to_entity}")


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"puxti {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False, "--version", "-v", callback=_version_callback, is_eager=True,
        help="Show puxti version and exit.",
    ),
) -> None:
    pass


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
        )
    )


@app.command()
def scan(
    dbt_project_dir: Optional[str] = typer.Option(
        None, "--dbt-project-dir", help="Path to dbt project root (overrides .puxti.yml and DBT_PROJECT_DIR)"
    ),
    interactive: bool = typer.Option(
        False, "--interactive", "-i",
        help="Confirm each definition one by one (slower, higher accuracy).",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Estimate token count and cost without running the scan."
    ),
) -> None:
    """Populate the Knowledge Graph so puxti can propagate changes safely.

    Reads the dbt manifest, extracts all entities and lineage, infers starter
    definitions via LLM, and proposes semantic edges.

    Two modes:
    - Default (auto): generates all definitions in one pass, shows a summary,
      you confirm everything at once before anything is written.
    - Interactive (--interactive): walks through each model one by one,
      you confirm or edit each definition before moving to the next.

    Nothing is written to the Knowledge Graph without your explicit confirmation.
    Run this before using `puxti redefine`.
    """
    ws = _load_workspace()
    resolved_project_dir = dbt_project_dir or (ws.dbt.project_dir if ws.dbt else None)
    _run(_run_scan(dbt_project_dir=resolved_project_dir, interactive=interactive, dry_run=dry_run))


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
    _run(_run_link(from_entity=from_entity, to_entity=to_entity, description=description))


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
        )
    )


@app.command()
def correct(
    entity: str = typer.Option(
        ..., "--entity", "-e",
        help="Entity ID to correct (e.g. model.jaffle_shop.orders)"
    ),
    project: Optional[str] = typer.Option(
        None, "--project", "-p",
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
    _run(_run_correct(entity=entity, project=project))


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
    _run(_run_purge(project=project, all_projects=all_projects))


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
    _run(_run_describe(entity=entity, project=project))


@app.command()
def config() -> None:
    """Show the current puxti configuration and where it is loaded from."""
    import os
    from pathlib import Path

    def _mask(value: str, secret: bool = False) -> str:
        if not value:
            return "[dim]not set[/dim]"
        if not secret:
            return value
        if len(value) <= 8:
            return "********"
        return value[:8] + "..." + value[-4:]

    env_file = Path(os.getcwd()) / ".env"

    table = Table(title="puxti config", show_header=False, box=None)
    table.add_column("Key", style="bold", no_wrap=True)
    table.add_column("Value")

    table.add_row("neo4j_uri",         _mask(settings.neo4j_uri))
    table.add_row("neo4j_username",    _mask(settings.neo4j_username))
    table.add_row("neo4j_password",    _mask(settings.neo4j_password, secret=True))
    table.add_row("anthropic_api_key", _mask(settings.anthropic_api_key, secret=True))
    table.add_row("github_token",      _mask(settings.github_token, secret=True))
    table.add_row("dbt_project_dir",   _mask(settings.dbt_project_dir))
    table.add_row("dbt_profiles_dir",  _mask(settings.dbt_profiles_dir))

    console.print(table)
    console.print()

    env_status = "[green]found[/green]" if env_file.exists() else "[dim]not found[/dim]"
    console.print(f"[bold].env file:[/bold]  {env_file}  {env_status}")

    ws = _load_workspace()
    if ws.path:
        parts = []
        if ws.dbt:
            parts.append(f"dbt ({ws.dbt.repo or '(no repo)'} · {ws.dbt.project_dir or '(no dir)'})")
        if ws.airflow:
            parts.append(f"airflow ({ws.airflow.repo or '(no repo)'} · {ws.airflow.project_dir or '(no dir)'})")
        connectors_summary = ", ".join(parts) if parts else "(no connectors configured)"
        console.print(f"[bold].puxti.yml:[/bold]  {ws.path}  [green]found[/green]")
        console.print(f"  connectors: {connectors_summary}")
    else:
        console.print("[bold].puxti.yml:[/bold]  [dim]not found — using flags and env vars[/dim]")


@app.command()
def health(
    dbt_project_dir: Optional[str] = typer.Option(
        None, "--dbt-project-dir", help="Path to dbt project root"
    ),
) -> None:
    """Check connectivity to all configured services."""
    ws = _load_workspace()
    resolved_project_dir = dbt_project_dir or (ws.dbt.project_dir if ws.dbt else None)
    _run(_run_health(dbt_project_dir=resolved_project_dir, workspace=ws))


# ── Async implementations ──────────────────────────────────────────────────────


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

        # --dry-run: count tokens and show cost estimate, then exit
        if dry_run:
            existing_definition = await graph.get_latest_definition(event.source_entity_id)
            semantic_dependents = await graph.get_semantic_dependents(event.source_entity_id)
            structural_dependents = await graph.get_structural_dependents(event.source_entity_id)
            user_message = _build_user_message(
                event=event,
                user_input=description,
                existing_definition=existing_definition.description if existing_definition else None,
                semantic_dependent_names=[e.name for e in semantic_dependents],
                structural_dependent_names=[e.name for e in structural_dependents],
            )
            with console.status("[bold]Counting tokens...[/bold]"):
                estimate = await capture.estimate_cost(user_message)
            console.print(
                Panel(
                    f"Input tokens:           {estimate['input_tokens']:,}\n"
                    f"Est. output tokens:     {estimate['estimated_output_tokens']:,}\n"
                    f"Est. cost:              ${estimate['estimated_cost_usd']:.4f} USD",
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

        # Step 2 — propagation
        with console.status("[bold]Generating diffs...[/bold]"):
            dbt = DbtConnector(config={"project_dir": project_dir})
            connectors: list = [dbt]
            ws_for_propagation = _load_workspace()
            if ws_for_propagation.airflow and ws_for_propagation.airflow.project_dir:
                dags_subdir = ws_for_propagation.airflow.extras.get("dags_dir", "dags")
                from pathlib import Path as _Path
                dags_dir_path = str(_Path(ws_for_propagation.airflow.project_dir) / dags_subdir)
                connectors.append(AirflowConnector(config={"dags_dir": dags_dir_path}))
            results = await PropagationEngine(connectors=connectors).propagate(semantic_event)

        if not results:
            affected = semantic_event.affected_entity_ids or []
            # Detect cross-project case: affected entities belong to a different
            # dbt project than the one currently configured.
            current_project = dbt.get_project_name()
            foreign_projects = {
                eid.split(".")[1]
                for eid in affected
                if len(eid.split(".")) >= 2 and eid.split(".")[1] != current_project
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

        # Apply repo subdir prefix when the dbt project is not at the repo root
        if repo_subdir:
            subdir = repo_subdir.strip("/")
            for result in results:
                for diff in result.diffs:
                    diff.file_path = f"{subdir}/{diff.file_path}"

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
                f"this can happen with non-standard dbt layering or shared column names.\n"
                f"  Review manually:\n{unverified_list}"
            )

        # Step 3 — open PRs (one per connector, each to its configured repo)
        # Track opened PRs so each can reference the others as companion PRs.
        opened: list[tuple[str, str, str, GitHubConnector]] = []  # (connector, repo, pr_url, gh)

        with console.status("[bold]Opening GitHub PR...[/bold]"):
            for result in results:
                if result.connector == "airflow" and ws_for_propagation.airflow and ws_for_propagation.airflow.repo:
                    pr_repo = ws_for_propagation.airflow.repo
                    pr_base_branch = ws_for_propagation.airflow.base_branch
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


async def _run_scan(dbt_project_dir: str | None, interactive: bool, dry_run: bool = False) -> None:
    project_dir = dbt_project_dir or settings.dbt_project_dir
    if not project_dir:
        err_console.print(
            "[red]Error:[/red] dbt project directory not configured. "
            "Pass --dbt-project-dir or set DBT_PROJECT_DIR."
        )
        raise typer.Exit(1)

    dbt = DbtConnector(config={"project_dir": project_dir})
    scanner = SemanticScanner()

    if dry_run:
        with console.status("[bold]Reading manifest and counting tokens...[/bold]"):
            estimate = await scanner.estimate_scan_cost(dbt)
        console.print(
            Panel(
                f"Models to define:       {estimate['models']}\n"
                f"Total entities:         {estimate['entities_total']}\n"
                f"\n"
                f"Definition calls\n"
                f"  Input tokens:         {estimate['def_input_tokens']:,}\n"
                f"  Est. output tokens:   {estimate['def_est_output_tokens']:,}\n"
                f"\n"
                f"Edge proposal call\n"
                f"  Input tokens:         {estimate['edges_input_tokens']:,}\n"
                f"  Est. output tokens:   {estimate['edges_est_output_tokens']:,}\n"
                f"\n"
                f"Total input tokens:     {estimate['total_input_tokens']:,}\n"
                f"Total est. output:      {estimate['total_est_output_tokens']:,}\n"
                f"Est. cost:              ${estimate['estimated_cost_usd']:.4f} USD",
                title="[bold]Dry run — cost estimate[/bold]",
                border_style="yellow",
            )
        )
        return

    graph = KnowledgeGraph()
    try:
        await graph.connect()
        mode = "interactive" if interactive else "auto"
        console.print(f"[bold]Scanning dbt project[/bold] ({mode} mode)")
        result = await scanner.scan(dbt, graph, interactive=interactive, console=console)
        console.print(
            f"\n[green]✓[/green] Scan complete: "
            f"{result.entities_upserted} entities, "
            f"{result.definitions_written} definitions, "
            f"{result.semantic_edges_written} semantic edges written."
        )
    finally:
        await graph.close()


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
                err_console.print(f"[red]Error:[/red] Entity '{entity}' not found in the Knowledge Graph.")
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
        corrected = console.input("[bold]Enter corrected definition[/bold] (blank to cancel) > ").strip()
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
                other = assessment.to_entity_id if assessment.from_entity_id == entity else assessment.from_entity_id
                edge_obj = next(
                    e for e in edges
                    if (e.from_entity_id, e.to_entity_id) == key
                )
                console.print(f"  {direction} ({edge_obj.type.value}) {other}")
                console.print(f"  LLM suggests: [bold]{assessment.action.upper()}[/bold] — {assessment.reasoning}")
                if assessment.action == "update":
                    console.print(f"  New description: {assessment.updated_description}")

                choice = console.input(
                    "  Accept? ([bold]y[/bold]=yes, k=keep, r=remove, blank=accept) > "
                ).strip().lower()

                if choice in ("", "y"):
                    confirmed_assessments.append(assessment)
                elif choice == "k":
                    from puxti.models import EdgeAssessment
                    confirmed_assessments.append(EdgeAssessment(
                        from_entity_id=assessment.from_entity_id,
                        to_entity_id=assessment.to_entity_id,
                        action="keep",
                        reasoning="User overrode to keep",
                    ))
                elif choice == "r":
                    from puxti.models import EdgeAssessment
                    confirmed_assessments.append(EdgeAssessment(
                        from_entity_id=assessment.from_entity_id,
                        to_entity_id=assessment.to_entity_id,
                        action="remove",
                        reasoning="User overrode to remove",
                    ))
                else:
                    confirmed_assessments.append(assessment)
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
        confirm = console.input("Write these changes? ([bold]y[/bold]=yes, n=cancel) > ").strip().lower()
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
            edges_kept=[(e.from_entity_id, e.to_entity_id) for e in updated_edges if (e.from_entity_id, e.to_entity_id) not in updated_pairs],
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
            import anthropic as _anthropic
            _client = _anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
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
                response = await _client.messages.count_tokens(
                    model="claude-sonnet-4-6",
                    system=_REDEFINE_SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": user_message}],
                )
                total_input_tokens += response.input_tokens
                llm_calls += 1

            est_output_tokens = llm_calls * 512
            est_cost = (
                (total_input_tokens / 1_000_000) * 3.00
                + (est_output_tokens / 1_000_000) * 15.00
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


async def _run_health(dbt_project_dir: str | None, workspace: WorkspaceConfig | None = None) -> None:
    all_ok = True

    # Neo4j
    graph = KnowledgeGraph()
    try:
        await graph.connect()
        console.print("[green]✓[/green] Neo4j")
        await graph.close()
    except Exception as exc:
        console.print(f"[red]✗[/red] Neo4j: {exc}")
        all_ok = False

    # Anthropic API — uses count_tokens (no credits consumed)
    if settings.anthropic_api_key:
        try:
            client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
            await client.messages.count_tokens(
                model="claude-sonnet-4-6",
                messages=[{"role": "user", "content": "ping"}],
            )
            console.print("[green]✓[/green] Anthropic API key")
        except anthropic.AuthenticationError:
            console.print("[red]✗[/red] Anthropic API key: invalid or expired")
            all_ok = False
        except anthropic.BadRequestError as exc:
            if "credit balance" in str(exc).lower():
                console.print("[red]✗[/red] Anthropic API key: valid but credit balance is too low")
            else:
                console.print(f"[red]✗[/red] Anthropic API: {exc}")
            all_ok = False
        except Exception as exc:
            console.print(f"[red]✗[/red] Anthropic API: {exc}")
            all_ok = False
    else:
        console.print("[yellow]–[/yellow] Anthropic API key (ANTHROPIC_API_KEY not configured)")
        all_ok = False

    # dbt connector
    project_dir = dbt_project_dir or settings.dbt_project_dir
    if project_dir:
        try:
            ok = await DbtConnector(config={"project_dir": project_dir}).health_check()
            if ok:
                console.print("[green]✓[/green] dbt manifest")
            else:
                console.print("[red]✗[/red] dbt manifest not found — run `dbt compile`")
                all_ok = False
        except Exception as exc:
            console.print(f"[red]✗[/red] dbt: {exc}")
            all_ok = False
    else:
        console.print("[yellow]–[/yellow] dbt (DBT_PROJECT_DIR not configured)")

    # Airflow connector
    if workspace and workspace.airflow and workspace.airflow.project_dir:
        from pathlib import Path as _Path
        _dags_subdir = workspace.airflow.extras.get("dags_dir", "dags")
        _dags_dir = str(_Path(workspace.airflow.project_dir) / _dags_subdir)
        try:
            ok = await AirflowConnector(config={"dags_dir": _dags_dir}).health_check()
            if ok:
                console.print(f"[green]✓[/green] Airflow dags dir — {_dags_dir}")
            else:
                console.print(f"[red]✗[/red] Airflow dags dir not found — {_dags_dir}")
                all_ok = False
        except Exception as exc:
            console.print(f"[red]✗[/red] Airflow: {exc}")
            all_ok = False
    elif workspace and workspace.airflow:
        console.print("[yellow]–[/yellow] Airflow (project_dir not set in .puxti.yml)")
    else:
        console.print("[yellow]–[/yellow] Airflow (not configured in .puxti.yml)")

    # GitHub write access — one check per connector with a repo configured
    if workspace:
        for repo, connector_type in workspace.connector_repos():
            if not settings.github_token:
                console.print(f"[yellow]–[/yellow] GitHub write access — {repo} ({connector_type}): GITHUB_TOKEN not configured")
                all_ok = False
                continue
            try:
                gh = GitHubConnector(config={"repo": repo, "token": settings.github_token})
                ok = await gh.health_check()
                if ok:
                    console.print(f"[green]✓[/green] GitHub write access — {repo} ({connector_type})")
                else:
                    console.print(f"[red]✗[/red] GitHub write access — {repo} ({connector_type}): no write permission")
                    all_ok = False
            except Exception as exc:
                console.print(f"[red]✗[/red] GitHub write access — {repo} ({connector_type}): {exc}")
                all_ok = False

    if not all_ok:
        raise typer.Exit(1)
