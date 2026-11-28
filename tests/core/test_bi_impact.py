"""`puxti impact --state`: BI specs a dbt change breaks or affects, between two builds."""

import copy
import json
from pathlib import Path

from puxti.core.bi_impact import impact_against_state

HEAD = "apiVersion: puxti/v1alpha1\n"

DATASET = (
    HEAD
    + """kind: dataset
name: orders
model: orders
metrics:
  - {name: revenue, agg: sum, column: amount}
"""
)

CHART = (
    HEAD
    + """kind: chart
name: revenue_by_month
dataset: orders
type: line
x: {column: order_date, grain: month}
metrics: [revenue]
"""
)

DASHBOARD = HEAD + "kind: dashboard\nname: sales\nrows:\n  - charts: [revenue_by_month]\n"

ORDERS = {
    "resource_type": "model",
    "name": "orders",
    "package_name": "shop",
    "checksum": {"name": "sha256", "checksum": "a"},
    "depends_on": {"nodes": ["model.shop.stg_orders"]},
    "columns": {},
}
STG = {
    "resource_type": "model",
    "name": "stg_orders",
    "package_name": "shop",
    "checksum": {"name": "sha256", "checksum": "s"},
    "depends_on": {"nodes": []},
    "columns": {},
}
CATALOG = {
    "model.shop.orders": {
        "columns": {"AMOUNT": {"type": "NUMBER"}, "ORDER_DATE": {"type": "DATE"}}
    },
    "model.shop.stg_orders": {"columns": {"ID": {"type": "NUMBER"}}},
}
NODES = {"model.shop.orders": ORDERS, "model.shop.stg_orders": STG}


def _write_target(folder: Path, nodes: dict, catalog: dict | None) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "manifest.json").write_text(json.dumps({"nodes": nodes, "metrics": {}}))
    if catalog is not None:
        (folder / "catalog.json").write_text(json.dumps({"nodes": catalog}))


def _run(
    tmp_path: Path,
    nodes: dict | None = None,
    catalog: dict | None | str = "default",
    base_nodes: dict | None = None,
    base_catalog: dict | None | str = "default",
    specs: dict[str, str] | None = None,
):
    project = tmp_path / "shop"
    files = {"bi/orders.yml": DATASET, "bi/chart.yml": CHART, "bi/dash.yml": DASHBOARD}
    files.update(specs or {})
    for rel, text in files.items():
        (project / rel).parent.mkdir(parents=True, exist_ok=True)
        (project / rel).write_text(text)
    _write_target(
        tmp_path / "prod",
        base_nodes if base_nodes is not None else NODES,
        CATALOG if base_catalog == "default" else base_catalog,
    )
    _write_target(
        project / "target",
        nodes if nodes is not None else NODES,
        CATALOG if catalog == "default" else catalog,
    )
    return impact_against_state(project / "bi", project, tmp_path / "prod")


def _assets(report) -> list[tuple[str, str, str]]:
    return [(a.kind, a.name, a.status) for a in report.assets]


def _with(node_id: str, **changes) -> dict:
    nodes = copy.deepcopy(NODES)
    nodes[node_id].update(changes)
    return nodes


def test_identical_builds_affect_nothing(tmp_path: Path) -> None:
    report = _run(tmp_path)
    assert report.ok and report.assets == [] and report.changed_models == []
    assert report.findings == []


def test_removed_column_breaks_the_dataset_and_everything_built_on_it(tmp_path: Path) -> None:
    catalog = copy.deepcopy(CATALOG)
    del catalog["model.shop.orders"]["columns"]["AMOUNT"]
    report = _run(tmp_path, catalog=catalog)
    assert not report.ok
    assert _assets(report) == [
        ("dataset", "orders", "broken"),
        ("chart", "revenue_by_month", "broken"),
        ("dashboard", "sales", "broken"),
    ]
    assert [c.model_dump() for c in report.changed_models] == [
        {"unique_id": "model.shop.orders", "changes": ["column 'amount' removed"]}
    ]
    (error,) = [f for f in report.findings if f.severity == "error"]
    assert (error.path.name, error.line) == ("orders.yml", 6)
    assert error.message == "column 'amount' is not in dbt model 'orders'"
    assert report.assets[1].reasons == ["dataset 'orders' is broken"]


def test_sql_change_upstream_marks_assets_affected_not_broken(tmp_path: Path) -> None:
    report = _run(tmp_path, nodes=_with("model.shop.stg_orders", checksum={"checksum": "s2"}))
    assert report.ok
    assert _assets(report) == [
        ("dataset", "orders", "affected"),
        ("chart", "revenue_by_month", "affected"),
        ("dashboard", "sales", "affected"),
    ]
    assert report.assets[0].reasons == ["upstream dbt model model.shop.stg_orders changed"]
    assert [c.unique_id for c in report.changed_models] == ["model.shop.stg_orders"]


def test_retyped_column_that_still_fits_is_affected(tmp_path: Path) -> None:
    catalog = copy.deepcopy(CATALOG)
    catalog["model.shop.orders"]["columns"]["AMOUNT"]["type"] = "FLOAT"
    report = _run(tmp_path, catalog=catalog)
    assert report.ok
    assert report.assets[0].reasons == [
        "dbt model model.shop.orders: column 'amount' type NUMBER → FLOAT"
    ]


def test_retyped_column_that_no_longer_fits_breaks(tmp_path: Path) -> None:
    catalog = copy.deepcopy(CATALOG)
    catalog["model.shop.orders"]["columns"]["AMOUNT"]["type"] = "VARCHAR"
    report = _run(tmp_path, catalog=catalog)
    assert ("dataset", "orders", "broken") in _assets(report)
    assert "`sum` needs a numeric column; 'amount' is VARCHAR" in report.assets[0].reasons


def test_removed_model_breaks_its_dataset(tmp_path: Path) -> None:
    report = _run(tmp_path, nodes={"model.shop.stg_orders": STG})
    assert _assets(report)[0] == ("dataset", "orders", "broken")
    assert report.assets[0].reasons == [
        "dbt model 'orders' does not exist",
        "dbt model model.shop.orders: removed",
    ]


def test_dependents_of_a_removed_model_are_affected(tmp_path: Path) -> None:
    """stg_orders is gone in the PR and orders now reads elsewhere: only the base knows
    the edge, and orders' own depends_on shows the change."""
    nodes = {"model.shop.orders": {**ORDERS, "depends_on": {"nodes": []}}}
    report = _run(tmp_path, nodes=nodes)
    assert ("dataset", "orders", "affected") in _assets(report)
    assert report.assets[0].reasons == [
        "dbt model model.shop.orders: no longer depends on model.shop.stg_orders"
    ]


def test_breakage_already_on_the_base_is_left_to_validate(tmp_path: Path) -> None:
    catalog = copy.deepcopy(CATALOG)
    del catalog["model.shop.orders"]["columns"]["AMOUNT"]
    report = _run(tmp_path, catalog=catalog, base_catalog=catalog)
    assert report.ok and report.assets == []


def test_spec_fixed_with_its_model_is_not_broken(tmp_path: Path) -> None:
    """The PR adds a column and the chart that uses it: fails on the base, passes now."""
    catalog = copy.deepcopy(CATALOG)
    catalog["model.shop.orders"]["columns"]["STATUS"] = {"type": "VARCHAR"}
    chart = CHART + "group_by: [status]\n"
    report = _run(tmp_path, catalog=catalog, specs={"bi/chart.yml": chart})
    assert report.ok
    assert report.assets == []  # a column added is no change to what specs read


def test_columns_that_cannot_be_compared_are_a_warning(tmp_path: Path) -> None:
    """No catalog and no YAML columns: a removed column would be invisible, so say so."""
    nodes = _with("model.shop.orders", checksum={"checksum": "b"})
    report = _run(tmp_path, nodes=nodes, catalog=None, base_catalog=None)
    assert _assets(report)[0] == ("dataset", "orders", "affected")
    warnings = [f.message for f in report.findings if f.path.name == "orders.yml"]
    assert any("columns could not be compared" in w for w in warnings)


def test_catalog_on_one_side_falls_back_to_yaml_columns(tmp_path: Path) -> None:
    """Comparing the base catalog with the PR's YAML would call every undeclared column
    removed; both sides use the YAML instead."""
    declared = {"amount": {"data_type": "numeric"}, "order_date": {"data_type": "date"}}
    nodes = _with("model.shop.orders", columns=declared)
    report = _run(tmp_path, nodes=nodes, base_nodes=nodes, catalog=None)
    assert report.ok and report.changed_models == []


def test_missing_base_manifest_is_an_error(tmp_path: Path) -> None:
    project = tmp_path / "shop"
    (project / "bi").mkdir(parents=True)
    (project / "bi" / "orders.yml").write_text(DATASET)
    _write_target(project / "target", NODES, None)
    report = impact_against_state(project / "bi", project, tmp_path / "nowhere")
    assert not report.ok
    (error,) = report.findings
    assert error.path == tmp_path / "nowhere" / "manifest.json"
    assert "dbt manifest not found; pass --state" in error.message


def test_malformed_base_is_a_warning_and_never_raises(tmp_path: Path) -> None:
    project = tmp_path / "shop"
    (project / "bi").mkdir(parents=True)
    (project / "bi" / "orders.yml").write_text(DATASET)
    _write_target(project / "target", NODES, CATALOG)
    (tmp_path / "prod").mkdir()
    (tmp_path / "prod" / "manifest.json").write_text(json.dumps({"nodes": []}))
    report = impact_against_state(project / "bi", project, tmp_path / "prod")
    assert any(
        f.severity == "warning" and "'nodes' is not a JSON object" in f.message
        for f in report.findings
    )


def test_spec_that_fails_to_load_is_a_warning(tmp_path: Path) -> None:
    broken = DATASET.replace("agg: sum", "agg: total")
    report = _run(tmp_path, specs={"bi/orders.yml": broken})
    assert report.ok
    (warning,) = [f for f in report.findings if f.path.name == "orders.yml"]
    assert warning.severity == "warning" and "run `puxti validate`" in warning.message


def test_json_shape(tmp_path: Path) -> None:
    catalog = copy.deepcopy(CATALOG)
    del catalog["model.shop.orders"]["columns"]["AMOUNT"]
    data = _run(tmp_path, catalog=catalog).to_dict()
    assert set(data) == {
        "ok",
        "specs_dir",
        "state_dir",
        "broken",
        "affected",
        "errors",
        "warnings",
        "changed_models",
        "assets",
        "findings",
    }
    assert data["ok"] is False and data["broken"] == 3 and data["affected"] == 0
    assert data["assets"][0] == {
        "kind": "dataset",
        "name": "orders",
        "path": str(tmp_path / "shop" / "bi" / "orders.yml"),
        "status": "broken",
        "reasons": [
            "column 'amount' is not in dbt model 'orders'",
            "dbt model model.shop.orders: column 'amount' removed",
        ],
    }


# ── review regressions ────────────────────────────────────────────────────────


def test_a_new_break_on_a_line_with_an_old_one_is_still_reported(tmp_path: Path) -> None:
    """`bogus` is already missing on prod; the PR drops `amount` from the same flow list."""
    dataset = DATASET.replace("model: orders\n", "model: orders\ncolumns: [amount, bogus]\n")
    catalog = copy.deepcopy(CATALOG)
    del catalog["model.shop.orders"]["columns"]["AMOUNT"]
    report = _run(tmp_path, catalog=catalog, specs={"bi/orders.yml": dataset})
    assert not report.ok
    errors = [f.message for f in report.findings if f.severity == "error"]
    assert "column 'amount' is not in dbt model 'orders'" in errors
    assert "column 'bogus' is not in dbt model 'orders'" not in errors


def test_a_still_unfit_column_retyped_is_not_a_new_break(tmp_path: Path) -> None:
    base = copy.deepcopy(CATALOG)
    base["model.shop.orders"]["columns"]["AMOUNT"]["type"] = "VARCHAR"
    current = copy.deepcopy(CATALOG)
    current["model.shop.orders"]["columns"]["AMOUNT"]["type"] = "TEXT"
    report = _run(tmp_path, catalog=current, base_catalog=base)
    assert report.ok and _assets(report)[0] == ("dataset", "orders", "affected")


def test_a_dataset_that_moves_to_a_new_latest_version_is_affected(tmp_path: Path) -> None:
    v1 = {**ORDERS, "version": 1, "latest_version": 1}
    base_nodes = {"model.shop.orders.v1": v1, "model.shop.stg_orders": STG}
    nodes = {
        "model.shop.orders.v1": {**v1, "latest_version": 2},
        "model.shop.orders.v2": {**v1, "version": 2, "latest_version": 2},
        "model.shop.stg_orders": STG,
    }
    report = _run(tmp_path, nodes=nodes, base_nodes=base_nodes, catalog=None, base_catalog=None)
    assert _assets(report)[0] == ("dataset", "orders", "affected")
    assert report.assets[0].reasons == [
        "dbt model 'orders' now resolves to model.shop.orders.v2 instead of model.shop.orders.v1"
    ]


def test_changed_seed_reaches_the_models_downstream(tmp_path: Path) -> None:
    seed = {
        "resource_type": "seed",
        "name": "country_codes",
        "package_name": "shop",
        "checksum": {"name": "sha256", "checksum": "c"},
    }
    snapshot = {
        "resource_type": "snapshot",
        "name": "stg_snapshot",
        "package_name": "shop",
        "checksum": {"name": "sha256", "checksum": "n"},
        "depends_on": {"nodes": ["seed.shop.country_codes"]},
    }
    base_nodes = {
        **NODES,
        "seed.shop.country_codes": seed,
        "snapshot.shop.stg_snapshot": snapshot,
        "model.shop.orders": {**ORDERS, "depends_on": {"nodes": ["snapshot.shop.stg_snapshot"]}},
    }
    nodes = copy.deepcopy(base_nodes)
    nodes["seed.shop.country_codes"]["checksum"] = {"checksum": "c2"}
    report = _run(tmp_path, nodes=nodes, base_nodes=base_nodes)
    assert _assets(report)[0] == ("dataset", "orders", "affected")
    assert report.assets[0].reasons == ["upstream dbt seed seed.shop.country_codes changed"]
    assert [c.model_dump() for c in report.changed_models] == [
        {"unique_id": "seed.shop.country_codes", "changes": ["data changed"]}
    ]


def test_a_seed_never_resolves_as_a_dataset_model(tmp_path: Path) -> None:
    seed = {"resource_type": "seed", "name": "orders", "package_name": "seeds"}
    nodes = {**NODES, "seed.seeds.orders": seed}
    report = _run(tmp_path, nodes=nodes, base_nodes=nodes)
    assert report.ok and report.assets == []


def test_checksum_in_one_build_only_is_not_a_silent_pass(tmp_path: Path) -> None:
    base_nodes = _with("model.shop.orders", checksum=None)
    report = _run(tmp_path, base_nodes=base_nodes, catalog=None, base_catalog=None)
    assert _assets(report)[0] == ("dataset", "orders", "affected")
    warnings = [f.message for f in report.findings if f.path.name == "orders.yml"]
    assert any("columns could not be compared" in w for w in warnings)


def test_a_ref_that_moves_to_a_new_latest_version_affects_the_model_downstream(
    tmp_path: Path,
) -> None:
    """An unpinned ref() follows the latest version without the file changing, so the
    checksum stays the same; the changed depends_on is what shows it."""
    stg_v1 = {**STG, "version": 1, "latest_version": 1}
    base_nodes = {
        "model.shop.orders": {**ORDERS, "depends_on": {"nodes": ["model.shop.stg_orders.v1"]}},
        "model.shop.stg_orders.v1": stg_v1,
    }
    nodes = {
        "model.shop.orders": {**ORDERS, "depends_on": {"nodes": ["model.shop.stg_orders.v2"]}},
        "model.shop.stg_orders.v1": {**stg_v1, "latest_version": 2},
        "model.shop.stg_orders.v2": {**stg_v1, "version": 2, "latest_version": 2},
    }
    report = _run(tmp_path, nodes=nodes, base_nodes=base_nodes)
    assert report.ok and _assets(report)[0] == ("dataset", "orders", "affected")
    assert report.assets[0].reasons == [
        "dbt model model.shop.orders: now depends on model.shop.stg_orders.v2, no longer "
        "depends on model.shop.stg_orders.v1"
    ]


def test_a_dataset_affected_through_lineage_warns_when_columns_cannot_be_compared(
    tmp_path: Path,
) -> None:
    """Only stg_orders changed, but a `select *` in orders would pass a removed column on;
    with nothing to compare orders' columns with, that is a warning, and still exit 0."""
    nodes = _with("model.shop.stg_orders", checksum={"name": "sha256", "checksum": "s2"})
    report = _run(tmp_path, nodes=nodes, catalog=None, base_catalog=None)
    assert report.ok and _assets(report)[0] == ("dataset", "orders", "affected")
    warnings = [f.message for f in report.findings if f.path.name == "orders.yml"]
    assert any(
        "model.shop.orders changed or sits downstream" in w and "columns could not be compared" in w
        for w in warnings
    )


def test_a_model_still_ambiguous_with_another_match_is_not_a_new_break(tmp_path: Path) -> None:
    """The ambiguity message lists the matches; one more match is the same failed check."""
    base_nodes = {**NODES, "model.other.orders": {**ORDERS, "package_name": "other"}}
    nodes = {**base_nodes, "model.third.orders": {**ORDERS, "package_name": "third"}}
    report = _run(tmp_path, nodes=nodes, base_nodes=base_nodes)
    assert report.broken == 0 and report.errors == 0


def test_a_large_seed_with_a_path_checksum_is_not_a_silent_pass(tmp_path: Path) -> None:
    """dbt checksums a seed over 1 MiB by its path, so edited rows keep the checksum."""
    seed = {
        "resource_type": "seed",
        "name": "country_codes",
        "package_name": "shop",
        "checksum": {"name": "path", "checksum": "seeds/country_codes.csv"},
    }
    nodes = {
        **NODES,
        "seed.shop.country_codes": seed,
        "model.shop.orders": {**ORDERS, "depends_on": {"nodes": ["seed.shop.country_codes"]}},
    }
    report = _run(tmp_path, nodes=nodes, base_nodes=nodes)
    assert report.ok and _assets(report)[0] == ("dataset", "orders", "affected")
    assert report.assets[0].reasons == ["upstream dbt seed seed.shop.country_codes changed"]
    (change,) = report.changed_models
    assert change.unique_id == "seed.shop.country_codes"
    assert "data edits could not be ruled out" in change.changes[0]
