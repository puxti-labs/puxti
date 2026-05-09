"""
Workspace-scoped Neo4j repository.

Safety guarantee: workspace_id is bound at construction time.
Query methods never accept workspace_id as a parameter — there is no
code path that can query across tenants.

Two isolation modes (controlled by NEO4J_ISOLATION env var):

  isolated  — one Neo4j database per workspace (prod / AuraDB Professional)
              session routes to database=workspace_id
              workspace_id on nodes is redundant but kept for schema consistency

  shared    — all workspaces share the default database (staging / AuraDB Free)
              workspace_id on every node IS the isolation boundary
              every MATCH pattern includes {workspace_id: $workspace_id}

The Cypher is identical in both modes. The session strategy is the only difference.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from neo4j import AsyncGraphDatabase, AsyncDriver, AsyncSession

from puxti.graph.models import Entity, Relationship, RelType

logger = logging.getLogger(__name__)

_NEO4J_URI = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
_NEO4J_USERNAME = os.environ.get("NEO4J_USERNAME", "neo4j")
_NEO4J_PASSWORD = os.environ.get("NEO4J_PASSWORD", "")
_NEO4J_ISOLATION = os.environ.get("NEO4J_ISOLATION", "shared")  # "isolated" | "shared"


class WorkspaceGraph:
    """
    All graph operations for a single workspace.

    Constructed by GraphDriver.workspace() — never instantiate directly.
    workspace_id is private and injected into every query automatically.
    """

    def __init__(
        self,
        driver: AsyncDriver,
        workspace_id: str,
        *,
        isolated: bool,
    ) -> None:
        self._driver = driver
        self._workspace_id = workspace_id
        self._isolated = isolated

    # ── session routing ───────────────────────────────────────────────────────

    def _session(self) -> AsyncSession:
        """Route to the correct database based on isolation mode."""
        if self._isolated:
            return self._driver.session(database=self._workspace_id)
        return self._driver.session()

    def _p(self, params: dict[str, Any]) -> dict[str, Any]:
        """Inject workspace_id into every query's parameter dict."""
        return {"workspace_id": self._workspace_id, **params}

    # ── entities ──────────────────────────────────────────────────────────────

    async def upsert_entity(self, entity: Entity) -> None:
        """Create or update an entity node. Scoped to this workspace."""
        async with self._session() as s:
            await s.run(
                "MERGE (e:Entity {name: $name, workspace_id: $workspace_id}) "
                "SET e.type = $type, e.description = $description",
                self._p({
                    "name": entity.name,
                    "type": entity.type,
                    "description": entity.description,
                }),
            )

    async def get_entity(self, name: str) -> Entity | None:
        """Fetch one entity by name. Returns None if not found in this workspace."""
        async with self._session() as s:
            result = await s.run(
                "MATCH (e:Entity {name: $name, workspace_id: $workspace_id}) RETURN e",
                self._p({"name": name}),
            )
            record = await result.single()
        if record is None:
            return None
        node = record["e"]
        return Entity(
            name=node["name"],
            type=node["type"],
            description=node.get("description", ""),
        )

    async def list_entities(self) -> list[Entity]:
        """List all entities in this workspace."""
        async with self._session() as s:
            result = await s.run(
                "MATCH (e:Entity {workspace_id: $workspace_id}) RETURN e ORDER BY e.name",
                self._p({}),
            )
            records = await result.data()
        return [
            Entity(
                name=r["e"]["name"],
                type=r["e"]["type"],
                description=r["e"].get("description", ""),
            )
            for r in records
        ]

    async def delete_entity(self, name: str) -> None:
        """Delete an entity and all its relationships. Scoped to this workspace."""
        async with self._session() as s:
            await s.run(
                "MATCH (e:Entity {name: $name, workspace_id: $workspace_id}) "
                "DETACH DELETE e",
                self._p({"name": name}),
            )

    # ── relationships ─────────────────────────────────────────────────────────

    async def relate(self, rel: Relationship) -> None:
        """
        Create a relationship between two entities in this workspace.

        rel_type is taken from the RelType enum — never interpolated from
        user input. The MATCH patterns on both sides include workspace_id,
        so a relationship across workspaces is structurally impossible.
        """
        rel_type_str = rel.rel_type.value  # validated StrEnum — safe to interpolate
        async with self._session() as s:
            await s.run(
                f"MATCH (a:Entity {{name: $from_name, workspace_id: $workspace_id}}) "
                f"MATCH (b:Entity {{name: $to_name, workspace_id: $workspace_id}}) "
                f"MERGE (a)-[:{rel_type_str}]->(b)",
                self._p({"from_name": rel.from_name, "to_name": rel.to_name}),
            )

    async def list_relationships(self) -> list[Relationship]:
        """List all relationships between entities in this workspace."""
        async with self._session() as s:
            result = await s.run(
                "MATCH (a:Entity {workspace_id: $workspace_id})-[r]->(b:Entity {workspace_id: $workspace_id}) "
                "RETURN a.name AS from_name, b.name AS to_name, type(r) AS rel_type",
                self._p({}),
            )
            records = await result.data()
        return [
            Relationship(
                from_name=r["from_name"],
                to_name=r["to_name"],
                rel_type=RelType(r["rel_type"]),
            )
            for r in records
        ]

    # ── workspace lifecycle ───────────────────────────────────────────────────

    async def drop(self) -> None:
        """
        Delete all data for this workspace.

        In isolated mode: drops the entire database.
        In shared mode: deletes all nodes with this workspace_id.

        Called when a workspace is deleted. Irreversible.
        """
        if self._isolated:
            async with self._driver.session(database="system") as s:
                await s.run(f"DROP DATABASE `{self._workspace_id}` IF EXISTS")
        else:
            async with self._session() as s:
                await s.run(
                    "MATCH (n {workspace_id: $workspace_id}) DETACH DELETE n",
                    self._p({}),
                )
        logger.info("Dropped workspace graph: %s", self._workspace_id)


class GraphDriver:
    """
    Manages the Neo4j driver lifecycle and vends WorkspaceGraph instances.

    Usage (FastAPI lifespan):
        graph_driver = GraphDriver()
        await graph_driver.connect()          # non-fatal — logs warning if unavailable
        graph = graph_driver.workspace(workspace_id)
        await graph.list_entities()
        await graph_driver.close()
    """

    def __init__(self) -> None:
        self._driver: AsyncDriver | None = None
        self._isolated: bool = _NEO4J_ISOLATION == "isolated"

    @property
    def connected(self) -> bool:
        return self._driver is not None

    async def connect(self) -> None:
        """
        Connect to Neo4j. Non-fatal: logs a warning if unavailable so the
        API can still start without graph capabilities (local dev, CI).
        """
        try:
            self._driver = AsyncGraphDatabase.driver(
                _NEO4J_URI,
                auth=(_NEO4J_USERNAME, _NEO4J_PASSWORD),
            )
            await self._driver.verify_connectivity()
            logger.info(
                "Neo4j connected — isolation mode: %s",
                "isolated" if self._isolated else "shared",
            )
        except Exception:
            logger.warning(
                "Neo4j unavailable at %s — graph features disabled. "
                "Set NEO4J_URI / NEO4J_USERNAME / NEO4J_PASSWORD to enable.",
                _NEO4J_URI,
            )
            self._driver = None

    async def close(self) -> None:
        if self._driver:
            await self._driver.close()

    def workspace(self, workspace_id: str) -> WorkspaceGraph:
        """
        Return a WorkspaceGraph bound to workspace_id.

        This is the only way to get a WorkspaceGraph. Workspace ID is
        locked in — all queries on the returned object are scoped to it.
        Raises RuntimeError if Neo4j is not connected.
        """
        if self._driver is None:
            raise RuntimeError("Neo4j not connected — graph features unavailable")
        return WorkspaceGraph(self._driver, workspace_id, isolated=self._isolated)

    async def provision_workspace(self, workspace_id: str) -> None:
        """
        Set up the Neo4j side of a new workspace.

        Isolated mode: creates a dedicated database.
        Shared mode: no-op (workspace is defined by nodes written later).
        No-op if Neo4j is not connected.
        """
        if not self.connected:
            return
        if not self._isolated:
            return
        assert self._driver is not None
        async with self._driver.session(database="system") as s:
            await s.run(f"CREATE DATABASE `{workspace_id}` IF NOT EXISTS")
        logger.info("Provisioned isolated Neo4j database: %s", workspace_id)
