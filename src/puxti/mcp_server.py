"""MCP server exposing puxti's Knowledge Graph to coding agents.

Exposes five read-only tools — all read directly from the Knowledge Graph, no LLM calls:
  search_entities   — find entity IDs by name, so agents don't guess them
  impact_of_change  — what depends on an entity and what breaks
  consumers         — direct structural consumers (1-hop lineage)
  definition_history — full version history of an entity's definition
  describe_entity   — type, connector, definition, and semantic edges
"""
from __future__ import annotations

import asyncio
import json

from mcp.server.mcpserver import MCPServer

from puxti.core.graph import GraphStore, KnowledgeGraph
from puxti.models import EntityStatus, EntityType

mcp = MCPServer(
    "puxti",
    instructions=(
        "Puxti exposes your dbt project's Knowledge Graph. "
        "The other tools take an exact entity_id: call search_entities first to find it "
        "instead of guessing the ID format. "
        "Use impact_of_change or consumers to understand blast radius before making changes. "
        "Use describe_entity to look up what a model or source means. "
        "Run `puxti scan` in the project first to populate the graph."
    ),
)

SEARCH_MAX_LIMIT = 100

_graph: GraphStore | None = None
_graph_lock: asyncio.Lock | None = None


async def _get_lock() -> asyncio.Lock:
    global _graph_lock
    if _graph_lock is None:
        _graph_lock = asyncio.Lock()
    return _graph_lock


async def _graph_connect() -> GraphStore:
    global _graph
    lock = await _get_lock()
    async with lock:
        if _graph is None:
            _graph = KnowledgeGraph()
            await _graph.connect()
    return _graph


@mcp.tool()
async def search_entities(query: str, type: str | None = None, limit: int = 20) -> str:
    """Find entities by name or ID. Call this first when you don't have an exact entity_id.

    Matches every word of the query case-insensitively against entity names and
    IDs ("average order" finds average_order_value). Exact name matches rank
    first. Optionally filter by type: table, column, model, view, dag, task,
    dashboard, metric. limit is capped at 100.

    Returns JSON: {query, type,
    results: [{entity_id, name, type, project, status, definition}], total_count,
    truncated} where definition is the first line of the current definition, or
    null, and truncated is true when more entities matched than limit allowed:
    narrow the query, pass a type, or raise limit.
    """
    entity_type = None
    if type is not None:
        try:
            entity_type = EntityType(type.lower())
        except ValueError:
            valid = ", ".join(t.value for t in EntityType)
            return json.dumps({"error": f"Unknown type '{type}'. Valid types: {valid}."})

    limit = max(1, min(limit, SEARCH_MAX_LIMIT))
    graph = await _graph_connect()
    # One extra row tells a full page apart from a cut-off one.
    hits = await graph.search_entities(query, entity_type=entity_type, limit=limit + 1)
    truncated = len(hits) > limit
    hits = hits[:limit]
    return json.dumps({
        "query": query,
        "type": entity_type.value if entity_type else None,
        "results": [
            {
                "entity_id": entity.id,
                "name": entity.name,
                "type": entity.type.value,
                "project": entity.project,
                "status": entity.status.value,
                "definition": (
                    definition.description.strip().splitlines()[0]
                    if definition and definition.description.strip() else None
                ),
            }
            for entity, definition in hits
        ],
        "total_count": len(hits),
        "truncated": truncated,
    })


@mcp.tool()
async def impact_of_change(entity_id: str, change_type: str | None = None) -> str:
    """Show which entities depend on a given entity and would be affected by a change.

    Returns semantic dependents (concept-level) and structural dependents
    (SQL lineage) with hop depth. Optionally scope risk annotation with
    change_type: rename (structural risk), redefine (semantic risk),
    drop or type_change (both).

    Returns JSON: {entity_id, change_type,
    dependents: [{entity_id, name, type, hop, relationship}], total_count}
    """
    graph = await _graph_connect()
    entity = await graph.get_entity_by_id(entity_id)
    if not entity:
        return json.dumps({"error": f"Entity '{entity_id}' not found. Run `puxti scan` first."})

    if entity.status == EntityStatus.PROPOSED:
        return json.dumps({
            "entity_id": entity_id,
            "change_type": change_type,
            "proposed": True,
            "note": "This metric is proposed (defined but not yet implemented by a model). "
                    "It has no structural dependents until it is bound to a real entity.",
            "dependents": [],
            "total_count": 0,
        })

    semantic_deps = await graph.get_semantic_dependents_with_depth(entity_id)
    structural_deps = await graph.get_structural_dependents(entity_id)

    dep_map: dict[str, dict] = {}
    for dep, hop in semantic_deps:
        entry = dep_map.setdefault(dep.id, {"entity": dep, "hop": hop, "rels": set()})
        entry["rels"].add("semantic")
        entry["hop"] = min(entry["hop"], hop)
    for dep in structural_deps:
        entry = dep_map.setdefault(dep.id, {"entity": dep, "hop": 1, "rels": set()})
        entry["rels"].add("structural")

    rows = sorted(dep_map.values(), key=lambda r: (r["hop"], r["entity"].name))
    return json.dumps({
        "entity_id": entity_id,
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
    })


@mcp.tool()
async def consumers(entity_id: str) -> str:
    """Return the direct structural consumers of an entity.

    These are models that directly reference this source or model in SQL
    lineage (1-hop structural dependents). Use this to know who reads from
    an entity before you change it.

    Returns JSON: {entity_id, consumers: [{entity_id, name, type, project}], total_count}
    """
    graph = await _graph_connect()
    entity = await graph.get_entity_by_id(entity_id)
    if not entity:
        return json.dumps({"error": f"Entity '{entity_id}' not found. Run `puxti scan` first."})

    if entity.status == EntityStatus.PROPOSED:
        return json.dumps({
            "entity_id": entity_id,
            "proposed": True,
            "note": "This metric is proposed (not yet implemented). No entity reads from it yet.",
            "consumers": [],
            "total_count": 0,
        })

    deps = await graph.get_structural_dependents(entity_id)
    return json.dumps({
        "entity_id": entity_id,
        "consumers": [
            {"entity_id": d.id, "name": d.name, "type": d.type.value, "project": d.project}
            for d in deps
        ],
        "total_count": len(deps),
    })


@mcp.tool()
async def definition_history(entity_id: str) -> str:
    """Return the full version history of semantic definitions for an entity.

    Shows how the meaning of an entity has evolved over time — each version
    records the definition text, who wrote it (user or llm), and when.

    Returns JSON: {entity_id,
    history: [{version, description, created_by, created_at}], total_versions}
    """
    graph = await _graph_connect()
    entity = await graph.get_entity_by_id(entity_id)
    if not entity:
        return json.dumps({"error": f"Entity '{entity_id}' not found. Run `puxti scan` first."})

    history = await graph.get_definition_history(entity_id)
    return json.dumps({
        "entity_id": entity_id,
        "history": [
            {
                "version": d.version,
                "description": d.description,
                "created_by": d.created_by,
                "created_at": d.created_at.isoformat(),
            }
            for d in history
        ],
        "total_versions": len(history),
    })


@mcp.tool()
async def describe_entity(entity_id: str) -> str:
    """Return full details for a single entity.

    Shows the entity type, source connector, project, current semantic
    definition, and all incoming/outgoing semantic edges. Use this to
    understand what an entity means and how it relates to others.

    Returns JSON: {entity_id, name, type, connector, project, definition, semantic_edges}
    """
    graph = await _graph_connect()
    entity = await graph.get_entity_by_id(entity_id)
    if not entity:
        return json.dumps({"error": f"Entity '{entity_id}' not found. Run `puxti scan` first."})

    definition = await graph.get_latest_definition(entity_id)
    edges = await graph.get_entity_semantic_edges(entity_id)

    is_bound = entity.status == EntityStatus.BOUND
    return json.dumps({
        "entity_id": entity_id,
        "name": entity.name,
        "type": entity.type.value,
        "connector": entity.source_connector,
        "project": entity.project,
        "status": entity.status.value,
        "bound": is_bound,
        "note": None if is_bound else (
            "PROPOSED: this metric is defined as intent but not yet implemented by any "
            "model. The definition describes what it should mean. Do NOT report a value "
            "for it as fact. Cite it as a proposed definition only."
        ),
        "definition": {
            "description": definition.description,
            "version": definition.version,
            "created_by": definition.created_by,
            "created_at": definition.created_at.isoformat(),
        } if definition else None,
        "semantic_edges": [
            {
                "from_entity_id": e.from_entity_id,
                "to_entity_id": e.to_entity_id,
                "type": e.type.value,
                "description": e.description,
                "direction": "outgoing" if e.from_entity_id == entity_id else "incoming",
            }
            for e in edges
        ],
    })
