"""`puxti mcp serve` — MCP server for coding agents."""

from puxti.cli._app import mcp_app


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

    Run `puxti scan` first to populate the Knowledge Graph.
    """
    from puxti.mcp_server import mcp as _mcp
    _mcp.run(transport="stdio")
