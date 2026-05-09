from neo4j import AsyncDriver, AsyncGraphDatabase, NotificationMinimumSeverity

from puxti.models import ChangeEvent, CorrectionEvent, Definition, Edge, Entity, SemanticEdge
from puxti.settings import settings


class KnowledgeGraph:
    """Interface to the Neo4j Knowledge Graph.

    Two layers:
    - Structural lineage: Entity nodes + Edge relationships (populated by connectors)
    - Semantic graph: Definition nodes + SemanticEdge relationships (populated by capture)
    """

    def __init__(self) -> None:
        self._driver: AsyncDriver | None = None

    async def connect(self) -> None:
        self._driver = AsyncGraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_username, settings.neo4j_password),
            # Suppress schema-missing warnings (01N42) that fire on an empty
            # database when MATCH queries reference labels not yet created.
            # These are expected and not actionable.
            notifications_min_severity=NotificationMinimumSeverity.OFF,
        )
        await self._driver.verify_connectivity()
        await self.create_indexes()

    async def close(self) -> None:
        if self._driver:
            await self._driver.close()

    # ── Schema ────────────────────────────────────────────────────────────────

    async def create_indexes(self) -> None:
        constraints = [
            "CREATE CONSTRAINT entity_id IF NOT EXISTS "
            "FOR (e:Entity) REQUIRE e.id IS UNIQUE",
            "CREATE CONSTRAINT definition_id IF NOT EXISTS "
            "FOR (d:Definition) REQUIRE d.id IS UNIQUE",
            "CREATE CONSTRAINT change_event_id IF NOT EXISTS "
            "FOR (c:ChangeEvent) REQUIRE c.id IS UNIQUE",
            "CREATE INDEX entity_name IF NOT EXISTS FOR (e:Entity) ON (e.name)",
        ]
        async with self._driver.session() as session:
            for query in constraints:
                await session.run(query)

    # ── Entities ──────────────────────────────────────────────────────────────

    async def upsert_entity(self, entity: Entity) -> None:
        async with self._driver.session() as session:
            await session.run(
                """
                MERGE (e:Entity {id: $id})
                ON CREATE SET e.created_at = $created_at
                SET e.name = $name,
                    e.type = $type,
                    e.source_connector = $source_connector,
                    e.project = $project,
                    e.metadata = $metadata,
                    e.updated_at = $updated_at
                """,
                id=entity.id,
                name=entity.name,
                type=entity.type.value,
                source_connector=entity.source_connector,
                project=entity.project,
                metadata=str(entity.metadata),
                created_at=entity.created_at.isoformat(),
                updated_at=entity.updated_at.isoformat(),
            )

    async def upsert_entity_by_name(self, entity: Entity) -> Entity:
        """Create or update an entity keyed on (name, source_connector).

        Unlike upsert_entity (which keys on uuid), this is safe to call for
        cross-system entities where we have no canonical id from the source system.
        Returns the entity as stored, with its id.
        """
        async with self._driver.session() as session:
            result = await session.run(
                """
                MERGE (e:Entity {name: $name, source_connector: $source_connector})
                ON CREATE SET e.id = $id, e.created_at = $created_at
                SET e.type = $type,
                    e.project = $project,
                    e.updated_at = $updated_at
                RETURN e
                """,
                name=entity.name,
                source_connector=entity.source_connector,
                id=entity.id,
                type=entity.type.value,
                project=entity.project,
                created_at=entity.created_at.isoformat(),
                updated_at=entity.updated_at.isoformat(),
            )
            record = await result.single()
        node = record["e"]
        return Entity(
            id=node["id"],
            name=node["name"],
            type=node["type"],
            source_connector=node["source_connector"],
            project=node.get("project", ""),
        )

    async def get_entity_by_name(self, name: str, connector: str) -> Entity | None:
        async with self._driver.session() as session:
            result = await session.run(
                "MATCH (e:Entity {name: $name, source_connector: $connector}) RETURN e",
                name=name,
                connector=connector,
            )
            record = await result.single()
            if not record:
                return None
            node = record["e"]
            return Entity(
                id=node["id"],
                name=node["name"],
                type=node["type"],
                source_connector=node["source_connector"],
                project=node.get("project", ""),
            )

    # ── Structural lineage edges ───────────────────────────────────────────────

    async def upsert_edge(self, edge: Edge) -> None:
        async with self._driver.session() as session:
            await session.run(
                """
                MATCH (from:Entity {id: $from_id})
                MATCH (to:Entity {id: $to_id})
                MERGE (from)-[r:LINEAGE {connector: $connector}]->(to)
                SET r.type = $type
                """,
                from_id=edge.from_entity_id,
                to_id=edge.to_entity_id,
                type=edge.type.value,
                connector=edge.connector,
            )

    # ── Semantic graph ────────────────────────────────────────────────────────

    async def upsert_definition(self, definition: Definition) -> None:
        async with self._driver.session() as session:
            await session.run(
                """
                MERGE (d:Definition {id: $id})
                SET d.entity_id = $entity_id,
                    d.description = $description,
                    d.version = $version,
                    d.created_by = $created_by,
                    d.change_event_id = $change_event_id,
                    d.created_at = $created_at
                WITH d
                MATCH (e:Entity {id: $entity_id})
                MERGE (e)-[:HAS_DEFINITION]->(d)
                """,
                id=definition.id,
                entity_id=definition.entity_id,
                description=definition.description,
                version=definition.version,
                created_by=definition.created_by,
                change_event_id=definition.change_event_id,
                created_at=definition.created_at.isoformat(),
            )

    async def get_latest_definition(self, entity_id: str) -> Definition | None:
        async with self._driver.session() as session:
            result = await session.run(
                """
                MATCH (e:Entity {id: $entity_id})-[:HAS_DEFINITION]->(d:Definition)
                RETURN d ORDER BY d.version DESC LIMIT 1
                """,
                entity_id=entity_id,
            )
            record = await result.single()
            if not record:
                return None
            node = record["d"]
            return Definition(
                id=node["id"],
                entity_id=node["entity_id"],
                description=node["description"],
                version=node["version"],
                created_by=node["created_by"],
                change_event_id=node.get("change_event_id"),
            )

    async def upsert_semantic_edge(self, edge: SemanticEdge) -> None:
        """Link concepts in the semantic graph.
        Example: revenue DERIVED_FROM sales."""
        async with self._driver.session() as session:
            await session.run(
                """
                MATCH (from:Entity {id: $from_id})
                MATCH (to:Entity {id: $to_id})
                MERGE (from)-[r:SEMANTIC {type: $type}]->(to)
                SET r.description = $description,
                    r.created_by = $created_by,
                    r.created_at = $created_at
                """,
                from_id=edge.from_entity_id,
                to_id=edge.to_entity_id,
                type=edge.type.value,
                description=edge.description,
                created_by=edge.created_by,
                created_at=edge.created_at.isoformat(),
            )

    async def get_entity_by_id(self, entity_id: str) -> Entity | None:
        """Fetch a single entity by its ID."""
        async with self._driver.session() as session:
            result = await session.run(
                "MATCH (e:Entity {id: $id}) RETURN e",
                id=entity_id,
            )
            record = await result.single()
            if not record:
                return None
            node = record["e"]
            return Entity(
                id=node["id"],
                name=node["name"],
                type=node["type"],
                source_connector=node["source_connector"],
                project=node.get("project", ""),
            )

    async def get_all_entities_with_definitions(
        self,
    ) -> list[tuple[Entity, "Definition | None"]]:
        """Return all entities paired with their latest definition (or None)."""
        async with self._driver.session() as session:
            result = await session.run(
                """
                MATCH (e:Entity)
                OPTIONAL MATCH (e)-[:HAS_DEFINITION]->(d:Definition)
                WITH e, d ORDER BY d.version DESC
                WITH e, collect(d)[0] AS latest_def
                RETURN e, latest_def
                ORDER BY e.name
                """
            )
            pairs = []
            async for record in result:
                node = record["e"]
                entity = Entity(
                    id=node["id"],
                    name=node["name"],
                    type=node["type"],
                    source_connector=node["source_connector"],
                    project=node.get("project", ""),
                )
                def_node = record["latest_def"]
                definition = None
                if def_node:
                    from puxti.models import Definition
                    definition = Definition(
                        id=def_node["id"],
                        entity_id=def_node["entity_id"],
                        description=def_node["description"],
                        version=def_node["version"],
                        created_by=def_node["created_by"],
                        change_event_id=def_node.get("change_event_id"),
                    )
                pairs.append((entity, definition))
            return pairs

    async def get_all_semantic_edges(self) -> list["SemanticEdge"]:
        """Return all semantic edges in the graph."""
        async with self._driver.session() as session:
            result = await session.run(
                """
                MATCH (a:Entity)-[r:SEMANTIC]->(b:Entity)
                RETURN a.id AS from_id, b.id AS to_id,
                       r.type AS type, r.description AS description,
                       r.created_by AS created_by
                ORDER BY a.name, b.name
                """
            )
            edges = []
            async for record in result:
                edges.append(SemanticEdge(
                    from_entity_id=record["from_id"],
                    to_entity_id=record["to_id"],
                    type=record["type"],
                    description=record["description"],
                    created_by=record["created_by"] or "scan",
                ))
            return edges

    async def get_semantic_dependents_with_depth(
        self, entity_id: str
    ) -> list[tuple[Entity, int]]:
        """Return all semantically dependent entities with their hop depth.

        Depth 1 = directly depends on this entity.
        Depth 2 = depends on a depth-1 dependent. Etc.
        Returns the minimum depth for each entity (shortest path).
        Ordered by depth ascending.
        """
        async with self._driver.session() as session:
            result = await session.run(
                """
                MATCH p = (source:Entity {id: $entity_id})<-[:SEMANTIC*1..]-(dependent:Entity)
                WITH last(nodes(p)) AS dep_node, min(length(p)) AS depth
                RETURN dep_node AS dependent, depth
                ORDER BY depth
                """,
                entity_id=entity_id,
            )
            pairs: list[tuple[Entity, int]] = []
            async for record in result:
                node = record["dependent"]
                pairs.append((
                    Entity(
                        id=node["id"],
                        name=node["name"],
                        type=node["type"],
                        source_connector=node["source_connector"],
                    ),
                    record["depth"],
                ))
            return pairs

    async def get_semantic_dependents(self, entity_id: str) -> list[Entity]:
        """Return all entities semantically dependent on this one.
        Used for Case 3 — definition redefinition impact traversal."""
        async with self._driver.session() as session:
            result = await session.run(
                """
                MATCH (source:Entity {id: $entity_id})<-[:SEMANTIC*1..]-(dependent:Entity)
                RETURN DISTINCT dependent
                """,
                entity_id=entity_id,
            )
            entities = []
            async for record in result:
                node = record["dependent"]
                entities.append(Entity(
                    id=node["id"],
                    name=node["name"],
                    type=node["type"],
                    source_connector=node["source_connector"],
                ))
            return entities

    async def get_feeds_producers(self, entity_id: str) -> list[Entity]:
        """Return entities with a FEEDS edge pointing to entity_id.

        Checks both the entity itself and its parent (for column-level IDs like
        source.clariva.raw_opportunities.amount → source.clariva.raw_opportunities).
        """
        ids_to_check = [entity_id]
        if "." in entity_id:
            parent_id = entity_id.rsplit(".", 1)[0]
            if parent_id != entity_id:
                ids_to_check.append(parent_id)

        async with self._driver.session() as session:
            result = await session.run(
                """
                UNWIND $ids AS target_id
                MATCH (producer:Entity)-[:SEMANTIC {type: "feeds"}]->(target:Entity {id: target_id})
                RETURN DISTINCT producer
                """,
                ids=ids_to_check,
            )
            entities: list[Entity] = []
            seen: set[str] = set()
            async for record in result:
                node = record["producer"]
                if node["id"] not in seen:
                    seen.add(node["id"])
                    entities.append(Entity(
                        id=node["id"],
                        name=node["name"],
                        type=node["type"],
                        source_connector=node["source_connector"],
                        project=node.get("project", ""),
                    ))
            return entities

    async def get_all_entity_ids(self) -> list[str]:
        """Return all entity IDs currently in the graph."""
        async with self._driver.session() as session:
            result = await session.run("MATCH (e:Entity) RETURN e.id AS id ORDER BY e.id")
            ids = []
            async for record in result:
                ids.append(record["id"])
            return ids

    async def filter_existing_entity_ids(self, entity_ids: list[str]) -> list[str]:
        """Return only the entity IDs from the list that exist in the graph."""
        if not entity_ids:
            return []
        async with self._driver.session() as session:
            result = await session.run(
                "UNWIND $ids AS id MATCH (e:Entity {id: id}) RETURN e.id AS id",
                ids=entity_ids,
            )
            existing = []
            async for record in result:
                existing.append(record["id"])
            return existing

    async def get_structural_ancestors(self, entity_id: str) -> list[tuple[Entity, int]]:
        """Return structural ancestors (upstream models) with hop depth from the entity.

        Depth 1 = directly upstream. Used to propagate new attributes from the
        ingestion boundary back through the staging/mart chain.
        Only returns model-type entities (not sources/columns).
        """
        async with self._driver.session() as session:
            result = await session.run(
                """
                MATCH p = (source:Entity {id: $entity_id})-[:LINEAGE*1..]->(ancestor:Entity)
                WHERE ancestor.type = 'model'
                WITH last(nodes(p)) AS anc_node, min(length(p)) AS depth
                RETURN anc_node AS ancestor, depth
                ORDER BY depth
                """,
                entity_id=entity_id,
            )
            pairs: list[tuple[Entity, int]] = []
            async for record in result:
                node = record["ancestor"]
                pairs.append((
                    Entity(
                        id=node["id"],
                        name=node["name"],
                        type=node["type"],
                        source_connector=node["source_connector"],
                    ),
                    record["depth"],
                ))
            return pairs

    async def get_structural_dependents(self, entity_id: str) -> list[Entity]:
        """Return direct (single-hop) structural dependents of this entity.

        Only returns models that directly reference this entity in lineage.
        Used as context for the LLM in capture — giving the LLM transitive
        dependents causes scope over-expansion, since those models may not
        reference the changed column at all.

        When the exact entity_id is not in the graph (e.g. a column entity
        that was never extracted because the model has no schema YAML), falls
        back to looking up the parent model by name. This ensures the LLM gets
        correct structural context even for non-standard entity ID formats.
        """
        async with self._driver.session() as session:
            result = await session.run(
                """
                MATCH (dependent:Entity)-[:LINEAGE]->(source:Entity {id: $entity_id})
                RETURN DISTINCT dependent
                """,
                entity_id=entity_id,
            )
            entities = []
            async for record in result:
                node = record["dependent"]
                entities.append(Entity(
                    id=node["id"],
                    name=node["name"],
                    type=node["type"],
                    source_connector=node["source_connector"],
                ))

            if entities:
                return entities

            # Fallback: entity_id not in graph — try resolving by model name.
            # Format examples:
            #   "sports_sims.prep.nba_results_log.type" → model "nba_results_log"
            #   "model.jaffle_shop.orders.order_date"   → model "orders"
            parts = entity_id.rsplit(".", 1)
            model_name = parts[0].rsplit(".", 1)[-1] if len(parts) == 2 else entity_id
            if not model_name:
                return []

            result2 = await session.run(
                """
                MATCH (dependent:Entity)-[:LINEAGE]->(source:Entity {name: $name, type: 'model'})
                RETURN DISTINCT dependent
                """,
                name=model_name,
            )
            fallback: list[Entity] = []
            async for record in result2:
                node = record["dependent"]
                fallback.append(Entity(
                    id=node["id"],
                    name=node["name"],
                    type=node["type"],
                    source_connector=node["source_connector"],
                ))
            return fallback

    async def get_entity_semantic_edges(self, entity_id: str) -> list[SemanticEdge]:
        """Return all semantic edges where this entity is the source or the target."""
        async with self._driver.session() as session:
            result = await session.run(
                """
                MATCH (a:Entity)-[r:SEMANTIC]->(b:Entity)
                WHERE a.id = $entity_id OR b.id = $entity_id
                RETURN a.id AS from_id, b.id AS to_id,
                       r.type AS type, r.description AS description,
                       r.created_by AS created_by, r.created_at AS created_at
                """,
                entity_id=entity_id,
            )
            edges: list[SemanticEdge] = []
            async for record in result:
                edges.append(SemanticEdge(
                    from_entity_id=record["from_id"],
                    to_entity_id=record["to_id"],
                    type=record["type"],
                    description=record["description"],
                    created_by=record["created_by"] or "scan",
                ))
            return edges

    async def delete_semantic_edge(self, from_entity_id: str, to_entity_id: str) -> None:
        """Remove a semantic edge between two entities."""
        async with self._driver.session() as session:
            await session.run(
                """
                MATCH (a:Entity {id: $from_id})-[r:SEMANTIC]->(b:Entity {id: $to_id})
                DELETE r
                """,
                from_id=from_entity_id,
                to_id=to_entity_id,
            )

    async def write_correction(self, event: CorrectionEvent, updated_edges: list[SemanticEdge]) -> None:
        """Atomically apply a correction: remove deleted edges, upsert updated edges.

        The new definition is written separately via upsert_definition before
        calling this method. This method handles only edge mutations and records
        the CorrectionEvent node.
        """
        async with self._driver.session() as session:
            # Remove edges marked for deletion
            for from_id, to_id in event.edges_removed:
                await session.run(
                    "MATCH (a:Entity {id: $from_id})-[r:SEMANTIC]->(b:Entity {id: $to_id}) DELETE r",
                    from_id=from_id,
                    to_id=to_id,
                )

            # Upsert updated edges
            for edge in updated_edges:
                await session.run(
                    """
                    MATCH (a:Entity {id: $from_id})
                    MATCH (b:Entity {id: $to_id})
                    MERGE (a)-[r:SEMANTIC {type: $type}]->(b)
                    SET r.description = $description,
                        r.created_by = $created_by,
                        r.created_at = $created_at
                    """,
                    from_id=edge.from_entity_id,
                    to_id=edge.to_entity_id,
                    type=edge.type.value,
                    description=edge.description,
                    created_by=edge.created_by,
                    created_at=edge.created_at.isoformat(),
                )

            # Record the CorrectionEvent node
            await session.run(
                """
                CREATE (c:CorrectionEvent {
                    id: $id,
                    entity_id: $entity_id,
                    old_definition_id: $old_definition_id,
                    new_definition_id: $new_definition_id,
                    classified_as: $classified_as,
                    change_event_id: $change_event_id,
                    created_at: $created_at
                })
                """,
                id=event.id,
                entity_id=event.entity_id,
                old_definition_id=event.old_definition_id,
                new_definition_id=event.new_definition_id,
                classified_as=event.classified_as,
                change_event_id=event.change_event_id,
                created_at=event.created_at.isoformat(),
            )

    # ── Project management ────────────────────────────────────────────────────

    async def get_projects(self) -> list[str]:
        """Return all distinct project names currently in the graph."""
        async with self._driver.session() as session:
            result = await session.run(
                """
                MATCH (e:Entity)
                WHERE e.project IS NOT NULL AND e.project <> ''
                RETURN DISTINCT e.project AS project ORDER BY project
                """
            )
            projects = []
            async for record in result:
                projects.append(record["project"])
            return projects

    async def purge_project(self, project: str) -> int:
        """Delete all entities (and their definitions + edges) for a project.

        Returns the number of entities deleted.
        """
        async with self._driver.session() as session:
            result = await session.run(
                """
                MATCH (e:Entity {project: $project})
                OPTIONAL MATCH (e)-[:HAS_DEFINITION]->(d:Definition)
                DETACH DELETE e, d
                RETURN count(DISTINCT e) AS deleted
                """,
                project=project,
            )
            record = await result.single()
            return record["deleted"] if record else 0

    async def purge_all(self) -> int:
        """Delete everything in the Knowledge Graph.

        Removes all Entity, Definition, ChangeEvent, and CorrectionEvent nodes
        along with all relationships. Returns the total number of nodes deleted.
        """
        async with self._driver.session() as session:
            result = await session.run("MATCH (n) DETACH DELETE n RETURN count(n) AS deleted")
            record = await result.single()
            return record["deleted"] if record else 0

    # ── Change events ─────────────────────────────────────────────────────────

    async def save_change_event(self, event: ChangeEvent) -> None:
        async with self._driver.session() as session:
            await session.run(
                """
                MERGE (c:ChangeEvent {id: $id})
                SET c.type = $type,
                    c.source_entity_id = $source_entity_id,
                    c.change = $change,
                    c.semantic_context = $semantic_context,
                    c.declared_by = $declared_by,
                    c.status = $status,
                    c.detected_at = $detected_at
                """,
                id=event.id,
                type=event.type.value,
                source_entity_id=event.source_entity_id,
                change=str(event.change),
                semantic_context=event.semantic_context,
                declared_by=event.declared_by,
                status=event.status.value,
                detected_at=event.detected_at.isoformat(),
            )
