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
- Columns of any of those match on `<table key>#<column>`, e.g. `orders#amount`
  (BI specs reference the dbt columns a dataset or chart reads)
- dbt metrics match on `metric:<name>`

Ambiguous names (two entities claiming the same key) are dropped from the
index — an unresolved edge that a human can inspect beats a silently wrong
one.
"""

from puxti.models import Edge, Entity, EntityType

SQLREF_PREFIX = "sqlref."

_AMBIGUOUS = object()


def _own_keys(entity: Entity) -> list[str]:
    """The reference keys a table, view, model or dbt metric answers to."""
    meta = entity.metadata or {}
    connector, etype = entity.source_connector, entity.type
    if connector == "prisma" and etype == EntityType.TABLE:
        return [meta.get("db_table") or entity.name]
    if connector == "dbt" and etype == EntityType.METRIC:
        return [f"metric:{entity.name}"]
    if (connector, etype) in (
        ("sql_views", EntityType.VIEW),
        ("dbt", EntityType.MODEL),
        ("dbt", EntityType.TABLE),
    ):
        schema = meta.get("schema")
        return [entity.name] + ([f"{schema}.{entity.name}"] if schema else [])
    return []


def build_reference_index(entities: list[Entity]) -> dict[str, str]:
    """Map lowercase references ("users", "public.users", "users#email",
    "metric:arr") → entity ID."""
    index: dict[str, object] = {}

    def claim(key: str, entity_id: str) -> None:
        existing = index.get(key)
        if existing is None:
            index[key] = entity_id
        elif existing != entity_id:
            index[key] = _AMBIGUOUS

    keys_by_id: dict[str, list[str]] = {}
    for entity in entities:
        keys = [k.lower() for k in _own_keys(entity) if k]
        keys_by_id[entity.id] = keys
        for key in keys:
            claim(key, entity.id)

    # Columns, under each key of their table. A column's ID is its table's ID plus
    # the column name, so the parent is everything before the last dot. A table key
    # two entities share stays unresolved, so its columns do too: resolving
    # `orders#amount` to one of two `orders` would be a silently wrong edge.
    # Prisma columns go by their database name (@map), as SQL and BI specs see them.
    for entity in entities:
        if entity.type != EntityType.COLUMN:
            continue
        meta = entity.metadata or {}
        parent = meta.get("model_id") or entity.id.rsplit(".", 1)[0]
        column = (meta.get("db_column") or entity.name).lower()
        for key in keys_by_id.get(parent, []):
            if index.get(key) is not _AMBIGUOUS:
                claim(f"{key}#{column}", entity.id)

    return {k: v for k, v in index.items() if v is not _AMBIGUOUS}


def resolve_edges(edges: list[Edge], index: dict[str, str]) -> tuple[list[Edge], list[str]]:
    """Rewrite `sqlref.` edge targets that the index can resolve.

    Returns (edges, unresolved_raw_references). Unresolved edges are kept
    as-is — the placeholder ID in the graph is inert but visible, and a later
    re-scan with more connectors configured can resolve it.
    """
    # Owners indexed under a schema-qualified key know their schema. A qualified
    # reference that missed their exact key names another schema, so it must not
    # fall back to their bare key.
    schema_known = {owner for key, owner in index.items() if "." in key.split("#", 1)[0]}
    unresolved: list[str] = []
    for edge in edges:
        if not edge.to_entity_id.startswith(SQLREF_PREFIX):
            continue
        raw = edge.to_entity_id[len(SQLREF_PREFIX) :].lower()
        resolved = index.get(raw)
        table, hash_, column = raw.partition("#")
        if resolved is None and "." in table:
            # schema-qualified reference whose owner indexed only the bare name
            bare = index.get(table.rsplit(".", 1)[-1] + hash_ + column)
            resolved = bare if bare not in schema_known else None
        if resolved is not None:
            edge.metadata["resolved_from"] = raw
            edge.to_entity_id = resolved
        else:
            unresolved.append(raw)
    return edges, sorted(set(unresolved))
