"""SQLite-backed Knowledge Graph."""
from __future__ import annotations

import json
import logging
from pathlib import Path

import aiosqlite

from puxti.models import ChangeEvent, CorrectionEvent, Definition, Edge, Entity, SemanticEdge

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path.home() / ".puxti" / "graph.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS entities (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT NOT NULL,
    source_connector TEXT NOT NULL,
    project TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entities_name_connector ON entities(name, source_connector);
CREATE INDEX IF NOT EXISTS idx_entities_project ON entities(project);

CREATE TABLE IF NOT EXISTS lineage_edges (
    from_id TEXT NOT NULL,
    to_id TEXT NOT NULL,
    connector TEXT NOT NULL,
    type TEXT NOT NULL,
    PRIMARY KEY (from_id, to_id, connector)
);
CREATE INDEX IF NOT EXISTS idx_lineage_to ON lineage_edges(to_id);

CREATE TABLE IF NOT EXISTS semantic_edges (
    from_id TEXT NOT NULL,
    to_id TEXT NOT NULL,
    type TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (from_id, to_id, type)
);
CREATE INDEX IF NOT EXISTS idx_semantic_to ON semantic_edges(to_id);

CREATE TABLE IF NOT EXISTS definitions (
    id TEXT PRIMARY KEY,
    entity_id TEXT NOT NULL,
    description TEXT NOT NULL,
    version INTEGER NOT NULL,
    created_by TEXT NOT NULL,
    change_event_id TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_definitions_entity_version ON definitions(entity_id, version);

CREATE TABLE IF NOT EXISTS change_events (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    source_entity_id TEXT NOT NULL,
    change TEXT NOT NULL,
    semantic_context TEXT NOT NULL DEFAULT '',
    declared_by TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    detected_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS correction_events (
    id TEXT PRIMARY KEY,
    entity_id TEXT NOT NULL,
    old_definition_id TEXT NOT NULL,
    new_definition_id TEXT NOT NULL,
    classified_as TEXT NOT NULL,
    change_event_id TEXT,
    created_at TEXT NOT NULL
);
"""


def _to_entity(row: aiosqlite.Row) -> Entity:
    return Entity(
        id=row["id"],
        name=row["name"],
        type=row["type"],
        source_connector=row["source_connector"],
        project=row["project"] or "",
    )


def _to_semantic_edge(row: aiosqlite.Row) -> SemanticEdge:
    return SemanticEdge(
        from_entity_id=row["from_id"],
        to_entity_id=row["to_id"],
        type=row["type"],
        description=row["description"] or "",
        created_by=row["created_by"] or "scan",
    )


class KnowledgeGraph:
    """SQLite-backed Knowledge Graph. Drop-in replacement for the Neo4j implementation."""

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path or DEFAULT_DB_PATH
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        if str(self._db_path) != ":memory:":
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self._db_path))
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(_SCHEMA)
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.commit()
        logger.info("Knowledge Graph connected: %s", self._db_path)

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    # ── Entities ──────────────────────────────────────────────────────────────

    async def upsert_entity(self, entity: Entity) -> None:
        await self._db.execute(
            """
            INSERT INTO entities (id, name, type, source_connector, project, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name, type=excluded.type,
                source_connector=excluded.source_connector,
                project=excluded.project, updated_at=excluded.updated_at
            """,
            (entity.id, entity.name, entity.type.value, entity.source_connector,
             entity.project, entity.created_at.isoformat(), entity.updated_at.isoformat()),
        )
        await self._db.commit()

    async def upsert_entity_by_name(self, entity: Entity) -> Entity:
        """Create or update an entity keyed on (name, source_connector). Returns stored entity."""
        async with self._db.execute(
            "SELECT id FROM entities WHERE name=? AND source_connector=?",
            (entity.name, entity.source_connector),
        ) as cur:
            row = await cur.fetchone()

        if row:
            existing_id = row["id"]
            await self._db.execute(
                "UPDATE entities SET type=?, project=?, updated_at=? WHERE id=?",
                (entity.type.value, entity.project, entity.updated_at.isoformat(), existing_id),
            )
            await self._db.commit()
            return Entity(
                id=existing_id,
                name=entity.name,
                type=entity.type,
                source_connector=entity.source_connector,
                project=entity.project,
            )

        await self._db.execute(
            """
            INSERT INTO entities (id, name, type, source_connector, project, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (entity.id, entity.name, entity.type.value, entity.source_connector,
             entity.project, entity.created_at.isoformat(), entity.updated_at.isoformat()),
        )
        await self._db.commit()
        return entity

    async def get_entity_by_name(self, name: str, connector: str) -> Entity | None:
        async with self._db.execute(
            "SELECT * FROM entities WHERE name=? AND source_connector=?",
            (name, connector),
        ) as cur:
            row = await cur.fetchone()
        return _to_entity(row) if row else None

    async def get_entity_by_id(self, entity_id: str) -> Entity | None:
        async with self._db.execute(
            "SELECT * FROM entities WHERE id=?", (entity_id,)
        ) as cur:
            row = await cur.fetchone()
        return _to_entity(row) if row else None

    async def get_all_entity_ids(self) -> list[str]:
        async with self._db.execute("SELECT id FROM entities ORDER BY id") as cur:
            return [row["id"] for row in await cur.fetchall()]

    async def filter_existing_entity_ids(self, entity_ids: list[str]) -> list[str]:
        if not entity_ids:
            return []
        placeholders = ",".join("?" * len(entity_ids))
        async with self._db.execute(
            f"SELECT id FROM entities WHERE id IN ({placeholders})", entity_ids
        ) as cur:
            return [row["id"] for row in await cur.fetchall()]

    async def get_all_entities_with_definitions(
        self,
    ) -> list[tuple[Entity, Definition | None]]:
        async with self._db.execute(
            """
            SELECT e.*,
                   d.id AS def_id, d.description AS def_desc, d.version AS def_ver,
                   d.created_by AS def_created_by, d.change_event_id AS def_change_event_id,
                   d.created_at AS def_created_at
            FROM entities e
            LEFT JOIN definitions d ON d.id = (
                SELECT id FROM definitions WHERE entity_id=e.id ORDER BY version DESC LIMIT 1
            )
            ORDER BY e.name
            """
        ) as cur:
            rows = await cur.fetchall()

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

    # ── Structural lineage edges ───────────────────────────────────────────────

    async def upsert_edge(self, edge: Edge) -> None:
        await self._db.execute(
            """
            INSERT INTO lineage_edges (from_id, to_id, connector, type)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(from_id, to_id, connector) DO UPDATE SET type=excluded.type
            """,
            (edge.from_entity_id, edge.to_entity_id, edge.connector, edge.type.value),
        )
        await self._db.commit()

    async def get_structural_dependents(self, entity_id: str) -> list[Entity]:
        """Return direct structural dependents (single-hop LINEAGE). Falls back to name lookup."""
        async with self._db.execute(
            """
            SELECT DISTINCT e.* FROM lineage_edges le
            JOIN entities e ON e.id = le.from_id
            WHERE le.to_id=?
            """,
            (entity_id,),
        ) as cur:
            rows = await cur.fetchall()

        if rows:
            return [_to_entity(r) for r in rows]

        # Fallback: resolve by model name (e.g. "model.jaffle_shop.orders.amount" → "orders")
        parts = entity_id.rsplit(".", 1)
        model_name = parts[0].rsplit(".", 1)[-1] if len(parts) == 2 else entity_id
        if not model_name:
            return []

        async with self._db.execute(
            """
            SELECT DISTINCT e.* FROM lineage_edges le
            JOIN entities e ON e.id = le.from_id
            JOIN entities src ON src.id = le.to_id
            WHERE src.name=? AND src.type='model'
            """,
            (model_name,),
        ) as cur:
            rows = await cur.fetchall()
        return [_to_entity(r) for r in rows]

    async def get_structural_ancestors(self, entity_id: str) -> list[tuple[Entity, int]]:
        """Return upstream model ancestors with hop depth via recursive CTE."""
        async with self._db.execute(
            """
            WITH RECURSIVE ancs(id, depth) AS (
                SELECT to_id, 1 FROM lineage_edges WHERE from_id=?
                UNION ALL
                SELECT le.to_id, ancs.depth + 1
                FROM lineage_edges le JOIN ancs ON le.from_id = ancs.id
                WHERE ancs.depth < 20
            )
            SELECT e.*, MIN(ancs.depth) AS depth
            FROM ancs JOIN entities e ON e.id = ancs.id
            WHERE e.type = 'model'
            GROUP BY ancs.id
            ORDER BY MIN(ancs.depth)
            """,
            (entity_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [(_to_entity(r), r["depth"]) for r in rows]

    # ── Semantic graph ────────────────────────────────────────────────────────

    async def upsert_semantic_edge(self, edge: SemanticEdge) -> None:
        await self._db.execute(
            """
            INSERT INTO semantic_edges (from_id, to_id, type, description, created_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(from_id, to_id, type) DO UPDATE SET
                description=excluded.description,
                created_by=excluded.created_by,
                created_at=excluded.created_at
            """,
            (edge.from_entity_id, edge.to_entity_id, edge.type.value,
             edge.description, edge.created_by, edge.created_at.isoformat()),
        )
        await self._db.commit()

    async def get_all_semantic_edges(self) -> list[SemanticEdge]:
        async with self._db.execute(
            """
            SELECT se.from_id, se.to_id, se.type, se.description, se.created_by, se.created_at
            FROM semantic_edges se
            JOIN entities a ON a.id = se.from_id
            JOIN entities b ON b.id = se.to_id
            ORDER BY a.name, b.name
            """
        ) as cur:
            rows = await cur.fetchall()
        return [_to_semantic_edge(r) for r in rows]

    async def get_entity_semantic_edges(self, entity_id: str) -> list[SemanticEdge]:
        async with self._db.execute(
            """
            SELECT from_id, to_id, type, description, created_by, created_at
            FROM semantic_edges WHERE from_id=? OR to_id=?
            """,
            (entity_id, entity_id),
        ) as cur:
            rows = await cur.fetchall()
        return [_to_semantic_edge(r) for r in rows]

    async def get_semantic_dependents_with_depth(
        self, entity_id: str
    ) -> list[tuple[Entity, int]]:
        """Return entities with SEMANTIC paths pointing to entity_id, with min hop depth."""
        async with self._db.execute(
            """
            WITH RECURSIVE deps(id, depth) AS (
                SELECT from_id, 1 FROM semantic_edges WHERE to_id=?
                UNION ALL
                SELECT se.from_id, deps.depth + 1
                FROM semantic_edges se JOIN deps ON se.to_id = deps.id
                WHERE deps.depth < 10
            )
            SELECT e.*, MIN(deps.depth) AS depth
            FROM deps JOIN entities e ON e.id = deps.id
            GROUP BY deps.id
            ORDER BY MIN(deps.depth)
            """,
            (entity_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [(_to_entity(r), r["depth"]) for r in rows]

    async def get_semantic_dependents(self, entity_id: str) -> list[Entity]:
        async with self._db.execute(
            """
            WITH RECURSIVE deps(id) AS (
                SELECT from_id FROM semantic_edges WHERE to_id=?
                UNION
                SELECT se.from_id FROM semantic_edges se JOIN deps ON se.to_id = deps.id
            )
            SELECT DISTINCT e.* FROM deps JOIN entities e ON e.id = deps.id
            """,
            (entity_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [_to_entity(r) for r in rows]

    async def get_feeds_producers(self, entity_id: str) -> list[Entity]:
        ids_to_check = [entity_id]
        if "." in entity_id:
            parent_id = entity_id.rsplit(".", 1)[0]
            if parent_id != entity_id:
                ids_to_check.append(parent_id)

        placeholders = ",".join("?" * len(ids_to_check))
        seen: set[str] = set()
        entities: list[Entity] = []
        async with self._db.execute(
            f"""
            SELECT DISTINCT e.* FROM semantic_edges se
            JOIN entities e ON e.id = se.from_id
            WHERE se.to_id IN ({placeholders}) AND se.type='feeds'
            """,
            ids_to_check,
        ) as cur:
            for row in await cur.fetchall():
                if row["id"] not in seen:
                    seen.add(row["id"])
                    entities.append(_to_entity(row))
        return entities

    async def delete_semantic_edge(self, from_entity_id: str, to_entity_id: str) -> None:
        await self._db.execute(
            "DELETE FROM semantic_edges WHERE from_id=? AND to_id=?",
            (from_entity_id, to_entity_id),
        )
        await self._db.commit()

    # ── Definitions ───────────────────────────────────────────────────────────

    async def upsert_definition(self, definition: Definition) -> None:
        await self._db.execute(
            """
            INSERT INTO definitions
                (id, entity_id, description, version, created_by, change_event_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                description=excluded.description,
                version=excluded.version,
                created_by=excluded.created_by
            """,
            (definition.id, definition.entity_id, definition.description,
             definition.version, definition.created_by, definition.change_event_id,
             definition.created_at.isoformat()),
        )
        await self._db.commit()

    async def get_latest_definition(self, entity_id: str) -> Definition | None:
        async with self._db.execute(
            "SELECT * FROM definitions WHERE entity_id=? ORDER BY version DESC LIMIT 1",
            (entity_id,),
        ) as cur:
            row = await cur.fetchone()
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

    # ── Change and correction events ──────────────────────────────────────────

    async def save_change_event(self, event: ChangeEvent) -> None:
        await self._db.execute(
            """
            INSERT INTO change_events
                (id, type, source_entity_id, change, semantic_context, declared_by, status, detected_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                status=excluded.status,
                semantic_context=excluded.semantic_context
            """,
            (event.id, event.type.value, event.source_entity_id,
             json.dumps(event.change), event.semantic_context or "",
             event.declared_by or "", event.status.value,
             event.detected_at.isoformat()),
        )
        await self._db.commit()

    async def write_correction(
        self, event: CorrectionEvent, updated_edges: list[SemanticEdge]
    ) -> None:
        for from_id, to_id in event.edges_removed:
            await self._db.execute(
                "DELETE FROM semantic_edges WHERE from_id=? AND to_id=?",
                (from_id, to_id),
            )

        for edge in updated_edges:
            await self._db.execute(
                """
                INSERT INTO semantic_edges (from_id, to_id, type, description, created_by, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(from_id, to_id, type) DO UPDATE SET
                    description=excluded.description,
                    created_by=excluded.created_by,
                    created_at=excluded.created_at
                """,
                (edge.from_entity_id, edge.to_entity_id, edge.type.value,
                 edge.description, edge.created_by, edge.created_at.isoformat()),
            )

        await self._db.execute(
            """
            INSERT INTO correction_events
                (id, entity_id, old_definition_id, new_definition_id,
                 classified_as, change_event_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (event.id, event.entity_id, event.old_definition_id,
             event.new_definition_id, event.classified_as,
             event.change_event_id, event.created_at.isoformat()),
        )
        await self._db.commit()

    # ── Project management ────────────────────────────────────────────────────

    async def get_projects(self) -> list[str]:
        async with self._db.execute(
            "SELECT DISTINCT project FROM entities WHERE project IS NOT NULL AND project != '' ORDER BY project"
        ) as cur:
            return [row["project"] for row in await cur.fetchall()]

    async def purge_project(self, project: str) -> int:
        async with self._db.execute(
            "SELECT id FROM entities WHERE project=?", (project,)
        ) as cur:
            ids = [row["id"] for row in await cur.fetchall()]

        count = len(ids)
        if ids:
            ph = ",".join("?" * len(ids))
            await self._db.execute(f"DELETE FROM definitions WHERE entity_id IN ({ph})", ids)
            await self._db.execute(
                f"DELETE FROM semantic_edges WHERE from_id IN ({ph}) OR to_id IN ({ph})",
                ids + ids,
            )
            await self._db.execute(
                f"DELETE FROM lineage_edges WHERE from_id IN ({ph}) OR to_id IN ({ph})",
                ids + ids,
            )
            await self._db.execute("DELETE FROM entities WHERE project=?", (project,))
            await self._db.commit()
        return count

    async def purge_all(self) -> int:
        async with self._db.execute("SELECT COUNT(*) AS n FROM entities") as cur:
            row = await cur.fetchone()
        count = row["n"] if row else 0

        for table in ("correction_events", "change_events", "definitions",
                      "semantic_edges", "lineage_edges", "entities"):
            await self._db.execute(f"DELETE FROM {table}")
        await self._db.commit()
        return count
