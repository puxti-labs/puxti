"""`puxti validate` against a dbt project: models, columns, types, metrics, offline."""

import json
from pathlib import Path

import pytest

from puxti.core.bi_validate import resolve_dirs, validate
from puxti.workspace import ConnectorConfig, WorkspaceConfig

HEAD = "apiVersion: puxti/v1alpha1\n"

DATASET = HEAD + """kind: dataset
name: orders
model: orders
columns: [order_date, status]
metrics:
  - {name: revenue, agg: sum, column: amount}
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

MODEL = {
    "resource_type": "model",
    "name": "orders",
    "package_name": "shop",
    "columns": {"amount": {"data_type": "numeric"}, "order_date": {}, "status": {}},
}

CATALOG_COLUMNS = {
    "AMOUNT": {"type": "NUMBER(38,2)"},
    "ORDER_DATE": {"type": "DATE"},
    "STATUS": {"type": "VARCHAR"},
}


def _project(
    tmp_path: Path,
    specs: dict[str, str] | None = None,
    nodes: dict | None = None,
    metrics: dict | None = None,
    catalog: dict | None | str = "default",
) -> Path:
    project = tmp_path / "shop"
    files = {"bi/orders.yml": DATASET, "bi/chart.yml": CHART}
    files.update(specs or {})
    for rel, text in files.items():
        path = project / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    manifest = {
        "nodes": nodes if nodes is not None else {"model.shop.orders": MODEL},
        "metrics": metrics
        if metrics is not None
        else {"metric.shop.arr": {"name": "arr", "type": "simple"}},
    }
    (project / "target").mkdir(exist_ok=True)
    (project / "target" / "manifest.json").write_text(json.dumps(manifest))
    if catalog == "default":
        catalog = {"nodes": {"model.shop.orders": {"columns": CATALOG_COLUMNS}}}
    if catalog is not None:
        (project / "target" / "catalog.json").write_text(json.dumps(catalog))
    return project


def _findings(project: Path) -> list[tuple[str, int | None, str, str]]:
    report = validate(project / "bi", project)
    return [(f.path.name, f.line, f.severity, f.message) for f in report.findings]


def test_valid_project_with_a_catalog_has_no_findings(tmp_path: Path) -> None:
    report = validate(_project(tmp_path) / "bi", _project(tmp_path))
    assert report.findings == [] and report.to_dict()["ok"]


def test_missing_model_is_an_error_at_its_line(tmp_path: Path) -> None:
    project = _project(tmp_path, nodes={})
    assert ("orders.yml", 4, "error", "dbt model 'orders' does not exist") in _findings(project)


def test_versioned_model_resolves_to_its_latest_version(tmp_path: Path) -> None:
    v1 = {**MODEL, "version": 1, "latest_version": 2}
    v2 = {**MODEL, "version": 2, "latest_version": 2}
    nodes = {"model.shop.orders.v1": v1, "model.shop.orders.v2": v2}
    catalog = {"nodes": {"model.shop.orders.v2": {"columns": CATALOG_COLUMNS}}}
    assert _findings(_project(tmp_path, nodes=nodes, catalog=catalog)) == []


def test_model_name_in_two_packages_is_ambiguous(tmp_path: Path) -> None:
    nodes = {
        "model.shop.orders": MODEL,
        "model.other.orders": {**MODEL, "package_name": "other"},
    }
    (finding,) = [f for f in _findings(_project(tmp_path, nodes=nodes)) if f[0] == "orders.yml"]
    assert finding[2] == "error" and "ambiguous" in finding[3]
    assert "model.other.orders" in finding[3] and "model.shop.orders" in finding[3]


OPEN_DATASET = DATASET.replace("columns: [order_date, status]\n", "")  # exposes every column


def test_column_missing_from_the_catalog_is_an_error(tmp_path: Path) -> None:
    chart = CHART.replace("group_by: [status]", "group_by: [region]")
    specs = {"bi/chart.yml": chart, "bi/orders.yml": OPEN_DATASET}
    assert _findings(_project(tmp_path, specs=specs)) == [
        ("chart.yml", 8, "error", "column 'region' is not in dbt model 'orders'")
    ]


def test_without_a_catalog_an_undeclared_column_is_a_warning(tmp_path: Path) -> None:
    chart = CHART.replace("group_by: [status]", "group_by: [region]")
    specs = {"bi/chart.yml": chart, "bi/orders.yml": OPEN_DATASET}
    findings = _findings(_project(tmp_path, specs=specs, catalog=None))
    (region,) = [f for f in findings if f[1] == 8]
    assert region[:3] == ("chart.yml", 8, "warning")
    assert "not declared in the YAML" in region[3]
    # amount is declared numeric; order_date has no declared type, so grain is a warning.
    assert any("grain `month` was not checked" in f[3] for f in findings)
    assert all(f[2] == "warning" for f in findings)


def test_unknown_columns_warn_once_per_file(tmp_path: Path) -> None:
    nodes = {"model.shop.orders": {**MODEL, "columns": {}}}
    specs = {"bi/orders.yml": OPEN_DATASET}
    findings = _findings(_project(tmp_path, specs=specs, nodes=nodes, catalog=None))
    assert [(f[0], f[2]) for f in findings] == [("chart.yml", "warning"), ("orders.yml", "warning")]
    assert all("columns of dbt model 'orders' are unknown" in f[3] for f in findings)


def test_sum_on_a_text_column_is_an_error(tmp_path: Path) -> None:
    dataset = DATASET.replace("column: amount", "column: status")
    findings = _findings(_project(tmp_path, specs={"bi/orders.yml": dataset}))
    assert ("orders.yml", 7, "error", "`sum` needs a numeric column; 'status' is VARCHAR") in (
        findings
    )


def test_grain_on_a_non_date_column_is_an_error(tmp_path: Path) -> None:
    chart = CHART.replace("column: order_date", "column: status").replace(
        "columns: [order_date, status]", "columns: [status]"
    )
    dataset = DATASET.replace("columns: [order_date, status]", "columns: [status, order_date]")
    findings = _findings(_project(tmp_path, specs={"bi/chart.yml": chart, "bi/orders.yml": dataset}))
    assert (
        "chart.yml",
        6,
        "error",
        "grain `month` needs a date or time column; 'status' is VARCHAR",
    ) in findings


@pytest.mark.parametrize(
    ("metrics", "message"),
    [
        ({}, "dbt metric 'arr' does not exist"),
        (
            {"metric.shop.arr": {"name": "arr", "type": "ratio"}},
            "dbt metric 'arr' is a ratio metric; v1alpha1 supports simple metrics only",
        ),
    ],
)
def test_dbt_metric_must_exist_and_be_simple(tmp_path: Path, metrics: dict, message: str) -> None:
    assert ("orders.yml", 8, "error", message) in _findings(_project(tmp_path, metrics=metrics))


def test_missing_manifest_is_an_error_and_spec_checks_still_run(tmp_path: Path) -> None:
    chart = CHART.replace("dataset: orders", "dataset: nope")
    project = _project(tmp_path, specs={"bi/chart.yml": chart})
    (project / "target" / "manifest.json").unlink()
    findings = _findings(project)
    assert ("chart.yml", 4, "error", "dataset 'nope' does not exist") in findings
    assert any(f[0] == "manifest.json" and "run `dbt parse`" in f[3] for f in findings)


def test_unreadable_catalog_is_a_warning(tmp_path: Path) -> None:
    project = _project(tmp_path)
    (project / "target" / "catalog.json").write_text("{not json")
    findings = _findings(project)
    assert any(f[0] == "catalog.json" and f[2] == "warning" for f in findings)


def test_missing_spec_folder_is_an_error(tmp_path: Path) -> None:
    report = validate(tmp_path / "nope", None)
    assert report.errors == 1 and "spec folder not found" in report.findings[0].message


def test_resolve_dirs_prefers_flags_then_workspace(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("puxti.core.bi_validate.settings.dbt_project_dir", "/env/dbt")
    ws = WorkspaceConfig(
        dbt=ConnectorConfig(project_dir="/ws/dbt"),
        bi=ConnectorConfig(project_dir="/ws/dbt", extras={"specs_dir": "specs"}),
    )
    assert resolve_dirs(ws) == (Path("/ws/dbt/specs"), Path("/ws/dbt"))
    assert resolve_dirs(ws, "/flag/bi", "/flag/dbt") == (Path("/flag/bi"), Path("/flag/dbt"))
    assert resolve_dirs(WorkspaceConfig()) == (Path("/env/dbt/bi"), Path("/env/dbt"))


def test_chart_column_is_reported_once_when_the_dataset_lists_columns(tmp_path: Path) -> None:
    """The chart is checked against the dataset's list, the list against dbt: one finding."""
    chart = CHART.replace("group_by: [status]", "group_by: [region]")
    assert _findings(_project(tmp_path, specs={"bi/chart.yml": chart})) == [
        ("chart.yml", 8, "error", "column 'region' is not in the columns of dataset 'orders'")
    ]


# ── Review fixes ──────────────────────────────────────────────────────────────


def test_schema_qualified_model_matches_the_model_in_that_schema(tmp_path: Path) -> None:
    dataset = DATASET.replace("model: orders", "model: analytics.orders")
    nodes = {"model.shop.orders": {**MODEL, "schema": "analytics"}}
    assert _findings(_project(tmp_path, specs={"bi/orders.yml": dataset}, nodes=nodes)) == []
    wrong = DATASET.replace("model: orders", "model: staging.orders")
    findings = _findings(_project(tmp_path, specs={"bi/orders.yml": wrong}, nodes=nodes))
    assert ("orders.yml", 4, "error", "dbt model 'staging.orders' does not exist") in findings


def test_column_declared_in_yaml_but_not_yet_built_is_a_warning(tmp_path: Path) -> None:
    """A PR adds `region` to the model and its YAML; the catalog predates the rebuild."""
    declared = {**MODEL["columns"], "region": {}}
    nodes = {"model.shop.orders": {**MODEL, "columns": declared}}
    chart = CHART.replace("group_by: [status]", "group_by: [region]")
    specs = {"bi/chart.yml": chart, "bi/orders.yml": OPEN_DATASET}
    findings = _findings(_project(tmp_path, specs=specs, nodes=nodes))
    assert len(findings) == 1 and findings[0][:3] == ("chart.yml", 8, "warning")
    assert "declared in the YAML" in findings[0][3] and "not in catalog.json" in findings[0][3]


def test_catalog_without_an_entry_for_the_model_says_so(tmp_path: Path) -> None:
    nodes = {"model.shop.orders": {**MODEL, "columns": {}}}
    project = _project(tmp_path, nodes=nodes, catalog={"nodes": {}})
    (finding,) = _findings(project)
    assert "catalog.json has no entry for it" in finding[3]
    assert "run `dbt docs generate`" not in finding[3]


@pytest.mark.parametrize(
    ("name", "content"),
    [("manifest.json", "[]"), ("manifest.json", '{"nodes": null, "metrics": [1]}')],
)
def test_manifest_of_the_wrong_shape_does_not_crash(tmp_path: Path, name: str, content: str) -> None:
    project = _project(tmp_path)
    (project / "target" / name).write_text(content)
    report = validate(project / "bi", project)
    assert report.errors >= 1  # a finding, never an exception


@pytest.mark.parametrize("content", ["[]", "null", '{"nodes": []}', '{"nodes": {"x": 3}}'])
def test_catalog_of_the_wrong_shape_does_not_crash(tmp_path: Path, content: str) -> None:
    project = _project(tmp_path)
    (project / "target" / "catalog.json").write_text(content)
    validate(project / "bi", project)  # must return a report


@pytest.mark.parametrize(
    ("dtype", "numeric", "temporal"),
    [
        ("NUMBER(38,2)", True, False),
        ("INT64", True, False),
        ("double precision", True, False),
        ("Nullable(Float64)", True, False),
        ("LowCardinality(Nullable(Int32))", True, False),
        ("Nullable(DateTime64(3))", False, True),
        ("timestamp with time zone", False, True),
        ("TIMESTAMP_NTZ", False, True),
        ("DATE", False, True),
        ("ARRAY<INT64>", False, False),
        ("STRUCT<a INT64>", False, False),
        ("bigint[]", False, False),
        ("interval", False, False),
        ("point", False, False),
        ("VARCHAR(255)", False, False),
    ],
)
def test_type_classification(dtype: str, numeric: bool, temporal: bool) -> None:
    from puxti.core.bi_validate import _is_numeric, _is_temporal

    assert (_is_numeric(dtype), _is_temporal(dtype)) == (numeric, temporal)


# ── CodeRabbit review on #99 ──────────────────────────────────────────────────


def test_artifacts_that_cannot_be_read_are_findings(tmp_path: Path) -> None:
    from puxti.core.bi_validate import _load_json

    assert _load_json(tmp_path / "missing.json") == (None, None)
    data, problem = _load_json(tmp_path)  # a directory: an OSError other than not-found
    assert data is None and problem.startswith("is unreadable")


def test_malformed_manifest_fields_never_raise(tmp_path: Path) -> None:
    node = {**MODEL, "unique_id": [], "package_name": [], "columns": {"amount": {"data_type": []}}}
    nodes = {"model.shop.orders": node, "model.shop.orders_2": {**node, "name": "orders"}}
    report = validate(_project(tmp_path, nodes=nodes) / "bi", tmp_path / "shop")
    assert report.findings  # ambiguous model or otherwise: findings, not an exception


def test_catalog_lookup_uses_the_manifest_key(tmp_path: Path) -> None:
    nodes = {"model.shop.orders": {**MODEL, "unique_id": ["not", "a", "key"]}}
    assert _findings(_project(tmp_path, nodes=nodes)) == []


def test_malformed_catalog_columns_fall_back_to_yaml_with_a_warning(tmp_path: Path) -> None:
    catalog = {"nodes": {"model.shop.orders": {"columns": []}}}
    findings = _findings(_project(tmp_path, catalog=catalog))
    assert all(f[2] == "warning" for f in findings)  # no "not in dbt model" errors
    assert any(f[0] == "catalog.json" and "no 'columns' object" in f[3] for f in findings)


def test_malformed_catalog_nodes_section_is_reported(tmp_path: Path) -> None:
    findings = _findings(_project(tmp_path, catalog={"nodes": []}))
    assert ("catalog.json", None, "warning", "'nodes' is not a JSON object, so it was ignored") in (
        findings
    )


def test_malformed_catalog_type_keeps_the_yaml_type(tmp_path: Path) -> None:
    columns = {**CATALOG_COLUMNS, "AMOUNT": {"type": ["NUMERIC"]}}
    catalog = {"nodes": {"model.shop.orders": {"columns": columns}}}
    assert _findings(_project(tmp_path, catalog=catalog)) == []  # YAML says amount is numeric


def test_schema_match_on_a_version_that_is_not_the_latest_is_an_error(tmp_path: Path) -> None:
    v1 = {**MODEL, "schema": "analytics", "version": 1, "latest_version": 2}
    v2 = {**MODEL, "schema": "marts", "version": 2, "latest_version": 2}
    nodes = {"model.shop.orders.v1": v1, "model.shop.orders.v2": v2}
    dataset = DATASET.replace("model: orders", "model: analytics.orders")
    findings = _findings(_project(tmp_path, specs={"bi/orders.yml": dataset}, nodes=nodes))
    (finding,) = [f for f in findings if f[1] == 4]
    assert finding[2] == "error" and "not its latest version 2" in finding[3]


def test_dbt_metric_is_checked_even_when_the_model_is_missing(tmp_path: Path) -> None:
    findings = _findings(_project(tmp_path, nodes={}, metrics={}))
    messages = {f[3] for f in findings}
    assert "dbt model 'orders' does not exist" in messages
    assert "dbt metric 'arr' does not exist" in messages


def test_dbt_metric_without_a_type_is_a_warning(tmp_path: Path) -> None:
    findings = _findings(_project(tmp_path, metrics={"metric.shop.arr": {"name": "arr"}}))
    assert [f[:3] for f in findings] == [("orders.yml", 8, "warning")]
    assert "records no type" in findings[0][3]


@pytest.mark.parametrize(
    ("dtype", "numeric"), [("integer", True), ("integer ARRAY", False), ("integer[]", False)]
)
def test_sql_array_spellings_are_not_numeric(dtype: str, numeric: bool) -> None:
    from puxti.core.bi_validate import _is_numeric

    assert _is_numeric(dtype) is numeric
