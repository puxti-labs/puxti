"""`puxti mcp serve` — MCP server for coding agents.
`puxti mcp init` — write the agent skill that teaches an agent to use the server.
"""

from pathlib import Path

import typer

from puxti.cli._app import mcp_app
from puxti.cli._shared import _run, console, err_console

# Where the Claude Code skill lands, relative to the current project.
_SKILL_PATH = Path(".claude") / "skills" / "puxti-analytics" / "SKILL.md"


@mcp_app.command("serve")
def mcp_serve() -> None:
    """Start the puxti MCP server (stdio transport).

    Exposes four read-only tools to any MCP-compatible agent:
      impact_of_change  — what depends on an entity and what breaks
      consumers         — direct structural consumers (1-hop lineage)
      definition_history — full version history of an entity's definition
      describe_entity   — type, connector, definition, and semantic edges

    \b
    Claude Code — add to your project's .claude/settings.json:
      {
        "mcpServers": {
          "puxti": { "command": "puxti", "args": ["mcp", "serve"] }
        }
      }

    Run `puxti scan` first to populate the Knowledge Graph, then `puxti mcp init`
    to give the agent the workflow skill that routes it to these tools.
    """
    from puxti.mcp_server import mcp as _mcp

    _mcp.run(transport="stdio")


@mcp_app.command("init")
def mcp_init(
    print_: bool = typer.Option(
        False,
        "--print",
        help="Print the skill markdown to stdout instead of writing a file "
        "(paste into Cursor rules, CLAUDE.md, or any other agent).",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Overwrite an existing skill file.",
    ),
) -> None:
    """Write the puxti agent skill that teaches an agent to answer truthfully.

    The MCP tools give an agent access to your knowledge graph; this skill tells it
    *when* to use them — check each entity's current definition before trusting a
    model, honor the latest version, and cite provenance in every metric answer.

    By default writes a Claude Code skill to .claude/skills/puxti-analytics/SKILL.md.
    Use --print to emit the same markdown to stdout for any other agent.

    Pair with `puxti mcp serve` (the server) and `puxti scan` (populates the graph).
    """
    _run(_run_init(print_=print_, force=force), command="mcp init")


async def _run_init(print_: bool, force: bool) -> None:
    from puxti.agent_skill import render_skill

    content = render_skill()

    if print_:
        # Raw markdown to stdout — NOT via the rich console, whose markup parser
        # would eat the `[brackets]` and `<placeholders>` in the skill text.
        typer.echo(content)
        return

    dest = Path.cwd() / _SKILL_PATH
    if dest.exists() and not force:
        err_console.print(
            f"[red]Error:[/red] {_SKILL_PATH} already exists.\n"
            "  Re-run with [bold]--force[/bold] to overwrite, or "
            "[bold]--print[/bold] to send it to stdout instead."
        )
        raise typer.Exit(1)

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(content, encoding="utf-8")

    console.print(
        f"[green]✓[/green] Wrote agent skill to [bold]{_SKILL_PATH}[/bold]\n"
        "\nNext:\n"
        "  1. [bold]puxti scan[/bold]       — populate the knowledge graph (if you haven't)\n"
        "  2. Add the MCP server to your agent — see [bold]puxti mcp serve --help[/bold]\n"
        "  3. Ask a metric question. The agent will check definitions before answering."
    )
