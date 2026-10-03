"""Postgres-backed Knowledge Graph.

An opt-in alternative to the default SQLite store, selected when ``DATABASE_URL``
names a Postgres DSN (see :func:`puxti.core.graph.KnowledgeGraph`). It implements
the same :class:`~puxti.core.graph.GraphStore` interface and reuses the SQLite
schema and row readers verbatim, so the two backends stay behaviourally
identical — the parametrized repository test suite runs against both.

Requires the ``asyncpg`` driver (``pip install 'puxti[postgres]'``), imported
lazily in :meth:`PostgresKnowledgeGraph.connect` so it is only needed when this
backend is actually used.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

from puxti.core.graph import _SCHEMA, _to_entity, _to_semantic_edge
from puxti.models import (
    ChangeEvent,
    CorrectionEvent,
    Definition,
    Edge,
    Entity,
    EntityStatus,
    EntityType,
    SemanticEdge,
)

logger = logging.getLogger(__name__)


class PostgresKnowledgeGraph:
    """Postgres-backed Knowledge Graph (implements ``GraphStore``).

    Timestamps are stored as ISO-8601 ``TEXT`` and the schema is identical to the
    SQLite backend, so stored values round-trip the same way through both. A
    single connection mirrors the SQLite single-connection model; the
    multi-statement operations are wrapped in a transaction for atomicity.
    """

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._conn = None  # asyncpg.Connection, set in connect()

    async def connect(self) -> None:
        try:
            import asyncpg
        except ModuleNotFoundError as exc:  # pragma: no cover - import guard
            raise RuntimeError(
                "The Postgres backend requires the 'asyncpg' driver. Install it with:\n"
                "    pip install 'puxti[postgres]'"
            ) from exc

        self._conn = await asyncpg.connect(self._dsn)
        # _SCHEMA is CREATE TABLE/INDEX IF NOT EXISTS and parameter-free, so it
        # runs as a single multi-statement execute.
        await self._conn.execute(_SCHEMA)
        await self._migrate()
        # No DSN in the log line — it may carry a password.
        logger.info("Knowledge Graph connected: postgres")

    async def _migrate(self) -> None:
        """Idempotent, forward-only migrations for graphs created by earlier
        versions. ``_SCHEMA`` uses CREATE TABLE IF NOT EXISTS, so a column added
        to an existing table must be applied here, not in the schema."""
        await self._conn.execute(
            "ALTER TABLE entities ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'bound'"
        )
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_entities_status ON entities(status)"
        )

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    # ── Entities ──────────────────────────────────────────────────────────────

    async def upsert_entity(self, entity: Entity) -> None:
        await self._conn.execute(
            """
            INSERT INTO entities
                (id, name, type, source_connector, project, status, created_at, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            ON CONFLICT (id) DO UPDATE SET
                name = EXCLUDED.name, type = EXCLUDED.type,
                source_connector = EXCLUDED.source_connector,
                project = EXCLUDED.project, status = EXCLUDED.status,
                updated_at = EXCLUDED.updated_at
            """,
            entity.id, entity.name, entity.type.value, entity.source_connector,
            entity.project, entity.status.value,
            entity.created_at.isoformat(), entity.updated_at.isoformat(),
        )

    async def upsert_entity_by_name(self, entity: Entity) -> Entity:
        """Create or update an entity keyed on (name, source_connector). Returns stored entity."""
        row = await self._conn.fetchrow(
            "SELECT id FROM entities WHERE name = $1 AND source_connector = $2",
            entity.name, entity.source_connector,
        )

        if row:
            existing_id = row["id"]
            await self._conn.execute(
                "UPDATE entities SET type = $1, project = $2, status = $3, updated_at = $4 "
                "WHERE id = $5",
                entity.type.value, entity.project, entity.status.value,
                entity.updated_at.isoformat(), existing_id,
            )
            return Entity(
                id=existing_id,
                name=entity.name,
                type=entity.type,
                source_connector=entity.source_connector,
                project=entity.project,
                status=entity.status,
            )

        await self._conn.execute(
            """
            INSERT INTO entities
                (id, name, type, source_connector, project, status, created_at, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            """,
            entity.id, entity.name, entity.type.value, entity.source_connector,
            entity.project, entity.status.value,
            entity.created_at.isoformat(), entity.updated_at.isoformat(),
        )
        return entity

    async def get_entity_by_name(self, name: str, connector: str) -> Entity | None:
        row = await self._conn.fetchrow(
            "SELECT * FROM entities WHERE name = $1 AND source_connector = $2",
            name, connector,
        )
        return _to_entity(row) if row else None

    async def get_entity_by_id(self, entity_id: str) -> Entity | None:
        row = await self._conn.fetchrow(
            "SELECT * FROM entities WHERE id = $1", entity_id
        )
        return _to_entity(row) if row else None

    async def get_all_entity_ids(self) -> list[str]:
        rows = await self._conn.fetch("SELECT id FROM entities ORDER BY id")
        return [row["id"] for row in rows]

    async def filter_existing_entity_ids(self, entity_ids: list[str]) -> list[str]:
        if not entity_ids:
            return []
        rows = await self._conn.fetch(
            "SELECT id FROM entities WHERE id = ANY($1::text[])", entity_ids
        )
        return [row["id"] for row in rows]

    async def get_all_entities_with_definitions(
        self,
    ) -> list[tuple[Entity, Definition | None]]:
        rows = await self._conn.fetch(
            """
            SELECT e.*,
                   d.id AS def_id, d.description AS def_desc, d.version AS def_ver,
                   d.created_by AS def_created_by, d.change_event_id AS def_change_event_id,
                   d.created_at AS def_created_at
            FROM entities e
            LEFT JOIN definitions d ON d.id = (
                SELECT id FROM definitions WHERE entity_id = e.id ORDER BY version DESC LIMIT 1
            )
            ORDER BY e.name
            """
        )

        result = []
        for row in rows:
            entity = _to_entity(row)
            definition = None
            if row["def_id"]:
                definition = Definition(
                    id=row["def_id"],
                    entity_id=row["id"],
                    description=row["def_desc"],
                    version=row["def_ver"],
                    created_by=row["def_created_by"],
                    change_event_id=row["def_change_event_id"],
                )
            result.append((entity, definition))
        return result

    # ── Proposed entities ──────────────────────────────────────────────────────

    async def get_proposed_entities(self) -> list[Entity]:
        """Return entities defined ahead of code (status='proposed'), not yet bound."""
        rows = await self._conn.fetch(
            "SELECT * FROM entities WHERE status = $1 ORDER BY name",
            EntityStatus.PROPOSED.value,
        )
        return [_to_entity(r) for r in rows]

    async def get_reconcile_candidates(self) -> list[Entity]:
        """Bound model/view/table entities a proposed metric could bind to."""
        types = [EntityType.MODEL.value, EntityType.VIEW.value, EntityType.TABLE.value]
        rows = await self._conn.fetch(
            "SELECT * FROM entities WHERE status = $1 AND type = ANY($2::text[])",
            EntityStatus.BOUND.value, types,
        )
        return [_to_entity(r) for r in rows]

    async def bind_entity(self, proposed_id: str, real_id: str) -> None:
        """Bind a proposed entity to a real one: carry its latest definition onto
        the real entity as a new version, re-point its semantic edges, then remove
        the placeholder. The whole bind runs in one transaction so a mid-way
        failure can't leave the target defined while the placeholder survives."""
        async with self._conn.transaction():
            proposed_def = await self.get_latest_definition(proposed_id)
            if proposed_def is not None:
                existing = await self.get_latest_definition(real_id)
                new_def = Definition(
                    entity_id=real_id,
                    description=proposed_def.description,
                    version=(existing.version + 1) if existing else 1,
                    created_by="user",
                )
                await self._conn.execute(
                    """
                    INSERT INTO definitions
                        (id, entity_id, description, version,
                         created_by, change_event_id, created_at)
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                    """,
                    new_def.id, new_def.entity_id, new_def.description, new_def.version,
                    new_def.created_by, new_def.change_event_id, new_def.created_at.isoformat(),
                )

            # Re-point semantic edges from the placeholder onto the real entity,
            # dropping self-loops and de-duping against edges already on the target.
            edges = await self._conn.fetch(
                "SELECT from_id, to_id, type, description, created_by, created_at "
                "FROM semantic_edges WHERE from_id = $1 OR to_id = $1",
                proposed_id,
            )
            for e in edges:
                new_from = real_id if e["from_id"] == proposed_id else e["from_id"]
                new_to = real_id if e["to_id"] == proposed_id else e["to_id"]
                if new_from == new_to:
                    continue
                await self._conn.execute(
                    """
                    INSERT INTO semantic_edges
                        (from_id, to_id, type, description, created_by, created_at)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    ON CONFLICT (from_id, to_id, type) DO NOTHING
                    """,
                    new_from, new_to, e["type"], e["description"], e["created_by"], e["created_at"],
                )

            await self._conn.execute(
                "DELETE FROM semantic_edges WHERE from_id = $1 OR to_id = $1", proposed_id
            )
            await self._conn.execute(
                "DELETE FROM definitions WHERE entity_id = $1", proposed_id
            )
            await self._conn.execute("DELETE FROM entities WHERE id = $1", proposed_id)

    # ── Structural lineage edges ───────────────────────────────────────────────

    async def upsert_edge(self, edge: Edge) -> None:
        await self._conn.execute(
            """
            INSERT INTO lineage_edges (from_id, to_id, connector, type)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (from_id, to_id, connector) DO UPDATE SET type = EXCLUDED.type
            """,
            edge.from_entity_id, edge.to_entity_id, edge.connector, edge.type.value,
        )

    async def get_structural_dependents(self, entity_id: str) -> list[Entity]:
        """Return direct structural dependents (single-hop LINEAGE). Falls back to name lookup."""
        rows = await self._conn.fetch(
            """
            SELECT DISTINCT e.* FROM lineage_edges le
            JOIN entities e ON e.id = le.from_id
            WHERE le.to_id = $1
            """,
            entity_id,
        )

        if rows:
            return [_to_entity(r) for r in rows]

        # Fallback: resolve by model name. Column IDs put the model name
        # second-to-last ("model.jaffle_shop.orders.amount" → "orders");
        # model IDs put it last ("model.jaffle_shop.orders" → "orders").
        # Try the column interpretation first, then the model one.
        parts = entity_id.split(".")
        candidates = ([parts[-2]] if len(parts) >= 2 else []) + [parts[-1]]
        for model_name in candidates:
            if not model_name:
                continue
            rows = await self._conn.fetch(
                """
                SELECT DISTINCT e.* FROM lineage_edges le
                JOIN entities e ON e.id = le.from_id
                JOIN entities src ON src.id = le.to_id
                WHERE src.name = $1 AND src.type = 'model'
                """,
                model_name,
            )
            if rows:
                return [_to_entity(r) for r in rows]
        return []

    async def get_structural_ancestors(self, entity_id: str) -> list[tuple[Entity, int]]:
        """Return upstream model ancestors with hop depth via recursive CTE."""
        # GROUP BY the entity primary key: Postgres then allows selecting the
        # other e.* columns (functionally dependent on the PK) alongside MIN().
        rows = await self._conn.fetch(
            """
            WITH RECURSIVE ancs(id, depth) AS (
                SELECT to_id, 1 FROM lineage_edges WHERE from_id = $1
                UNION ALL
                SELECT le.to_id, ancs.depth + 1
                FROM lineage_edges le JOIN ancs ON le.from_id = ancs.id
                WHERE ancs.depth < 20
            )
            SELECT e.*, MIN(ancs.depth) AS depth
            FROM ancs JOIN entities e ON e.id = ancs.id
            WHERE e.type = 'model'
            GROUP BY e.id
            ORDER BY MIN(ancs.depth)
            """,
            entity_id,
        )
        return [(_to_entity(r), r["depth"]) for r in rows]

    # ── Semantic graph ────────────────────────────────────────────────────────

    async def upsert_semantic_edge(self, edge: SemanticEdge) -> None:
        await self._conn.execute(
            """
            INSERT INTO semantic_edges (from_id, to_id, type, description, created_by, created_at)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (from_id, to_id, type) DO UPDATE SET
                description = EXCLUDED.description,
                created_by = EXCLUDED.created_by,
                created_at = EXCLUDED.created_at
            """,
            edge.from_entity_id, edge.to_entity_id, edge.type.value,
            edge.description, edge.created_by, edge.created_at.isoformat(),
        )

    async def get_all_semantic_edges(self) -> list[SemanticEdge]:
        rows = await self._conn.fetch(
            """
            SELECT se.from_id, se.to_id, se.type, se.description, se.created_by, se.created_at
            FROM semantic_edges se
            JOIN entities a ON a.id = se.from_id
            JOIN entities b ON b.id = se.to_id
            ORDER BY a.name, b.name
            """
        )
        return [_to_semantic_edge(r) for r in rows]

    async def get_all_lineage_edges(self) -> list[Edge]:
        """All structural lineage edges whose endpoints are real entities (dangling
        `sqlref.` placeholder targets are excluded via the joins)."""
        rows = await self._conn.fetch(
            """
            SELECT le.from_id, le.to_id, le.connector, le.type
            FROM lineage_edges le
            JOIN entities a ON a.id = le.from_id
            JOIN entities b ON b.id = le.to_id
            ORDER BY a.name, b.name
            """
        )
        return [
            Edge(
                from_entity_id=r["from_id"],
                to_entity_id=r["to_id"],
                type=r["type"],
                connector=r["connector"],
            )
            for r in rows
        ]

    async def get_entity_semantic_edges(self, entity_id: str) -> list[SemanticEdge]:
        rows = await self._conn.fetch(
            """
            SELECT from_id, to_id, type, description, created_by, created_at
            FROM semantic_edges WHERE from_id = $1 OR to_id = $1
            """,
            entity_id,
        )
        return [_to_semantic_edge(r) for r in rows]

    async def get_semantic_dependents_with_depth(
        self, entity_id: str
    ) -> list[tuple[Entity, int]]:
        """Return entities with SEMANTIC paths pointing to entity_id, with min hop depth."""
        rows = await self._conn.fetch(
            """
            WITH RECURSIVE deps(id, depth) AS (
                SELECT from_id, 1 FROM semantic_edges WHERE to_id = $1
                UNION ALL
                SELECT se.from_id, deps.depth + 1
                FROM semantic_edges se JOIN deps ON se.to_id = deps.id
                WHERE deps.depth < 10
            )
            SELECT e.*, MIN(deps.depth) AS depth
            FROM deps JOIN entities e ON e.id = deps.id
            GROUP BY e.id
            ORDER BY MIN(deps.depth)
            """,
            entity_id,
        )
        return [(_to_entity(r), r["depth"]) for r in rows]

    async def get_semantic_dependents(self, entity_id: str) -> list[Entity]:
        rows = await self._conn.fetch(
            """
            WITH RECURSIVE deps(id) AS (
                SELECT from_id FROM semantic_edges WHERE to_id = $1
                UNION
                SELECT se.from_id FROM semantic_edges se JOIN deps ON se.to_id = deps.id
            )
            SELECT DISTINCT e.* FROM deps JOIN entities e ON e.id = deps.id
            """,
            entity_id,
        )
        return [_to_entity(r) for r in rows]

    async def get_feeds_producers(self, entity_id: str) -> list[Entity]:
        ids_to_check = [entity_id]
        if "." in entity_id:
            parent_id = entity_id.rsplit(".", 1)[0]
            if parent_id != entity_id:
                ids_to_check.append(parent_id)

        seen: set[str] = set()
        entities: list[Entity] = []
        rows = await self._conn.fetch(
            """
            SELECT DISTINCT e.* FROM semantic_edges se
            JOIN entities e ON e.id = se.from_id
            WHERE se.to_id = ANY($1::text[]) AND se.type = 'feeds'
            """,
            ids_to_check,
        )
        for row in rows:
            if row["id"] not in seen:
                seen.add(row["id"])
                entities.append(_to_entity(row))
        return entities

    async def delete_semantic_edge(self, from_entity_id: str, to_entity_id: str) -> None:
        await self._conn.execute(
            "DELETE FROM semantic_edges WHERE from_id = $1 AND to_id = $2",
            from_entity_id, to_entity_id,
        )

    # ── Definitions ───────────────────────────────────────────────────────────

    async def upsert_definition(self, definition: Definition) -> None:
        await self._conn.execute(
            """
            INSERT INTO definitions
                (id, entity_id, description, version, created_by, change_event_id, created_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (id) DO UPDATE SET
                description = EXCLUDED.description,
                version = EXCLUDED.version,
                created_by = EXCLUDED.created_by
            """,
            definition.id, definition.entity_id, definition.description,
            definition.version, definition.created_by, definition.change_event_id,
            definition.created_at.isoformat(),
        )

    async def get_latest_definition(self, entity_id: str) -> Definition | None:
        row = await self._conn.fetchrow(
            "SELECT * FROM definitions WHERE entity_id = $1 ORDER BY version DESC LIMIT 1",
            entity_id,
        )
        if not row:
            return None
        return Definition(
            id=row["id"],
            entity_id=row["entity_id"],
            description=row["description"],
            version=row["version"],
            created_by=row["created_by"],
            change_event_id=row["change_event_id"],
        )

    async def get_definition_history(self, entity_id: str) -> list[Definition]:
        """Return all definition versions for an entity, oldest first."""
        rows = await self._conn.fetch(
            "SELECT * FROM definitions WHERE entity_id = $1 ORDER BY version ASC",
            entity_id,
        )
        return [
            Definition(
                id=row["id"],
                entity_id=row["entity_id"],
                description=row["description"],
                version=row["version"],
                created_by=row["created_by"],
                change_event_id=row["change_event_id"],
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in rows
        ]

    # ── Change and correction events ──────────────────────────────────────────

    async def save_change_event(self, event: ChangeEvent) -> None:
        await self._conn.execute(
            """
            INSERT INTO change_events
                (id, type, source_entity_id, change, semantic_context,
                 declared_by, status, detected_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            ON CONFLICT (id) DO UPDATE SET
                status = EXCLUDED.status,
                semantic_context = EXCLUDED.semantic_context
            """,
            event.id, event.type.value, event.source_entity_id,
            json.dumps(event.change), event.semantic_context or "",
            event.declared_by or "", event.status.value,
            event.detected_at.isoformat(),
        )

    async def write_correction(
        self, event: CorrectionEvent, updated_edges: list[SemanticEdge]
    ) -> None:
        async with self._conn.transaction():
            for from_id, to_id in event.edges_removed:
                await self._conn.execute(
                    "DELETE FROM semantic_edges WHERE from_id = $1 AND to_id = $2",
                    from_id, to_id,
                )

            for edge in updated_edges:
                await self._conn.execute(
                    """
                    INSERT INTO semantic_edges
                        (from_id, to_id, type, description, created_by, created_at)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    ON CONFLICT (from_id, to_id, type) DO UPDATE SET
                        description = EXCLUDED.description,
                        created_by = EXCLUDED.created_by,
                        created_at = EXCLUDED.created_at
                    """,
                    edge.from_entity_id, edge.to_entity_id, edge.type.value,
                    edge.description, edge.created_by, edge.created_at.isoformat(),
                )

            await self._conn.execute(
                """
                INSERT INTO correction_events
                    (id, entity_id, old_definition_id, new_definition_id,
                     classified_as, change_event_id, created_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                """,
                event.id, event.entity_id, event.old_definition_id,
                event.new_definition_id, event.classified_as,
                event.change_event_id, event.created_at.isoformat(),
            )

    # ── Project management ────────────────────────────────────────────────────

    async def get_projects(self) -> list[str]:
        rows = await self._conn.fetch(
            "SELECT DISTINCT project FROM entities "
            "WHERE project IS NOT NULL AND project != '' ORDER BY project"
        )
        return [row["project"] for row in rows]

    async def purge_project(self, project: str) -> int:
        async with self._conn.transaction():
            rows = await self._conn.fetch(
                "SELECT id FROM entities WHERE project = $1", project
            )
            ids = [row["id"] for row in rows]

            if ids:
                await self._conn.execute(
                    "DELETE FROM definitions WHERE entity_id = ANY($1::text[])", ids
                )
                await self._conn.execute(
                    "DELETE FROM semantic_edges "
                    "WHERE from_id = ANY($1::text[]) OR to_id = ANY($1::text[])",
                    ids,
                )
                await self._conn.execute(
                    "DELETE FROM lineage_edges "
                    "WHERE from_id = ANY($1::text[]) OR to_id = ANY($1::text[])",
                    ids,
                )
                # Audit records referencing the purged entities go too — matching
                # purge_all, which wipes both event tables.
                await self._conn.execute(
                    "DELETE FROM change_events WHERE source_entity_id = ANY($1::text[])", ids
                )
                await self._conn.execute(
                    "DELETE FROM correction_events WHERE entity_id = ANY($1::text[])", ids
                )
                await self._conn.execute(
                    "DELETE FROM entities WHERE project = $1", project
                )
        return len(ids)

    async def purge_all(self) -> int:
        async with self._conn.transaction():
            row = await self._conn.fetchrow("SELECT COUNT(*) AS n FROM entities")
            count = row["n"] if row else 0

            for table in ("correction_events", "change_events", "definitions",
                          "semantic_edges", "lineage_edges", "entities"):
                await self._conn.execute(f"DELETE FROM {table}")
        return count
