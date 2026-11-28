"""`puxti impact` — blast radius of a change: from the graph, or between two dbt builds."""

import json
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import typer
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from puxti.cli._app import app
from puxti.cli._shared import _load_workspace, _run, console, err_console
from puxti.core.graph import KnowledgeGraph

if TYPE_CHECKING:
    from puxti.core.bi_impact import ImpactReport


@app.command()
def impact(
    entity: Optional[str] = typer.Argument(
        None, help="Entity ID to analyze (e.g. model.jaffle_shop.orders). Omit with --state."
    ),
    change_type: Optional[str] = typer.Option(
        None,
        "--change-type",
        help="Type of change to evaluate: rename, redefine, drop, type_change",
    ),
    state: Optional[str] = typer.Option(
        None,
        "--state",
        help="Folder holding the base build's manifest.json (and catalog.json), e.g. the "
        "production job's target/. Compares it with the current target/ and lists the BI "
        "specs the change breaks or affects, offline.",
    ),
    specs_dir: Optional[str] = typer.Option(
        None,
        "--specs-dir",
        help="With --state: BI spec folder. Defaults as for `puxti validate`.",
    ),
    dbt_project_dir: Optional[str] = typer.Option(
        None,
        "--dbt-project-dir",
        help="With --state: dbt project whose target/ is the current build. Defaults to "
        ".puxti.yml, then DBT_PROJECT_DIR.",
    ),
    as_json: bool = typer.Option(
        False,
        "--json",
        help="Output as JSON (for an entity, the same shape as the MCP impact_of_change tool).",
    ),
) -> None:
    """Show what depends on an entity and what would be affected by a change.

    With an ENTITY, queries the Knowledge Graph for semantic and structural
    dependents without making any LLM calls. Run `puxti scan` first to populate
    the graph. Use --change-type to see which dependents are primary risk for a
    specific type of change: rename (structural), redefine (semantic), drop or
    type_change (both).

    With --state DIR instead, compares two dbt builds (DIR and the current
    target/) and lists the BI datasets, charts and dashboards the change breaks
    or affects. No Knowledge Graph, no LLM; puxti runs no dbt command. Exits 1 when an asset
    breaks, so it can gate CI; affected assets and warnings exit 0.
    """
    if state is not None:
        if entity is not None or change_type is not None:
            err_console.print(
                "[red]Error:[/red] --state compares two dbt builds; it takes no ENTITY "
                "or --change-type."
            )
            raise typer.Exit(1)
        _run(_run_state_impact(state, specs_dir, dbt_project_dir, as_json), command="impact")
        return
    if entity is None:
        err_console.print("[red]Error:[/red] Pass an ENTITY ID, or --state DIR to compare builds.")
        raise typer.Exit(1)
    if specs_dir is not None or dbt_project_dir is not None:
        err_console.print("[red]Error:[/red] --specs-dir and --dbt-project-dir need --state.")
        raise typer.Exit(1)
    _VALID_CHANGE_TYPES = {"rename", "redefine", "drop", "type_change"}
    if change_type and change_type not in _VALID_CHANGE_TYPES:
        err_console.print(
            f"[red]Error:[/red] Invalid --change-type '{change_type}'. "
            f"Valid values: {', '.join(sorted(_VALID_CHANGE_TYPES))}"
        )
        raise typer.Exit(1)
    _run(_run_impact(entity=entity, change_type=change_type, as_json=as_json), command="impact")


async def _run_state_impact(
    state: str, specs_dir: str | None, dbt_project_dir: str | None, as_json: bool
) -> None:
    from puxti.core.bi_impact import ImpactReport, impact_against_state
    from puxti.core.bi_validate import config_error_report, resolve_dirs
    from puxti.workspace import load_workspace

    try:
        ws = load_workspace()
    except ValueError as exc:
        if not as_json:
            _load_workspace()  # prints the config error and exits 1
        findings = config_error_report(exc).findings
        report = ImpactReport(specs_dir=Path("."), state_dir=Path(state), findings=findings)
        console.print_json(json.dumps(report.to_dict()))
        raise typer.Exit(1)
    specs_path, dbt_path = resolve_dirs(ws, specs_dir, dbt_project_dir)
    report = impact_against_state(specs_path, dbt_path, Path(state))
    if as_json:
        console.print_json(json.dumps(report.to_dict()))
    else:
        _print_state_report(report)
    if not report.ok:
        raise typer.Exit(1)


def _print_state_report(report: "ImpactReport") -> None:
    from puxti.core.bi_validate import display_path

    if report.changed_models:
        console.print(Text(f"Changed dbt models (base: {report.state_dir})", style="bold"))
        for change in report.changed_models:
            console.print(Text(f"  {change.unique_id}: {', '.join(change.changes)}"))
    if report.assets:
        table = Table(show_lines=False)
        table.add_column("Asset", style="bold", no_wrap=True)
        table.add_column("Kind", style="dim", no_wrap=True)
        table.add_column("Status", no_wrap=True)
        table.add_column("Reason")
        for asset in report.assets:
            color = "red" if asset.status == "broken" else "yellow"
            table.add_row(
                Text(asset.name),
                Text(asset.kind),
                Text(asset.status, style=color),
                Text("\n".join(asset.reasons)),
            )
        console.print(table)
    for finding in report.findings:
        path = display_path(finding.path)
        where = f"{path}:{finding.line}" if finding.line else path
        line = Text(f"{where}: ")
        line.append(finding.severity, style="red" if finding.severity == "error" else "yellow")
        line.append(f": {finding.message}")
        console.print(line, highlight=False)
    counts = f"{report.broken} broken, {report.affected} affected, {report.warnings} warning(s)"
    if report.ok:
        mark = "[green]✓[/green]"
        if not report.assets:
            counts = f"No BI spec is affected ({report.warnings} warning(s))"
    else:
        mark = "[red]✗[/red]"
        if report.errors and not report.broken:
            counts = f"{report.errors} error(s), {report.warnings} warning(s)"
    console.print(f"{mark} {escape(counts)}", highlight=False)


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
        structural_deps = await graph.get_structural_dependents_with_depth(entity)

        # Merge into a single map keyed by entity ID.
        # An entity can appear in both semantic and structural — track both.
        dep_map: dict[str, dict] = {}
        for dep, hop in semantic_deps:
            entry = dep_map.setdefault(dep.id, {"entity": dep, "hop": hop, "rels": set()})
            entry["rels"].add("semantic")
            entry["hop"] = min(entry["hop"], hop)
        for dep, hop in structural_deps:
            entry = dep_map.setdefault(dep.id, {"entity": dep, "hop": hop, "rels": set()})
            entry["rels"].add("structural")
            entry["hop"] = min(entry["hop"], hop)

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
            definition.description if definition else "[dim]no definition — run puxti scan[/dim]"
        )
        header_lines = (
            f"[bold]Entity:[/bold]      {entity_obj.name}  [dim]({entity_obj.type.value})[/dim]\n"
            f"[bold]Definition:[/bold]  {def_text}"
        )
        if change_type:
            header_lines += f"\n[bold]Change type:[/bold] {change_type}"

        console.print(
            Panel(header_lines, title=f"[bold]Impact: {entity}[/bold]", border_style="blue")
        )

        if not rows:
            console.print(
                "[yellow]No dependents found.[/yellow] Nothing in the graph depends on this entity."
            )
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
            f"\n[dim]{len(rows)} dependent(s) — {sem_count} semantic, {str_count} structural[/dim]"
        )

        if change_type in ("rename", "drop", "type_change") and str_count:
            console.print(
                f"[yellow]⚠  {str_count} structural dependent(s) will need "
                f"updating for a {change_type}.[/yellow]"
            )
        if change_type in ("redefine", "drop", "type_change") and sem_count:
            console.print(
                f"[yellow]⚠  {sem_count} semantic dependent(s) may need "
                f"review for a {change_type}.[/yellow]"
            )

    finally:
        await graph.close()
