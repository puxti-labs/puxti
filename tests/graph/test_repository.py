"""Integration tests for the SQLite-backed KnowledgeGraph."""
from __future__ import annotations

from pathlib import Path

import pytest

import aiosqlite

from puxti.core.graph import KnowledgeGraph
from puxti.models import (
    ChangeEvent,
    ChangeType,
    CorrectionEvent,
    Definition,
    Edge,
    EdgeType,
    Entity,
    EntityStatus,
    EntityType,
    SemanticEdge,
)


@pytest.fixture
async def kg() -> KnowledgeGraph:
    graph = KnowledgeGraph(db_path=Path(":memory:"))
    await graph.connect()
    yield graph
    await graph.close()


def _entity(name: str, etype: EntityType = EntityType.MODEL, project: str = "test") -> Entity:
    return Entity(name=name, type=etype, source_connector="dbt", project=project)


def _sedge(from_id: str, to_id: str, etype: EdgeType = EdgeType.DERIVED_FROM) -> SemanticEdge:
    return SemanticEdge(
        from_entity_id=from_id,
        to_entity_id=to_id,
        type=etype,
        description="test edge",
        created_by="test",
    )


# ── entity CRUD ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_upsert_and_get_entity_by_id(kg: KnowledgeGraph) -> None:
    e = _entity("orders")
    await kg.upsert_entity(e)
    result = await kg.get_entity_by_id(e.id)
    assert result is not None
    assert result.name == "orders"
    assert result.id == e.id


@pytest.mark.asyncio
async def test_get_entity_by_id_missing_returns_none(kg: KnowledgeGraph) -> None:
    assert await kg.get_entity_by_id("does-not-exist") is None


@pytest.mark.asyncio
async def test_upsert_entity_is_idempotent(kg: KnowledgeGraph) -> None:
    e = _entity("orders")
    await kg.upsert_entity(e)
    await kg.upsert_entity(e)
    ids = await kg.get_all_entity_ids()
    assert ids.count(e.id) == 1


@pytest.mark.asyncio
async def test_upsert_entity_by_name_creates_then_returns_same_id(kg: KnowledgeGraph) -> None:
    e = _entity("revenue")
    first = await kg.upsert_entity_by_name(e)
    second = await kg.upsert_entity_by_name(_entity("revenue"))
    assert first.id == second.id


@pytest.mark.asyncio
async def test_get_entity_by_name(kg: KnowledgeGraph) -> None:
    e = _entity("customers")
    await kg.upsert_entity(e)
    result = await kg.get_entity_by_name("customers", "dbt")
    assert result is not None
    assert result.id == e.id


@pytest.mark.asyncio
async def test_filter_existing_entity_ids(kg: KnowledgeGraph) -> None:
    e = _entity("orders")
    await kg.upsert_entity(e)
    found = await kg.filter_existing_entity_ids([e.id, "phantom-id"])
    assert e.id in found
    assert "phantom-id" not in found


@pytest.mark.asyncio
async def test_filter_existing_entity_ids_empty_input(kg: KnowledgeGraph) -> None:
    assert await kg.filter_existing_entity_ids([]) == []


# ── lineage edges ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_upsert_edge_and_get_structural_dependents(kg: KnowledgeGraph) -> None:
    parent = await kg.upsert_entity_by_name(_entity("raw_orders"))
    child = await kg.upsert_entity_by_name(_entity("stg_orders"))
    await kg.upsert_edge(Edge(
        from_entity_id=child.id,
        to_entity_id=parent.id,
        type=EdgeType.DEPENDS_ON,
        connector="dbt",
    ))
    deps = await kg.get_structural_dependents(parent.id)
    assert any(e.id == child.id for e in deps)


@pytest.mark.asyncio
async def test_get_structural_ancestors(kg: KnowledgeGraph) -> None:
    source = await kg.upsert_entity_by_name(_entity("raw_orders"))
    stg = await kg.upsert_entity_by_name(_entity("stg_orders"))
    mart = await kg.upsert_entity_by_name(_entity("orders"))
    await kg.upsert_edge(Edge(from_entity_id=stg.id, to_entity_id=source.id, type=EdgeType.DEPENDS_ON, connector="dbt"))
    await kg.upsert_edge(Edge(from_entity_id=mart.id, to_entity_id=stg.id, type=EdgeType.DEPENDS_ON, connector="dbt"))
    ancestors = await kg.get_structural_ancestors(mart.id)
    ancestor_ids = {e.id for e, _ in ancestors}
    assert stg.id in ancestor_ids
    assert source.id in ancestor_ids


# ── semantic edges ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_upsert_and_get_semantic_edge(kg: KnowledgeGraph) -> None:
    a = await kg.upsert_entity_by_name(_entity("revenue"))
    b = await kg.upsert_entity_by_name(_entity("sales"))
    await kg.upsert_semantic_edge(_sedge(a.id, b.id))
    edges = await kg.get_all_semantic_edges()
    assert any(e.from_entity_id == a.id and e.to_entity_id == b.id for e in edges)


@pytest.mark.asyncio
async def test_delete_semantic_edge(kg: KnowledgeGraph) -> None:
    a = await kg.upsert_entity_by_name(_entity("a"))
    b = await kg.upsert_entity_by_name(_entity("b"))
    await kg.upsert_semantic_edge(_sedge(a.id, b.id))
    await kg.delete_semantic_edge(a.id, b.id)
    edges = await kg.get_all_semantic_edges()
    assert not any(e.from_entity_id == a.id for e in edges)


@pytest.mark.asyncio
async def test_get_semantic_dependents_with_depth(kg: KnowledgeGraph) -> None:
    # chain: c → b → a  (c and b depend on a transitively)
    a = await kg.upsert_entity_by_name(_entity("a"))
    b = await kg.upsert_entity_by_name(_entity("b"))
    c = await kg.upsert_entity_by_name(_entity("c"))
    await kg.upsert_semantic_edge(_sedge(b.id, a.id))
    await kg.upsert_semantic_edge(_sedge(c.id, b.id))
    deps = await kg.get_semantic_dependents_with_depth(a.id)
    depths = {e.id: d for e, d in deps}
    assert depths[b.id] == 1
    assert depths[c.id] == 2


@pytest.mark.asyncio
async def test_get_semantic_dependents(kg: KnowledgeGraph) -> None:
    a = await kg.upsert_entity_by_name(_entity("a"))
    b = await kg.upsert_entity_by_name(_entity("b"))
    await kg.upsert_semantic_edge(_sedge(b.id, a.id))
    deps = await kg.get_semantic_dependents(a.id)
    assert any(e.id == b.id for e in deps)


@pytest.mark.asyncio
async def test_get_entity_semantic_edges(kg: KnowledgeGraph) -> None:
    a = await kg.upsert_entity_by_name(_entity("a"))
    b = await kg.upsert_entity_by_name(_entity("b"))
    await kg.upsert_semantic_edge(_sedge(a.id, b.id))
    edges = await kg.get_entity_semantic_edges(a.id)
    assert len(edges) == 1
    assert edges[0].from_entity_id == a.id


# ── definitions ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_upsert_and_get_latest_definition(kg: KnowledgeGraph) -> None:
    e = await kg.upsert_entity_by_name(_entity("orders"))
    d1 = Definition(entity_id=e.id, description="first", version=1, created_by="test")
    d2 = Definition(entity_id=e.id, description="second", version=2, created_by="test")
    await kg.upsert_definition(d1)
    await kg.upsert_definition(d2)
    latest = await kg.get_latest_definition(e.id)
    assert latest is not None
    assert latest.version == 2
    assert latest.description == "second"


@pytest.mark.asyncio
async def test_get_latest_definition_missing_returns_none(kg: KnowledgeGraph) -> None:
    assert await kg.get_latest_definition("no-entity") is None


@pytest.mark.asyncio
async def test_get_definition_history_returns_versions_in_order(kg: KnowledgeGraph) -> None:
    e = await kg.upsert_entity_by_name(_entity("orders"))
    d1 = Definition(entity_id=e.id, description="first", version=1, created_by="llm")
    d2 = Definition(entity_id=e.id, description="second", version=2, created_by="user")
    d3 = Definition(entity_id=e.id, description="third", version=3, created_by="user")
    for d in [d1, d2, d3]:
        await kg.upsert_definition(d)
    history = await kg.get_definition_history(e.id)
    assert len(history) == 3
    assert [h.version for h in history] == [1, 2, 3]
    assert history[0].description == "first"
    assert history[2].created_by == "user"


@pytest.mark.asyncio
async def test_get_definition_history_empty(kg: KnowledgeGraph) -> None:
    assert await kg.get_definition_history("no-entity") == []


# ── project management ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_projects(kg: KnowledgeGraph) -> None:
    await kg.upsert_entity_by_name(_entity("orders", project="alpha"))
    await kg.upsert_entity_by_name(_entity("revenue", project="beta"))
    projects = await kg.get_projects()
    assert "alpha" in projects
    assert "beta" in projects


@pytest.mark.asyncio
async def test_purge_project_removes_entities_and_edges(kg: KnowledgeGraph) -> None:
    a = await kg.upsert_entity_by_name(_entity("a", project="alpha"))
    b = await kg.upsert_entity_by_name(_entity("b", project="beta"))
    await kg.upsert_semantic_edge(_sedge(a.id, b.id))
    deleted = await kg.purge_project("alpha")
    assert deleted == 1
    assert await kg.get_entity_by_id(a.id) is None
    assert await kg.get_entity_by_id(b.id) is not None
    edges = await kg.get_all_semantic_edges()
    assert not any(e.from_entity_id == a.id for e in edges)


@pytest.mark.asyncio
async def test_purge_all(kg: KnowledgeGraph) -> None:
    await kg.upsert_entity_by_name(_entity("x"))
    await kg.upsert_entity_by_name(_entity("y"))
    deleted = await kg.purge_all()
    assert deleted == 2
    assert await kg.get_all_entity_ids() == []


# ── change events ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_save_change_event(kg: KnowledgeGraph) -> None:
    event = ChangeEvent(
        type=ChangeType.SEMANTIC,
        source_entity_id="model.test.orders",
        change={"description": "new meaning"},
    )
    await kg.save_change_event(event)
    # Verify it's persisted by re-saving (upsert should not error)
    await kg.save_change_event(event)


# ── get_all_entities_with_definitions ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_all_entities_with_definitions(kg: KnowledgeGraph) -> None:
    a = await kg.upsert_entity_by_name(_entity("a"))
    b = await kg.upsert_entity_by_name(_entity("b"))
    await kg.upsert_definition(Definition(entity_id=a.id, description="defined", version=1, created_by="test"))
    pairs = await kg.get_all_entities_with_definitions()
    d = {e.id: defn for e, defn in pairs}
    assert d[a.id] is not None
    assert d[b.id] is None


# ── proposed entities + status migration + binding ────────────────────────────

_OLD_ENTITIES_SCHEMA = """
CREATE TABLE entities (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT NOT NULL,
    source_connector TEXT NOT NULL,
    project TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


@pytest.mark.asyncio
async def test_status_migration_adds_column_and_defaults_bound(tmp_path: Path) -> None:
    """A graph.db created before the status column upgrades cleanly, and its
    pre-existing rows default to bound."""
    db_path = tmp_path / "graph.db"
    async with aiosqlite.connect(str(db_path)) as raw:
        await raw.executescript(_OLD_ENTITIES_SCHEMA)
        await raw.execute(
            "INSERT INTO entities (id, name, type, source_connector, project, created_at, updated_at) "
            "VALUES ('e1', 'orders', 'model', 'dbt', 'test', '2024-01-01T00:00:00', '2024-01-01T00:00:00')"
        )
        await raw.commit()

    graph = KnowledgeGraph(db_path=db_path)
    await graph.connect()
    try:
        existing = await graph.get_entity_by_id("e1")
        assert existing is not None
        assert existing.status == EntityStatus.BOUND
    finally:
        await graph.close()

    # Re-opening is a no-op (idempotent).
    graph2 = KnowledgeGraph(db_path=db_path)
    await graph2.connect()
    try:
        assert (await graph2.get_entity_by_id("e1")).status == EntityStatus.BOUND
    finally:
        await graph2.close()


@pytest.mark.asyncio
async def test_get_proposed_and_reconcile_candidates(kg: KnowledgeGraph) -> None:
    proposed = Entity(name="nrr", type=EntityType.METRIC, source_connector="proposed",
                      project="test", status=EntityStatus.PROPOSED)
    await kg.upsert_entity(proposed)
    model = _entity("orders")  # bound dbt model
    await kg.upsert_entity(model)

    proposed_list = await kg.get_proposed_entities()
    assert [e.id for e in proposed_list] == [proposed.id]

    candidates = await kg.get_reconcile_candidates()
    assert model.id in {e.id for e in candidates}
    assert proposed.id not in {e.id for e in candidates}


@pytest.mark.asyncio
async def test_bind_entity_carries_definition_and_edges(kg: KnowledgeGraph) -> None:
    proposed = Entity(name="nrr", type=EntityType.METRIC, source_connector="proposed",
                      project="test", status=EntityStatus.PROPOSED)
    await kg.upsert_entity(proposed)
    await kg.upsert_definition(Definition(
        entity_id=proposed.id, description="net revenue retention", version=1, created_by="user"))

    upstream = _entity("revenue")
    await kg.upsert_entity(upstream)
    await kg.upsert_semantic_edge(_sedge(proposed.id, upstream.id))

    target = _entity("fct_nrr")
    await kg.upsert_entity(target)

    await kg.bind_entity(proposed.id, target.id)

    # Placeholder gone.
    assert await kg.get_entity_by_id(proposed.id) is None
    assert await kg.get_proposed_entities() == []
    # Definition carried onto the target.
    defn = await kg.get_latest_definition(target.id)
    assert defn is not None and defn.description == "net revenue retention"
    # Semantic edge re-pointed to the target.
    edges = await kg.get_entity_semantic_edges(target.id)
    assert any(e.from_entity_id == target.id and e.to_entity_id == upstream.id for e in edges)


@pytest.mark.asyncio
async def test_bind_entity_dedups_and_drops_self_loops(kg: KnowledgeGraph) -> None:
    proposed = Entity(name="nrr", type=EntityType.METRIC, source_connector="proposed",
                      project="test", status=EntityStatus.PROPOSED)
    await kg.upsert_entity(proposed)
    target = _entity("fct_nrr")
    await kg.upsert_entity(target)
    upstream = _entity("revenue")
    await kg.upsert_entity(upstream)

    # Edge proposed→target would become a self-loop after binding → dropped.
    await kg.upsert_semantic_edge(_sedge(proposed.id, target.id))
    # Duplicate edge already present on the target → de-duped, no error.
    await kg.upsert_semantic_edge(_sedge(proposed.id, upstream.id))
    await kg.upsert_semantic_edge(_sedge(target.id, upstream.id))

    await kg.bind_entity(proposed.id, target.id)

    edges = await kg.get_entity_semantic_edges(target.id)
    assert not any(e.from_entity_id == target.id and e.to_entity_id == target.id for e in edges)
    to_upstream = [e for e in edges if e.to_entity_id == upstream.id and e.from_entity_id == target.id]
    assert len(to_upstream) == 1


@pytest.mark.asyncio
async def test_bind_entity_is_single_transaction(kg: KnowledgeGraph) -> None:
    """The whole bind must be one transaction: the definition copy must not commit
    mid-bind (which would let a later failure leave a half-done bind)."""
    proposed = Entity(name="nrr", type=EntityType.METRIC, source_connector="proposed",
                      project="test", status=EntityStatus.PROPOSED)
    await kg.upsert_entity(proposed)
    await kg.upsert_definition(Definition(
        entity_id=proposed.id, description="net revenue retention", version=1, created_by="user"))
    target = _entity("fct_nrr")
    await kg.upsert_entity(target)

    original_commit = kg._db.commit
    calls = {"n": 0}

    async def counting_commit():
        calls["n"] += 1
        return await original_commit()

    kg._db.commit = counting_commit
    try:
        await kg.bind_entity(proposed.id, target.id)
    finally:
        kg._db.commit = original_commit

    assert calls["n"] == 1  # one commit for the whole bind, not one per write
    defn = await kg.get_latest_definition(target.id)
    assert defn is not None and defn.description == "net revenue retention"
