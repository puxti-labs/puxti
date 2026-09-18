"""`puxti scan` reconcile pass — binding proposed metrics to scanned entities."""

import asyncio
from unittest.mock import patch

from puxti.cli.scan import _reconcile_proposed
from puxti.core.graph import KnowledgeGraph
from puxti.models import Definition, Entity, EntityStatus, EntityType


def _proposed(kg_calls, name="orders_total"):
    return Entity(
        id=f"metric.proposed.{name}",
        name=name,
        type=EntityType.METRIC,
        source_connector="proposed",
        project="",
        status=EntityStatus.PROPOSED,
    )


async def _seed_proposed(kg, name="orders_total"):
    await kg.upsert_entity(_proposed(kg, name))
    await kg.upsert_definition(
        Definition(
            entity_id=f"metric.proposed.{name}",
            description="total orders",
            version=1,
            created_by="user",
        )
    )


async def _seed_model(kg, entity_id, name, project="shop"):
    await kg.upsert_entity(
        Entity(
            id=entity_id, name=name, type=EntityType.MODEL, source_connector="dbt", project=project
        )
    )


def test_reconcile_binds_exact_name_match_on_confirm(tmp_path):
    db_path = tmp_path / "graph.db"

    async def scenario():
        kg = KnowledgeGraph(db_path=db_path)
        await kg.connect()
        await _seed_proposed(kg)
        await _seed_model(kg, "model.shop.orders_total", "orders_total")
        with patch("puxti.cli.scan.console.input", return_value="y"):
            await _reconcile_proposed(kg, interactive=False)
        gone = await kg.get_entity_by_id("metric.proposed.orders_total")
        defn = await kg.get_latest_definition("model.shop.orders_total")
        await kg.close()
        return gone, defn

    gone, defn = asyncio.run(scenario())
    assert gone is None
    assert defn is not None and defn.description == "total orders"


def test_reconcile_cancel_leaves_metric_unbound(tmp_path):
    db_path = tmp_path / "graph.db"

    async def scenario():
        kg = KnowledgeGraph(db_path=db_path)
        await kg.connect()
        await _seed_proposed(kg)
        await _seed_model(kg, "model.shop.orders_total", "orders_total")
        with patch("puxti.cli.scan.console.input", return_value="n"):
            await _reconcile_proposed(kg, interactive=False)
        still = await kg.get_entity_by_id("metric.proposed.orders_total")
        await kg.close()
        return still

    still = asyncio.run(scenario())
    assert still is not None
    assert still.status == EntityStatus.PROPOSED


def test_reconcile_skips_ambiguous_name(tmp_path):
    db_path = tmp_path / "graph.db"

    async def scenario():
        kg = KnowledgeGraph(db_path=db_path)
        await kg.connect()
        await _seed_proposed(kg)
        # Two bound entities share the name → ambiguous → dropped from the index.
        await _seed_model(kg, "model.shop.orders_total", "orders_total", project="shop")
        await _seed_model(kg, "model.mart.orders_total", "orders_total", project="mart")
        called = {"n": 0}

        def _input(*a, **k):
            called["n"] += 1
            return "y"

        with patch("puxti.cli.scan.console.input", side_effect=_input):
            await _reconcile_proposed(kg, interactive=False)
        still = await kg.get_entity_by_id("metric.proposed.orders_total")
        await kg.close()
        return still, called["n"]

    still, prompts = asyncio.run(scenario())
    assert still is not None  # not bound — ambiguous match skipped
    assert prompts == 0  # no confirmation prompt when there is nothing to bind


def test_reconcile_no_proposed_is_noop(tmp_path):
    db_path = tmp_path / "graph.db"

    async def scenario():
        kg = KnowledgeGraph(db_path=db_path)
        await kg.connect()
        await _seed_model(kg, "model.shop.orders_total", "orders_total")
        # Should not prompt or raise when there are no proposed metrics.
        with patch("puxti.cli.scan.console.input", side_effect=AssertionError("should not prompt")):
            await _reconcile_proposed(kg, interactive=False)
        await kg.close()

    asyncio.run(scenario())
