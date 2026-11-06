"""`puxti scan --if-changed` — skip the scan when no connector's inputs changed.

Runs the real CLI against a real dbt connector and a real SQLite graph; only
the LLM-backed scanner is mocked, standing in for a scan that writes entities."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

from puxti.cli import app
from puxti.cli.scan import EXIT_RESCAN_NEEDED
from puxti.connectors.dbt import DbtConnector
from puxti.connectors.sql_views import SqlViewsConnector
from puxti.core.graph import SqliteKnowledgeGraph
from puxti.core.scanner import ScanResult
from puxti.models import Entity, EntityType
from tests.cli._helpers import plain, runner

MANIFEST = {
    "metadata": {"project_name": "shop", "generated_at": "2026-01-01T00:00:00Z"},
    "nodes": {
        "model.shop.orders": {
            "resource_type": "model",
            "name": "orders",
            "original_file_path": "models/orders.sql",
            "columns": {},
            "depends_on": {"nodes": []},
        },
    },
    "sources": {},
}


class Harness:
    """A dbt project on disk, a SQLite graph file, and a mocked scanner that
    upserts one entity per scan (as a real scan would)."""

    def __init__(self, tmp_path):
        self.project = tmp_path / "shop"
        (self.project / "target").mkdir(parents=True)
        (self.project / "models").mkdir()
        self.write_manifest(MANIFEST)
        (self.project / "models" / "orders.sql").write_text("select 1 as order_id")
        self.db_path = tmp_path / "graph.db"
        self.scans = 0
        self.scan_error: Exception | None = None
        self.truncated = 0
        self.failed = 0
        self.connectors = lambda: [DbtConnector({"project_dir": str(self.project)})]

    def write_manifest(self, manifest: dict) -> None:
        (self.project / "target" / "manifest.json").write_text(json.dumps(manifest))

    async def _fake_scan(self, connector, graph, **_kwargs) -> ScanResult:
        if self.scan_error is not None:
            raise self.scan_error
        self.scans += 1
        await graph.upsert_entity(Entity(
            id="model.shop.orders", name="orders", type=EntityType.MODEL,
            source_connector="dbt", project="shop",
        ))
        return ScanResult(
            entities_upserted=1, lineage_edges_upserted=0,
            definitions_written=0, semantic_edges_written=0,
            edge_batches_truncated=self.truncated,
            edge_batches_failed=self.failed,
        )

    def invoke(self, *args: str):
        scanner = MagicMock()
        scanner.scan = AsyncMock(side_effect=self._fake_scan)
        scanner.estimate_scan_cost = AsyncMock(return_value={
            "models": 1, "entities_total": 1, "tokens_exact": True,
            "estimated_cost_usd": 0.01, "def_input_tokens": 1, "def_est_output_tokens": 1,
            "edges_input_tokens": 1, "edges_est_output_tokens": 1,
            "total_input_tokens": 2, "total_est_output_tokens": 2,
        })
        with (
            patch("puxti.cli.scan.KnowledgeGraph",
                  side_effect=lambda: SqliteKnowledgeGraph(self.db_path)),
            patch("puxti.cli.scan.SemanticScanner", return_value=scanner),
            patch("puxti.cli.scan.build_configured_connectors",
                  side_effect=lambda *_a, **_k: self.connectors()),
        ):
            result = runner.invoke(app, ["scan", *args])
        self.estimates = scanner.estimate_scan_cost.await_count
        return result

    def purge(self, project: str) -> None:
        async def go():
            kg = SqliteKnowledgeGraph(self.db_path)
            await kg.connect()
            await kg.purge_project(project)
            await kg.close()
        asyncio.run(go())


def test_help_lists_if_changed():
    result = runner.invoke(app, ["scan", "--help"])
    assert "--if-changed" in plain(result.output)


def test_second_run_on_unchanged_project_exits_0_without_scanning(tmp_path):
    h = Harness(tmp_path)
    first = h.invoke("--if-changed")
    assert first.exit_code == 0, first.output
    assert h.scans == 1
    assert "Knowledge Graph is empty" in plain(first.output)

    second = h.invoke("--if-changed")
    assert second.exit_code == 0, second.output
    assert h.scans == 1
    assert "No changes since the last scan" in plain(second.output)


def test_plain_scan_records_the_baseline(tmp_path):
    h = Harness(tmp_path)
    assert h.invoke().exit_code == 0
    assert h.invoke("--if-changed").exit_code == 0
    assert h.scans == 1


def test_model_sql_edit_triggers_rescan(tmp_path):
    h = Harness(tmp_path)
    h.invoke()
    (h.project / "models" / "orders.sql").write_text("select 2 as order_id")
    result = h.invoke("--if-changed")
    assert result.exit_code == 0, result.output
    assert h.scans == 2
    assert "changed since the last scan" in plain(result.output)


def test_reparse_with_only_new_timestamps_still_skips(tmp_path):
    h = Harness(tmp_path)
    h.invoke()
    h.write_manifest({**MANIFEST, "metadata": {**MANIFEST["metadata"],
                                               "generated_at": "2026-03-03T00:00:00Z"}})
    assert h.invoke("--if-changed").exit_code == 0
    assert h.scans == 1


def test_failed_scan_records_nothing(tmp_path):
    h = Harness(tmp_path)
    h.invoke()
    (h.project / "models" / "orders.sql").write_text("select 2 as order_id")
    h.scan_error = RuntimeError("boom")
    assert h.invoke("--if-changed").exit_code == 1
    h.scan_error = None
    # Had the failed scan recorded the new fingerprint, this would skip.
    assert h.invoke("--if-changed").exit_code == 0
    assert h.scans == 2


def test_unfingerprintable_connector_always_rescans(tmp_path):
    h = Harness(tmp_path)
    h.invoke()

    def blind():
        connector = DbtConnector({"project_dir": str(h.project)})
        connector.source_fingerprint = lambda: None
        return [connector]

    h.connectors = blind
    result = h.invoke("--if-changed")
    assert h.scans == 2
    assert "cannot be fingerprinted" in plain(result.output)


def test_purged_project_rescans(tmp_path):
    h = Harness(tmp_path)
    h.invoke()
    h.purge("shop")
    h.invoke("--if-changed")
    assert h.scans == 2


def test_dry_run_exits_3_when_rescan_needed_and_records_nothing(tmp_path):
    h = Harness(tmp_path)
    result = h.invoke("--if-changed", "--dry-run")
    assert result.exit_code == EXIT_RESCAN_NEEDED, result.output
    assert h.estimates == 1
    assert h.scans == 0
    # Still stale: a dry run never records a scan run.
    assert h.invoke("--if-changed", "--dry-run").exit_code == EXIT_RESCAN_NEEDED


def test_dry_run_exits_0_without_estimating_when_unchanged(tmp_path):
    h = Harness(tmp_path)
    h.invoke()
    result = h.invoke("--if-changed", "--dry-run")
    assert result.exit_code == 0, result.output
    assert h.estimates == 0


def test_dry_run_without_if_changed_keeps_exit_0(tmp_path):
    h = Harness(tmp_path)
    assert h.invoke("--dry-run").exit_code == 0


def test_truncated_edge_proposals_are_not_recorded(tmp_path):
    h = Harness(tmp_path)
    h.truncated = 1
    h.invoke()
    h.truncated = 0
    # The scanner asked for a re-run; --if-changed must not skip it.
    h.invoke("--if-changed")
    assert h.scans == 2
    assert h.invoke("--if-changed").exit_code == 0
    assert h.scans == 2


def test_connector_config_change_triggers_rescan(tmp_path):
    h = Harness(tmp_path)
    views = tmp_path / "views"
    views.mkdir()
    (views / "v.sql").write_text("create view v as select 1 as a")
    schema = {"value": "public"}
    h.connectors = lambda: [SqlViewsConnector({
        "project_dir": str(tmp_path), "views_dir": "views",
        "default_schema": schema["value"],
    })]
    h.invoke()
    h.invoke("--if-changed")
    assert h.scans == 1
    schema["value"] = "analytics"
    h.invoke("--if-changed")
    assert h.scans == 2


def test_puxti_upgrade_triggers_rescan(tmp_path):
    h = Harness(tmp_path)
    h.invoke()
    with patch("puxti.cli.scan.__version__", "99.0.0"):
        h.invoke("--if-changed")
    assert h.scans == 2


def test_fingerprint_error_is_logged_and_rescans(tmp_path, caplog):
    h = Harness(tmp_path)
    h.invoke()

    def broken():
        connector = DbtConnector({"project_dir": str(h.project)})
        connector.source_fingerprint = MagicMock(side_effect=KeyError("original_file_path"))
        return [connector]

    h.connectors = broken
    with caplog.at_level("WARNING", logger="puxti.cli.scan"):
        h.invoke("--if-changed")
    assert h.scans == 2
    assert "Cannot fingerprint dbt inputs" in caplog.text


def test_plain_dry_run_does_not_fingerprint(tmp_path):
    h = Harness(tmp_path)
    connector = DbtConnector({"project_dir": str(h.project)})
    connector.source_fingerprint = MagicMock(return_value=None)
    h.connectors = lambda: [connector]
    assert h.invoke("--dry-run").exit_code == 0
    connector.source_fingerprint.assert_not_called()


def test_truncation_over_a_matching_baseline_drops_it(tmp_path):
    h = Harness(tmp_path)
    h.invoke()
    h.truncated = 1
    h.invoke()  # unchanged inputs, but this scan came back partial
    h.truncated = 0
    h.invoke("--if-changed")
    assert h.scans == 3


def test_skipped_edge_batches_are_not_recorded(tmp_path):
    h = Harness(tmp_path)
    h.invoke()
    h.failed = 1
    h.invoke()
    h.failed = 0
    h.invoke("--if-changed")
    assert h.scans == 3


def test_scanning_connectors_together_after_separately_rescans(tmp_path):
    h = Harness(tmp_path)
    views = tmp_path / "views"
    views.mkdir()
    (views / "v.sql").write_text("create view v as select 1 as a")
    dbt = lambda: DbtConnector({"project_dir": str(h.project)})  # noqa: E731
    sql = lambda: SqlViewsConnector({"project_dir": str(tmp_path), "views_dir": "views"})  # noqa: E731
    h.connectors = lambda: [dbt()]
    h.invoke()
    h.connectors = lambda: [sql()]
    h.invoke()
    h.connectors = lambda: [dbt(), sql()]
    h.invoke("--if-changed")
    assert h.scans == 4  # both connectors, scanned together this time


def test_dry_run_check_does_not_create_the_graph(tmp_path):
    h = Harness(tmp_path)
    result = h.invoke("--if-changed", "--dry-run")
    assert result.exit_code == EXIT_RESCAN_NEEDED, result.output
    assert not h.db_path.exists()


def _second_checkout(h: Harness, tmp_path) -> DbtConnector:
    """Checkout B: a copy of the same dbt project (same entity IDs) elsewhere."""
    import shutil
    other = tmp_path / "shop-b"
    shutil.copytree(h.project, other)
    return DbtConnector({"project_dir": str(other)})


def test_incomplete_scan_of_another_checkout_invalidates_the_first(tmp_path):
    h = Harness(tmp_path)
    checkout_a = h.connectors
    h.invoke()  # A recorded
    checkout_b = _second_checkout(h, tmp_path)
    h.connectors = lambda: [checkout_b]
    h.truncated = 1
    h.invoke()  # B wrote entities but came back incomplete
    h.truncated = 0
    h.connectors = checkout_a
    h.invoke("--if-changed")
    assert h.scans == 3


def test_failed_scan_of_another_checkout_invalidates_the_first(tmp_path):
    h = Harness(tmp_path)
    checkout_a = h.connectors
    h.invoke()
    checkout_b = _second_checkout(h, tmp_path)
    h.connectors = lambda: [checkout_b]
    h.scan_error = RuntimeError("boom after writes")
    assert h.invoke().exit_code == 1
    h.scan_error = None
    h.connectors = checkout_a
    h.invoke("--if-changed")
    assert h.scans == 2
