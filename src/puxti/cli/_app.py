"""Typer application objects and the root callback."""

import typer

from puxti import __version__

app = typer.Typer(
    name="puxti",
    help="Safe schema and semantic change propagation for data teams.",
    no_args_is_help=True,
)

telemetry_app = typer.Typer(
    name="telemetry",
    help="Manage anonymous usage telemetry.",
    no_args_is_help=True,
)
app.add_typer(telemetry_app, name="telemetry")

mcp_app = typer.Typer(
    name="mcp",
    help="MCP server for coding agents (Claude Code, Cursor, etc.).",
    no_args_is_help=True,
)
app.add_typer(mcp_app, name="mcp")


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
