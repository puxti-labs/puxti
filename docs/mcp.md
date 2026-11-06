# Use with Claude Code / Cursor (MCP)

Puxti ships an MCP server so coding agents can query your knowledge graph without leaving their context window.

**Add to Claude Code:**

```bash
claude mcp add puxti -- puxti mcp serve
```

Add `--scope project` to write it to the project's `.mcp.json` instead, so everyone working in the repo gets it.

**Add to Cursor** (`.cursor/mcp.json` in the project, or `~/.cursor/mcp.json` for all projects):

```json
{
  "mcpServers": {
    "puxti": {
      "command": "puxti",
      "args": ["mcp", "serve"]
    }
  }
}
```

The same `command` and `args` work for any other MCP client that launches stdio servers.

Once connected, five read-only tools are available:

| Tool | What it answers |
|---|---|
| `search_entities` | What is the exact ID of the entity called "average order value"? Matches name and ID, filters by type. |
| `impact_of_change` | Which entities depend on this one and would break? |
| `consumers` | Which models directly read from this entity? |
| `describe_entity` | What does this entity mean? What semantic edges does it have? |
| `definition_history` | How has the meaning of this entity evolved over time? |

For a *proposed* metric (one created with `puxti define` but not yet bound to a model), `describe_entity` returns the definition with `"bound": false`, and `impact_of_change` / `consumers` return a `"proposed"` marker with no dependents. The tools present a proposed metric as intent, never as a value to report.

The other four tools take an exact `entity_id`. Agents call `search_entities` first to find it instead of guessing the ID format.

Run `puxti scan` in your dbt project first to populate the graph (see [Commands](commands.md#step-1--bootstrap-the-knowledge-graph)).

## Teach the agent to use the tools

The tools give an agent *access* to the graph; they don't tell it *when* to consult the
graph or how to report what it found. Without that, an agent answers a metric question
from the first plausible model and returns a stale-but-confident number — the exact
failure that makes agentic analytics untrustworthy.

`puxti mcp init` writes an agent skill that closes the gap:

```bash
puxti mcp init            # writes .claude/skills/puxti-analytics/SKILL.md
puxti mcp init --print    # same markdown to stdout — paste into Cursor rules, CLAUDE.md, any agent
puxti mcp init --force    # overwrite an existing skill file, e.g. after upgrading puxti
```

The skill instructs the agent to check each entity's current definition and
`definition_history` before trusting a model, honor the latest definition (a model whose
SQL lags its definition is flagged as unreliable, not reported as fact), and end every
metric answer with a provenance footer citing the definition version, who authored it,
and when. It is puxti's small-scale version of the "skills" layer from Anthropic's
self-service analytics work.
