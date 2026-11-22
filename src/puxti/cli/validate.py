"""`puxti validate` — check BI specs against each other and the dbt project, offline."""

import json
from typing import Optional

import typer
from rich.markup import escape
from rich.text import Text

from puxti.cli._app import app
from puxti.cli._shared import _load_workspace, _run, console
from puxti.core.bi_validate import (
    ValidationReport,
    config_error_report,
    display_path,
    resolve_dirs,
    validate,
)
from puxti.workspace import load_workspace


@app.command("validate")
def validate_command(
    specs_dir: Optional[str] = typer.Option(
        None,
        "--specs-dir",
        help="BI spec folder. Defaults to the bi connector's folder in .puxti.yml, "
        "else bi/ in the dbt project.",
    ),
    dbt_project_dir: Optional[str] = typer.Option(
        None,
        "--dbt-project-dir",
        help="dbt project whose target/manifest.json (and catalog.json) the specs are "
        "checked against. Defaults to .puxti.yml, then DBT_PROJECT_DIR.",
    ),
    as_json: bool = typer.Option(
        False, "--json", help="Output as JSON (same shape as the MCP validate_bi_specs tool)."
    ),
) -> None:
    """Check BI specs before they ship: schema, references, dbt models, columns, types.

    Reads the spec folder and the dbt project's target/manifest.json, plus
    target/catalog.json when present (`dbt docs generate`) for column names and
    types. No LLM calls, no Knowledge Graph, no BI tool. Exits 1 when any error is
    found, so it can gate CI; warnings (something that could not be checked, such
    as a column type with no catalog) print but exit 0.
    """
    _run(_run_validate(specs_dir, dbt_project_dir, as_json), command="validate")


async def _run_validate(specs_dir: str | None, dbt_project_dir: str | None, as_json: bool) -> None:
    try:
        ws = load_workspace()
    except ValueError as exc:
        if not as_json:
            _load_workspace()  # prints the config error and exits 1
        console.print_json(json.dumps(config_error_report(exc).to_dict()))
        raise typer.Exit(1)
    specs_path, dbt_path = resolve_dirs(ws, specs_dir, dbt_project_dir)
    report = validate(specs_path, dbt_path)
    if as_json:
        console.print_json(json.dumps(report.to_dict()))
    else:
        _print_report(report)
    if report.errors:
        raise typer.Exit(1)


def _print_report(report: ValidationReport) -> None:
    for finding in report.findings:
        path = display_path(finding.path)
        where = f"{path}:{finding.line}" if finding.line else path
        line = Text(f"{where}: ")
        line.append(finding.severity, style="red" if finding.severity == "error" else "yellow")
        line.append(f": {finding.message}")
        console.print(line, highlight=False)
    if report.errors:
        summary = f"[red]✗[/red] {report.errors} error(s), {report.warnings} warning(s)"
    else:
        summary = f"[green]✓[/green] Specs are valid ({report.warnings} warning(s))"
    console.print(f"{summary} in {escape(str(report.specs_dir))}", highlight=False)
