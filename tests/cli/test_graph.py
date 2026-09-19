"""`puxti graph` — HTML export of the Knowledge Graph."""

import asyncio
from unittest.mock import patch

from puxti.cli import app
from puxti.core.graph import KnowledgeGraph
from puxti.models import (
    Definition,
    Edge,
    EdgeType,
    Entity,
    EntityStatus,
    EntityType,
    SemanticEdge,
)
from tests.cli._helpers import plain, runner


def _seed(db_path):
    async def seed():
        kg = KnowledgeGraph(db_path=db_path)
        await kg.connect()
        source = Entity(id="source.shop.raw_orders", name="raw_orders", type=EntityType.TABLE,
                        source_connector="dbt", project="shop")
        orders = Entity(id="model.shop.orders", name="orders", type=EntityType.MODEL,
                        source_connector="dbt", project="shop")
        nrr = Entity(id="metric.proposed.nrr", name="nrr", type=EntityType.METRIC,
                     source_connector="proposed", project="", status=EntityStatus.PROPOSED)
        for e in (source, orders, nrr):
            await kg.upsert_entity(e)
        await kg.upsert_definition(Definition(
            entity_id=orders.id, description="One row per order.", version=1, created_by="scan"))
        await kg.upsert_definition(Definition(
            entity_id=nrr.id, description="Net revenue retention.", version=1, created_by="user"))
        # lineage: orders depends on the raw source
        await kg.upsert_edge(Edge(from_entity_id=orders.id, to_entity_id=source.id,
                                  type=EdgeType.DEPENDS_ON, connector="dbt"))
        # semantic: the proposed metric derives from orders
        await kg.upsert_semantic_edge(SemanticEdge(
            from_entity_id=nrr.id, to_entity_id=orders.id, type=EdgeType.DERIVED_FROM,
            description="nrr derived from orders", created_by="user"))
        await kg.close()
    asyncio.run(seed())


def test_graph_shows_help():
    result = runner.invoke(app, ["graph", "--help"])
    assert result.exit_code == 0
    out = plain(result.output)
    assert "--output" in out
    assert "--project" in out
    assert "--open" in out


def test_graph_writes_self_contained_html(tmp_path):
    db_path = tmp_path / "graph.db"
    _seed(db_path)
    out = tmp_path / "kg.html"

    with patch("puxti.cli.graph.KnowledgeGraph", lambda: KnowledgeGraph(db_path=db_path)):
        result = runner.invoke(app, ["graph", "-o", str(out)])
    assert result.exit_code == 0, result.output

    html = out.read_text()
    # Embedded data for the seeded entities and both edge kinds.
    assert "orders" in html
    assert "metric.proposed.nrr" in html
    assert '"kind": "lineage"' in html
    assert '"kind": "semantic"' in html
    assert '"status": "proposed"' in html
    assert "One row per order." in html
    # Self-contained: an inline <script>, no external script/stylesheet sources.
    assert "<script>" in html
    assert "<script src" not in html
    assert "</html>" in html


def test_graph_project_filter_excludes_other_entities(tmp_path):
    db_path = tmp_path / "graph.db"
    _seed(db_path)
    out = tmp_path / "kg.html"

    # The proposed metric has project "" so a shop filter drops it.
    with patch("puxti.cli.graph.KnowledgeGraph", lambda: KnowledgeGraph(db_path=db_path)):
        result = runner.invoke(app, ["graph", "-o", str(out), "--project", "shop"])
    assert result.exit_code == 0, result.output
    html = out.read_text()
    assert "model.shop.orders" in html
    assert "metric.proposed.nrr" not in html


def test_graph_empty_writes_placeholder(tmp_path):
    db_path = tmp_path / "empty.db"
    out = tmp_path / "kg.html"

    with patch("puxti.cli.graph.KnowledgeGraph", lambda: KnowledgeGraph(db_path=db_path)):
        result = runner.invoke(app, ["graph", "-o", str(out)])
    assert result.exit_code == 0, result.output
    assert "empty" in out.read_text().lower()
    assert "Run `puxti scan`" in plain(result.output)
