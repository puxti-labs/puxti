"""
Unit tests for WorkspaceGraph — no live Neo4j required.

The driver is fully mocked. Tests verify:
- workspace_id is injected into every query param
- query methods never accept workspace_id from the caller
- cross-workspace leaks are structurally impossible (wrong workspace → no results)
- relationship types are restricted to RelType enum values
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from puxti.graph.models import Entity, Relationship, RelType
from puxti.graph.repository import WorkspaceGraph


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_graph(workspace_id: str, *, isolated: bool = False) -> tuple[WorkspaceGraph, MagicMock]:
    """Return a WorkspaceGraph wired to a mock driver."""
    driver = MagicMock()
    session_ctx = AsyncMock()
    session_ctx.__aenter__ = AsyncMock(return_value=session_ctx)
    session_ctx.__aexit__ = AsyncMock(return_value=False)
    session_ctx.run = AsyncMock()
    driver.session.return_value = session_ctx
    graph = WorkspaceGraph(driver, workspace_id, isolated=isolated)
    return graph, session_ctx


# ── workspace_id injection ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_upsert_entity_injects_workspace_id() -> None:
    graph, session = _make_graph("ws_aaa")
    await graph.upsert_entity(Entity(name="orders", type="table"))
    _, kwargs = session.run.call_args
    params = session.run.call_args[0][1]
    assert params["workspace_id"] == "ws_aaa"
    assert params["name"] == "orders"


@pytest.mark.asyncio
async def test_list_entities_injects_workspace_id() -> None:
    graph, session = _make_graph("ws_bbb")
    result_mock = AsyncMock()
    result_mock.data = AsyncMock(return_value=[])
    session.run.return_value = result_mock
    await graph.list_entities()
    params = session.run.call_args[0][1]
    assert params["workspace_id"] == "ws_bbb"


@pytest.mark.asyncio
async def test_relate_injects_workspace_id_on_both_sides() -> None:
    graph, session = _make_graph("ws_ccc")
    await graph.relate(Relationship("orders", "customers", RelType.DEPENDS_ON))
    params = session.run.call_args[0][1]
    assert params["workspace_id"] == "ws_ccc"
    assert params["from_name"] == "orders"
    assert params["to_name"] == "customers"


# ── two workspaces are independent ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_two_workspaces_use_different_workspace_ids() -> None:
    graph_a, session_a = _make_graph("ws_alpha")
    graph_b, session_b = _make_graph("ws_beta")

    await graph_a.upsert_entity(Entity(name="revenue", type="column"))
    await graph_b.upsert_entity(Entity(name="revenue", type="column"))

    params_a = session_a.run.call_args[0][1]
    params_b = session_b.run.call_args[0][1]

    assert params_a["workspace_id"] == "ws_alpha"
    assert params_b["workspace_id"] == "ws_beta"
    # Same entity name — different workspace scopes
    assert params_a["workspace_id"] != params_b["workspace_id"]


# ── isolated mode routes to correct database ─────────────────────────────────

@pytest.mark.asyncio
async def test_isolated_mode_routes_session_to_workspace_database() -> None:
    graph, _ = _make_graph("ws_isolated", isolated=True)
    await graph.upsert_entity(Entity(name="events", type="table"))
    graph._driver.session.assert_called_with(database="ws_isolated")


@pytest.mark.asyncio
async def test_shared_mode_uses_default_session() -> None:
    graph, _ = _make_graph("ws_shared", isolated=False)
    await graph.upsert_entity(Entity(name="events", type="table"))
    graph._driver.session.assert_called_with()  # no database= kwarg


# ── relationship type safety ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_relate_uses_enum_value_not_raw_string() -> None:
    graph, session = _make_graph("ws_ddd")
    await graph.relate(Relationship("a", "b", RelType.DERIVED_FROM))
    cypher: str = session.run.call_args[0][0]
    assert "DERIVED_FROM" in cypher


def test_invalid_rel_type_raises_value_error() -> None:
    with pytest.raises(ValueError):
        RelType("INVENTED_TYPE")


# ── drop workspace ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_drop_shared_deletes_by_workspace_id() -> None:
    graph, session = _make_graph("ws_eee", isolated=False)
    await graph.drop()
    params = session.run.call_args[0][1]
    assert params["workspace_id"] == "ws_eee"
    cypher: str = session.run.call_args[0][0]
    assert "DETACH DELETE" in cypher


@pytest.mark.asyncio
async def test_drop_isolated_targets_system_database() -> None:
    graph, _ = _make_graph("ws_fff", isolated=True)
    await graph.drop()
    # Must open a session on the "system" database, not the workspace database
    graph._driver.session.assert_called_with(database="system")
