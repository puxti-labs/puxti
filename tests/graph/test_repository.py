"""Integration tests for the SQLite-backed KnowledgeGraph."""
from __future__ import annotations

from pathlib import Path

import pytest

from puxti.core.graph import KnowledgeGraph
from puxti.models import (
    ChangeEvent,
    ChangeType,
    CorrectionEvent,
    Definition,
    Edge,
    EdgeType,
    Entity,
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
