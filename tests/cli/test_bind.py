"""`puxti bind` — binding a proposed metric to a real entity."""

import asyncio
from unittest.mock import patch

from puxti.cli import app
from puxti.core.graph import KnowledgeGraph
from puxti.models import Definition, Entity, EntityStatus, EntityType
from tests.cli._helpers import plain, runner


def _seed_proposed_and_target(db_path):
    async def seed():
        kg = KnowledgeGraph(db_path=db_path)
        await kg.connect()
        await kg.upsert_entity(
            Entity(
                id="metric.proposed.nrr",
                name="nrr",
                type=EntityType.METRIC,
                source_connector="proposed",
                project="",
                status=EntityStatus.PROPOSED,
            )
        )
        await kg.upsert_definition(
            Definition(
                entity_id="metric.proposed.nrr",
                description="net revenue retention",
                version=1,
                created_by="user",
            )
        )
        await kg.upsert_entity(
            Entity(
                id="model.jaffle_shop.fct_nrr",
                name="fct_nrr",
                type=EntityType.MODEL,
                source_connector="dbt",
                project="jaffle_shop",
            )
        )
        await kg.close()

    asyncio.run(seed())


def test_bind_happy_path_carries_definition(tmp_path):
    db_path = tmp_path / "graph.db"
    _seed_proposed_and_target(db_path)

    with patch("puxti.cli.bind.KnowledgeGraph", lambda: KnowledgeGraph(db_path=db_path)):
        result = runner.invoke(
            app,
            [
                "bind",
                "--proposed",
                "metric.proposed.nrr",
                "--to",
                "model.jaffle_shop.fct_nrr",
            ],
        )
    assert result.exit_code == 0, result.output

    async def read():
        kg = KnowledgeGraph(db_path=db_path)
        await kg.connect()
        try:
            gone = await kg.get_entity_by_id("metric.proposed.nrr")
            defn = await kg.get_latest_definition("model.jaffle_shop.fct_nrr")
            return gone, defn
        finally:
            await kg.close()

    gone, defn = asyncio.run(read())
    assert gone is None
    assert defn is not None and defn.description == "net revenue retention"


def test_bind_rejects_non_proposed_source(tmp_path):
    db_path = tmp_path / "graph.db"
    _seed_proposed_and_target(db_path)

    with patch("puxti.cli.bind.KnowledgeGraph", lambda: KnowledgeGraph(db_path=db_path)):
        result = runner.invoke(
            app,
            [
                "bind",
                "--proposed",
                "model.jaffle_shop.fct_nrr",
                "--to",
                "model.jaffle_shop.fct_nrr",
            ],
        )
    assert result.exit_code == 1
    assert "not a proposed metric" in plain(result.output)


def test_bind_rejects_missing_target(tmp_path):
    db_path = tmp_path / "graph.db"
    _seed_proposed_and_target(db_path)

    with patch("puxti.cli.bind.KnowledgeGraph", lambda: KnowledgeGraph(db_path=db_path)):
        result = runner.invoke(
            app,
            [
                "bind",
                "--proposed",
                "metric.proposed.nrr",
                "--to",
                "model.jaffle_shop.missing",
            ],
        )
    assert result.exit_code == 1
    assert "not found" in plain(result.output)
