"""BI spec connector: entities, lineage, resolution against dbt, and the path to impact."""

import asyncio
import json
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from rich.console import Console

from puxti.cli import app
from puxti.connectors.bi_specs import BiSpecsConnector
from puxti.connectors.dbt import DbtConnector
from puxti.core.graph import SqliteKnowledgeGraph
from puxti.core.resolution import build_reference_index
from puxti.core.scanner import SemanticScanner
from puxti.models import ChangeType, EntityType, SemanticChangeEvent, UnverifiedReason
from tests.cli._helpers import runner

HEAD = "apiVersion: puxti/v1alpha1\n"

DATASET = HEAD + """kind: dataset
name: orders
model: orders
columns: [order_date, status]
metrics:
  - {name: revenue, agg: sum, column: amount}
  - {name: order_count, agg: count}
  - {name: arr, dbt_metric: arr}
"""

CHART = HEAD + """kind: chart
name: revenue_by_month
dataset: orders
type: line
x: {column: order_date, grain: month}
metrics: [revenue]
group_by: [status]
"""

DASHBOARD = HEAD + """kind: dashboard
name: sales
rows:
  - charts: [revenue_by_month]
"""

MANIFEST = {
    "metadata": {"project_name": "shop"},
    "nodes": {
        "model.shop.orders": {
            "resource_type": "model",
            "name": "orders",
            "schema": "analytics",
            "original_file_path": "models/orders.sql",
            "columns": {"amount": {}, "order_date": {}, "status": {}},
            "depends_on": {"nodes": []},
        },
    },
    "sources": {},
    "metrics": {
        "metric.shop.arr": {
            "name": "arr",
            "depends_on": {"nodes": ["model.shop.orders"]},
        },
    },
}


def _project(tmp_path: Path, **files: str) -> Path:
    project = tmp_path / "shop"
    specs = {"bi/datasets/orders.yml": DATASET, "bi/charts/revenue.yml": CHART}
    specs["bi/dashboards/sales.yml"] = DASHBOARD
    specs.update(files)
    for rel, text in specs.items():
        path = project / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    (project / "target").mkdir(exist_ok=True)
    (project / "target" / "manifest.json").write_text(json.dumps(MANIFEST))
    (project / "models").mkdir(exist_ok=True)
    (project / "models" / "orders.sql").write_text("select 1 as amount")
    return project


def _bi(project: Path) -> BiSpecsConnector:
    return BiSpecsConnector({"project_dir": str(project)})


def _edges(connector: BiSpecsConnector) -> set[tuple[str, str]]:
    return {(e.from_entity_id, e.to_entity_id) for e in asyncio.run(connector.extract_lineage())}


# ── Entities and lineage ──────────────────────────────────────────────────────


def test_every_asset_becomes_an_entity(tmp_path: Path) -> None:
    entities = {e.id: e for e in asyncio.run(_bi(_project(tmp_path)).extract_entities())}
    assert set(entities) == {"dataset.bi.orders", "chart.bi.revenue_by_month", "dashboard.bi.sales"}
    assert entities["dataset.bi.orders"].type == EntityType.DATASET
    assert entities["chart.bi.revenue_by_month"].type == EntityType.CHART
    assert entities["dashboard.bi.sales"].type == EntityType.DASHBOARD
    assert entities["chart.bi.revenue_by_month"].metadata["path"] == "bi/charts/revenue.yml"
    assert entities["dashboard.bi.sales"].metadata["charts"] == ["revenue_by_month"]
    assert {e.source_connector for e in entities.values()} == {"bi"}


def test_lineage_runs_from_each_asset_to_what_it_reads(tmp_path: Path) -> None:
    assert _edges(_bi(_project(tmp_path))) == {
        ("dataset.bi.orders", "sqlref.orders"),
        ("dataset.bi.orders", "sqlref.orders#order_date"),
        ("dataset.bi.orders", "sqlref.orders#status"),
        ("dataset.bi.orders", "sqlref.orders#amount"),
        ("dataset.bi.orders", "sqlref.metric:arr"),
        ("chart.bi.revenue_by_month", "dataset.bi.orders"),
        ("chart.bi.revenue_by_month", "sqlref.orders#order_date"),
        ("chart.bi.revenue_by_month", "sqlref.orders#status"),
        ("chart.bi.revenue_by_month", "sqlref.orders#amount"),  # behind the revenue metric
        ("dashboard.bi.sales", "chart.bi.revenue_by_month"),
    }


def test_table_and_pie_charts_name_their_columns(tmp_path: Path) -> None:
    table = HEAD + "kind: chart\nname: t\ndataset: orders\ntype: table\ncolumns: [status, amount]\n"
    pie = HEAD + "kind: chart\nname: p\ndataset: orders\ntype: pie\nmetric: revenue\n"
    pie += "group_by: [status]\n"
    edges = _edges(_bi(_project(tmp_path, **{"bi/t.yml": table, "bi/p.yml": pie})))
    assert {("chart.bi.t", "sqlref.orders#status"), ("chart.bi.t", "sqlref.orders#amount")} <= edges
    assert {("chart.bi.p", "sqlref.orders#status"), ("chart.bi.p", "sqlref.orders#amount")} <= edges


def test_bad_file_is_skipped_with_a_warning_and_the_rest_load(tmp_path: Path, caplog) -> None:
    project = _project(tmp_path, **{"bi/broken.yml": HEAD + "kind: dataset\nname: Bad\n"})
    with caplog.at_level(logging.WARNING, logger="puxti.connectors.bi_specs"):
        entities = asyncio.run(_bi(project).extract_entities())
    assert len(entities) == 3
    assert "broken.yml" in caplog.text


def test_reference_to_a_missing_spec_is_warned_and_not_written(tmp_path: Path, caplog) -> None:
    orphan = HEAD + "kind: chart\nname: orphan\ndataset: nope\ntype: big_number\nmetric: m\n"
    with caplog.at_level(logging.WARNING, logger="puxti.connectors.bi_specs"):
        edges = _edges(_bi(_project(tmp_path, **{"bi/orphan.yml": orphan})))
    assert not any(f == "chart.bi.orphan" for f, _ in edges)
    assert "chart 'orphan' → dataset 'nope'" in caplog.text


def test_health_check_and_fingerprint_follow_the_spec_folder(tmp_path: Path) -> None:
    project = _project(tmp_path)
    connector = _bi(project)
    assert asyncio.run(connector.health_check())
    before = connector.source_fingerprint()
    (project / "bi" / "charts" / "revenue.yml").write_text(CHART.replace("month", "week"))
    assert _bi(project).source_fingerprint() != before
    missing = BiSpecsConnector({"project_dir": str(project), "specs_dir": "nope"})
    assert not asyncio.run(missing.health_check())
    assert missing.source_fingerprint() is None


def test_no_source_text_so_scan_makes_no_llm_calls(tmp_path: Path) -> None:
    assert _bi(_project(tmp_path)).get_model_sql_map() == {}


# ── Capture: affected assets are listed for manual review ─────────────────────


def _event(affected: list[str]) -> SemanticChangeEvent:
    return SemanticChangeEvent(
        change_event_id="e1",
        entity_id="model.shop.orders",
        change_type=ChangeType.STRUCTURAL,
        semantic_context="",
        affected_entity_ids=affected,
        reasoning="",
        change={},
    )


def test_affected_dataset_lists_everything_above_it(tmp_path: Path) -> None:
    diffs, unverified = asyncio.run(
        _bi(_project(tmp_path)).generate_changes(_event(["dataset.bi.orders"]))
    )
    assert diffs == []
    assert [u.entity_id for u in unverified] == [
        "chart.bi.revenue_by_month",
        "dashboard.bi.sales",
        "dataset.bi.orders",
    ]
    assert {u.reason for u in unverified} == {UnverifiedReason.BI_SPEC_MANUAL}
    assert unverified[0].detail == "bi/charts/revenue.yml"


def test_change_that_reaches_no_bi_asset_lists_nothing(tmp_path: Path) -> None:
    event = _event(["model.shop.x"]).model_copy(update={"entity_id": "model.shop.x"})
    assert asyncio.run(_bi(_project(tmp_path)).generate_changes(event)) == ([], [])


# ── End to end: dbt + specs → resolution → scan → graph → impact ──────────────


@pytest.fixture
def scanned(tmp_path: Path) -> Path:
    """Scan dbt (structural layer only) and the specs into a real SQLite graph."""
    project = _project(tmp_path)
    db_path = tmp_path / "graph.db"
    dbt = DbtConnector({"project_dir": str(project)})
    bi = _bi(project)
    backend = MagicMock()

    async def go() -> None:
        index = build_reference_index(
            await dbt.extract_entities() + await bi.extract_entities()
        )
        kg = SqliteKnowledgeGraph(db_path)
        await kg.connect()
        for entity in await dbt.extract_entities():
            await kg.upsert_entity(entity)
        for edge in await dbt.extract_lineage():
            await kg.upsert_edge(edge)
        result = await SemanticScanner(backend=backend).scan(
            bi, kg, interactive=False, console=Console(quiet=True), reference_index=index
        )
        assert result.entities_upserted == 3
        await kg.close()

    asyncio.run(go())
    assert backend.method_calls == []  # the bi connector costs no LLM calls
    return db_path


def _impact(db_path: Path, entity_id: str) -> dict[str, tuple[str, int]]:
    with patch(
        "puxti.cli.impact.KnowledgeGraph", side_effect=lambda: SqliteKnowledgeGraph(db_path)
    ):
        result = runner.invoke(app, ["impact", entity_id, "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    return {d["entity_id"]: (d["type"], d["hop"]) for d in payload["dependents"]}


def test_impact_of_a_model_reaches_its_dashboards(scanned: Path) -> None:
    deps = _impact(scanned, "model.shop.orders")
    assert deps["dataset.bi.orders"] == ("dataset", 1)
    assert deps["chart.bi.revenue_by_month"] == ("chart", 2)
    assert deps["dashboard.bi.sales"] == ("dashboard", 3)


def test_impact_of_a_column_reaches_the_charts_that_read_it(scanned: Path) -> None:
    deps = _impact(scanned, "model.shop.orders.amount")
    assert deps["chart.bi.revenue_by_month"] == ("chart", 1)
    assert deps["dataset.bi.orders"] == ("dataset", 1)
    assert deps["dashboard.bi.sales"] == ("dashboard", 2)


def test_impact_of_a_dbt_metric_reaches_its_dataset(scanned: Path) -> None:
    assert _impact(scanned, "metric.shop.arr")["dataset.bi.orders"] == ("dataset", 1)


def test_scan_of_bi_alone_warns_about_its_unresolved_dbt_references(tmp_path: Path) -> None:
    """Without dbt, nothing owns the models the specs name: scan must say so."""
    project = _project(tmp_path)
    with (
        patch(
            "puxti.cli.scan.KnowledgeGraph",
            side_effect=lambda: SqliteKnowledgeGraph(tmp_path / "graph.db"),
        ),
        patch("puxti.cli.scan.SemanticScanner", side_effect=lambda: SemanticScanner(MagicMock())),
        patch("puxti.cli.scan.build_configured_connectors", return_value=[_bi(project)]),
    ):
        result = runner.invoke(app, ["scan"])
    assert result.exit_code == 0, result.output
    assert "not owned by any configured" in result.output
    assert "orders#amount" in result.output


def _changed(entity_id: str, affected: list[str] | None = None) -> SemanticChangeEvent:
    event = _event(affected or [])
    return event.model_copy(update={"entity_id": entity_id})


def _listed(project: Path, event: SemanticChangeEvent) -> list[str]:
    return [u.entity_id for u in asyncio.run(_bi(project).generate_changes(event))[1]]


def test_column_change_reaches_its_readers_without_capture_naming_them(tmp_path: Path) -> None:
    """The LLM's affected list is empty; specs that read the column are still listed."""
    other = HEAD + "kind: chart\nname: statuses\ndataset: orders\ntype: table\ncolumns: [status]\n"
    project = _project(tmp_path, **{"bi/other.yml": other})
    assert _listed(project, _changed("model.shop.orders.amount")) == [
        "chart.bi.revenue_by_month",  # via the revenue metric on amount
        "dashboard.bi.sales",
        "dataset.bi.orders",  # aggregates amount
    ]  # chart.bi.statuses reads only status


def test_model_change_reaches_every_dataset_on_it_and_their_charts(tmp_path: Path) -> None:
    assert _listed(_project(tmp_path), _changed("model.shop.orders")) == [
        "chart.bi.revenue_by_month",
        "dashboard.bi.sales",
        "dataset.bi.orders",
    ]


def test_change_to_another_model_reaches_nothing(tmp_path: Path) -> None:
    assert _listed(_project(tmp_path), _changed("model.shop.customers.amount")) == []


def test_schema_qualified_dataset_model_is_matched_on_change(tmp_path: Path) -> None:
    qualified = DATASET.replace("model: orders", "model: analytics.orders")
    project = _project(tmp_path, **{"bi/datasets/orders.yml": qualified})
    assert "dataset.bi.orders" in _listed(project, _changed("model.shop.orders.amount"))


def test_dry_run_estimate_for_specs_alone_is_zero_with_no_backend_calls(tmp_path: Path) -> None:
    backend = MagicMock()
    estimate = asyncio.run(SemanticScanner(backend).estimate_scan_cost(_bi(_project(tmp_path))))
    assert estimate["total_input_tokens"] == 0
    assert estimate["total_est_output_tokens"] == 0
    assert estimate["estimated_cost_usd"] == 0.0
    assert backend.method_calls == []


def test_scan_of_specs_needs_no_llm_configuration(tmp_path: Path) -> None:
    """get_backend() raises on an incomplete provider config; a BI-only scan never calls it."""
    with patch("puxti.core.scanner.get_backend", side_effect=RuntimeError("no LLM config")):
        scanner = SemanticScanner()

        async def go() -> int:
            kg = SqliteKnowledgeGraph(tmp_path / "g.db")
            await kg.connect()
            try:
                result = await scanner.scan(
                    _bi(_project(tmp_path)), kg, interactive=False, console=Console(quiet=True)
                )
            finally:
                await kg.close()
            return result.entities_upserted

        assert asyncio.run(go()) == 3
