"""`puxti define` — proposed metric creation."""

import asyncio
from unittest.mock import patch

from puxti.cli import app
from puxti.core.graph import KnowledgeGraph
from puxti.models import EdgeType, Entity, EntityStatus, EntityType
from tests.cli._helpers import plain, runner


def test_define_shows_help():
    result = runner.invoke(app, ["define", "--help"])
    assert result.exit_code == 0
    out = plain(result.output)
    assert "--name" in out
    assert "--description" in out
    assert "--derived-from" in out


def test_define_missing_args_exits_nonzero():
    assert runner.invoke(app, ["define"]).exit_code != 0


def test_define_creates_proposed_entity_and_definition(tmp_path):
    db_path = tmp_path / "graph.db"
    with patch("puxti.cli.define.KnowledgeGraph", lambda: KnowledgeGraph(db_path=db_path)):
        result = runner.invoke(
            app,
            [
                "define",
                "--name",
                "net_revenue_retention",
                "--description",
                "Revenue retained from existing customers, net of churn.",
            ],
        )
    assert result.exit_code == 0, result.output

    async def read():
        kg = KnowledgeGraph(db_path=db_path)
        await kg.connect()
        try:
            proposed = await kg.get_proposed_entities()
            entity = proposed[0]
            definition = await kg.get_latest_definition(entity.id)
            return entity, definition
        finally:
            await kg.close()

    entity, definition = asyncio.run(read())
    assert entity.id == "metric.proposed.net_revenue_retention"
    assert entity.type == EntityType.METRIC
    assert entity.status == EntityStatus.PROPOSED
    assert definition is not None
    assert definition.version == 1
    assert definition.created_by == "user"


def test_define_derived_from_writes_edge(tmp_path):
    db_path = tmp_path / "graph.db"

    async def seed():
        kg = KnowledgeGraph(db_path=db_path)
        await kg.connect()
        await kg.upsert_entity(
            Entity(
                id="model.jaffle_shop.subscriptions",
                name="subscriptions",
                type=EntityType.MODEL,
                source_connector="dbt",
                project="jaffle_shop",
            )
        )
        await kg.close()

    asyncio.run(seed())

    with patch("puxti.cli.define.KnowledgeGraph", lambda: KnowledgeGraph(db_path=db_path)):
        result = runner.invoke(
            app,
            [
                "define",
                "--name",
                "nrr",
                "--description",
                "…",
                "--derived-from",
                "model.jaffle_shop.subscriptions",
            ],
        )
    assert result.exit_code == 0, result.output

    async def read_edges():
        kg = KnowledgeGraph(db_path=db_path)
        await kg.connect()
        try:
            return await kg.get_entity_semantic_edges("metric.proposed.nrr")
        finally:
            await kg.close()

    edges = asyncio.run(read_edges())
    assert any(
        e.from_entity_id == "metric.proposed.nrr"
        and e.to_entity_id == "model.jaffle_shop.subscriptions"
        and e.type == EdgeType.DERIVED_FROM
        for e in edges
    )


def test_define_derived_from_missing_target_exits_1(tmp_path):
    db_path = tmp_path / "graph.db"
    with patch("puxti.cli.define.KnowledgeGraph", lambda: KnowledgeGraph(db_path=db_path)):
        result = runner.invoke(
            app,
            [
                "define",
                "--name",
                "nrr",
                "--description",
                "…",
                "--derived-from",
                "model.jaffle_shop.does_not_exist",
            ],
        )
    assert result.exit_code == 1
    assert "not found" in plain(result.output)
