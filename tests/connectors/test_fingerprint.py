"""Source fingerprints for `puxti scan --if-changed`.

A fingerprint must change whenever a scan would read something different, and
stay put when only per-invocation noise changed. A false "unchanged" skips a
needed rescan, so the change cases matter most."""

import copy
import json
from pathlib import Path

from puxti.connectors.airflow import AirflowConnector
from puxti.connectors.base import fingerprint_files
from puxti.connectors.dbt import DbtConnector
from puxti.connectors.prisma import PrismaConnector
from puxti.connectors.sql_views import SqlViewsConnector

MANIFEST = {
    "metadata": {
        "project_name": "shop",
        "generated_at": "2026-01-01T00:00:00Z",
        "invocation_id": "aaaa",
        "invocation_started_at": "2026-01-01T00:00:00Z",
        "run_started_at": "2026-01-01T00:00:00Z",
        "adapter_type": "duckdb",
    },
    "nodes": {
        "model.shop.orders": {
            "resource_type": "model",
            "name": "orders",
            "original_file_path": "models/orders.sql",
            "created_at": 1700000000.0,
            "columns": {"created_at": {"name": "created_at", "description": "When placed"}},
            "depends_on": {"nodes": []},
        },
    },
    "sources": {},
    "disabled": {"model.shop.old": [{"name": "old", "created_at": 1700000000.0}]},
}


def _dbt_project(tmp_path: Path, manifest: dict = MANIFEST, sql: str = "select 1") -> Path:
    (tmp_path / "target").mkdir(exist_ok=True)
    (tmp_path / "target" / "manifest.json").write_text(json.dumps(manifest))
    (tmp_path / "models").mkdir(exist_ok=True)
    (tmp_path / "models" / "orders.sql").write_text(sql)
    return tmp_path


def _dbt_digest(project: Path) -> str:
    fp = DbtConnector({"project_dir": str(project)}).source_fingerprint()
    assert fp is not None
    return fp.digest


# ── dbt ───────────────────────────────────────────────────────────────────────

def test_dbt_fingerprint_source_is_resolved_manifest_path(tmp_path):
    project = _dbt_project(tmp_path)
    fp = DbtConnector({"project_dir": str(project)}).source_fingerprint()
    assert fp.source == str((project / "target" / "manifest.json").resolve())


def test_dbt_fingerprint_ignores_reparse_timestamps(tmp_path):
    project = _dbt_project(tmp_path)
    before = _dbt_digest(project)

    reparsed = copy.deepcopy(MANIFEST)
    reparsed["metadata"].update(
        generated_at="2026-02-02T00:00:00Z", invocation_id="bbbb",
        invocation_started_at="2026-02-02T00:00:00Z", run_started_at="2026-02-02T00:00:00Z",
    )
    reparsed["nodes"]["model.shop.orders"]["created_at"] = 1800000000.0
    reparsed["disabled"]["model.shop.old"][0]["created_at"] = 1800000000.0
    _dbt_project(tmp_path, reparsed)

    assert _dbt_digest(project) == before


def test_dbt_fingerprint_ignores_compile_output(tmp_path):
    project = _dbt_project(tmp_path)
    before = _dbt_digest(project)

    compiled = copy.deepcopy(MANIFEST)
    compiled["nodes"]["model.shop.orders"].update(
        compiled=True, compiled_code="select 1", compiled_path="target/compiled/orders.sql",
        extra_ctes=[], extra_ctes_injected=True,
    )
    _dbt_project(tmp_path, compiled)

    assert _dbt_digest(project) == before


def test_dbt_fingerprint_keeps_a_column_named_created_at(tmp_path):
    project = _dbt_project(tmp_path)
    before = _dbt_digest(project)

    changed = copy.deepcopy(MANIFEST)
    changed["nodes"]["model.shop.orders"]["columns"]["created_at"]["description"] = "Changed"
    _dbt_project(tmp_path, changed)

    assert _dbt_digest(project) != before


def test_dbt_fingerprint_changes_with_manifest_content(tmp_path):
    project = _dbt_project(tmp_path)
    before = _dbt_digest(project)

    changed = copy.deepcopy(MANIFEST)
    changed["nodes"]["model.shop.orders"]["depends_on"]["nodes"] = ["source.shop.raw.orders"]
    _dbt_project(tmp_path, changed)

    assert _dbt_digest(project) != before


def test_dbt_fingerprint_changes_with_model_sql_not_yet_reparsed(tmp_path):
    project = _dbt_project(tmp_path)
    before = _dbt_digest(project)
    (project / "models" / "orders.sql").write_text("select 2")
    assert _dbt_digest(project) != before


def test_dbt_fingerprint_none_without_manifest(tmp_path):
    assert DbtConnector({"project_dir": str(tmp_path)}).source_fingerprint() is None


def test_dbt_fingerprint_none_for_unreadable_manifest(tmp_path):
    (tmp_path / "target").mkdir()
    (tmp_path / "target" / "manifest.json").write_text("{not json")
    assert DbtConnector({"project_dir": str(tmp_path)}).source_fingerprint() is None


# ── sql_views ─────────────────────────────────────────────────────────────────

def _views_digest(project: Path) -> str:
    fp = SqlViewsConnector({"project_dir": str(project), "views_dir": "views"}).source_fingerprint()
    assert fp is not None
    return fp.digest


def test_sql_views_fingerprint_tracks_files_under_views_dir(tmp_path):
    views = tmp_path / "views" / "nested"
    views.mkdir(parents=True)
    (views / "a.sql").write_text("create view a as select 1")
    before = _views_digest(tmp_path)

    assert _views_digest(tmp_path) == before
    (views / "b.sql").write_text("create view b as select 1")
    added = _views_digest(tmp_path)
    assert added != before

    (views / "b.sql").rename(views / "c.sql")
    assert _views_digest(tmp_path) != added


def test_sql_views_fingerprint_none_without_views_dir(tmp_path):
    connector = SqlViewsConnector({"project_dir": str(tmp_path), "views_dir": "missing"})
    assert connector.source_fingerprint() is None


# ── prisma ────────────────────────────────────────────────────────────────────

def test_prisma_fingerprint_tracks_schema(tmp_path):
    schema = tmp_path / "prisma" / "schema.prisma"
    schema.parent.mkdir()
    schema.write_text("model User {\n  id Int @id\n}\n")
    connector = PrismaConnector({"project_dir": str(tmp_path)})
    before = connector.source_fingerprint()
    assert before.source == str(schema.resolve())

    schema.write_text("model User {\n  id Int @id\n  email String\n}\n")
    assert connector.source_fingerprint().digest != before.digest


def test_prisma_fingerprint_none_without_schema(tmp_path):
    assert PrismaConnector({"project_dir": str(tmp_path)}).source_fingerprint() is None


# ── airflow ───────────────────────────────────────────────────────────────────

def test_airflow_fingerprint_tracks_dag_files(tmp_path):
    (tmp_path / "etl.py").write_text("# dag")
    connector = AirflowConnector({"dags_dir": str(tmp_path)})
    before = connector.source_fingerprint().digest
    (tmp_path / "etl.py").write_text("# dag v2")
    assert connector.source_fingerprint().digest != before


# ── helper ────────────────────────────────────────────────────────────────────

def test_fingerprint_files_none_when_a_file_is_missing(tmp_path):
    assert fingerprint_files(tmp_path, [tmp_path / "gone.sql"]) is None
