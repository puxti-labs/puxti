"""Cross-connector reference resolution.

Connectors are isolated — a SQL view that selects from `public.users` cannot
know that another connector owns that table. Such connectors emit lineage
edges whose target is a `sqlref.<name>` placeholder. This module resolves
those placeholders against the entities every configured connector extracted,
so the graph gets real edges instead of silently dangling ones.

Resolution is by database-level name:
- Prisma tables match on their @@map name (or model name when unmapped)
- dbt models and sources match on their name, and schema-qualified when the
  manifest provides a schema
- SQL views match on their schema-qualified and bare names

Ambiguous names (two entities claiming the same key) are dropped from the
index — an unresolved edge that a human can inspect beats a silently wrong
one.
"""

from puxti.models import Edge, Entity, EntityType

SQLREF_PREFIX = "sqlref."

_AMBIGUOUS = object()


def build_reference_index(entities: list[Entity]) -> dict[str, str]:
    """Map lowercase table references ("users", "public.users") → entity ID."""
    index: dict[str, object] = {}

    def claim(key: str | None, entity_id: str) -> None:
        if not key:
            return
        key = key.lower()
        existing = index.get(key)
        if existing is None:
            index[key] = entity_id
        elif existing != entity_id:
            index[key] = _AMBIGUOUS

    for entity in entities:
        meta = entity.metadata or {}
        if entity.source_connector == "prisma" and entity.type == EntityType.TABLE:
            claim(meta.get("db_table") or entity.name, entity.id)
        elif entity.source_connector == "sql_views" and entity.type == EntityType.VIEW:
            claim(entity.name, entity.id)
            schema = meta.get("schema")
            if schema:
                claim(f"{schema}.{entity.name}", entity.id)
        elif entity.source_connector == "dbt" and entity.type in (
            EntityType.MODEL, EntityType.TABLE
        ):
            claim(entity.name, entity.id)
            schema = meta.get("schema")
            if schema:
                claim(f"{schema}.{entity.name}", entity.id)

    return {k: v for k, v in index.items() if v is not _AMBIGUOUS}


def resolve_edges(edges: list[Edge], index: dict[str, str]) -> tuple[list[Edge], list[str]]:
    """Rewrite `sqlref.` edge targets that the index can resolve.

    Returns (edges, unresolved_raw_references). Unresolved edges are kept
    as-is — the placeholder ID in the graph is inert but visible, and a later
    re-scan with more connectors configured can resolve it.
    """
    unresolved: list[str] = []
    for edge in edges:
        if not edge.to_entity_id.startswith(SQLREF_PREFIX):
            continue
        raw = edge.to_entity_id[len(SQLREF_PREFIX):].lower()
        resolved = index.get(raw)
        if resolved is None and "." in raw:
            # schema-qualified reference whose owner indexed only the bare name
            resolved = index.get(raw.rsplit(".", 1)[-1])
        if resolved is not None:
            edge.metadata["resolved_from"] = raw
            edge.to_entity_id = resolved
        else:
            unresolved.append(raw)
    return edges, sorted(set(unresolved))
