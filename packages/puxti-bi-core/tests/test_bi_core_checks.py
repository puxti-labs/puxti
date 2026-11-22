"""Checks between specs: every reference names something that exists, at its line."""

from pathlib import Path

from puxti_bi_core import SpecError, check_references, field_line, load_specs

HEAD = "apiVersion: puxti/v1alpha1\n"

DATASET = (
    HEAD
    + """kind: dataset
name: orders
model: orders
columns: [order_date, status]
metrics:
  - {name: revenue, agg: sum, column: amount}
"""
)


def _write(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


def _check(tmp_path: Path, **files: str) -> list[tuple[str, int | None, str]]:
    specs, errors = load_specs(_write(tmp_path, {"orders.yml": DATASET, **files}))
    assert errors == []
    return [(e.path.name, e.line, e.message) for e in check_references(specs)]


def test_valid_references_report_nothing(tmp_path: Path) -> None:
    chart = HEAD + "kind: chart\nname: c\ndataset: orders\ntype: line\n"
    chart += "x: {column: order_date}\nmetrics: [revenue]\ngroup_by: [status]\n"
    dash = HEAD + "kind: dashboard\nname: d\nrows:\n  - charts: [c, {chart: c, width: 6}]\n"
    assert _check(tmp_path, **{"c.yml": chart, "d.yml": dash}) == []


def test_chart_naming_a_missing_dataset(tmp_path: Path) -> None:
    chart = HEAD + "kind: chart\nname: c\ndataset: nope\ntype: big_number\nmetric: m\n"
    assert _check(tmp_path, **{"c.yml": chart}) == [("c.yml", 4, "dataset 'nope' does not exist")]


def test_chart_metric_missing_from_its_dataset(tmp_path: Path) -> None:
    chart = HEAD + "kind: chart\nname: c\ndataset: orders\ntype: line\n"
    chart += "x: {column: order_date}\nmetrics:\n  - revenue\n  - profit\n"
    big = HEAD + "kind: chart\nname: b\ndataset: orders\ntype: big_number\nmetric: profit\n"
    assert _check(tmp_path, **{"c.yml": chart, "b.yml": big}) == [
        ("b.yml", 6, "metric 'profit' is not defined on dataset 'orders'"),
        ("c.yml", 9, "metric 'profit' is not defined on dataset 'orders'"),
    ]


def test_chart_column_not_exposed_by_its_dataset(tmp_path: Path) -> None:
    chart = HEAD + "kind: chart\nname: c\ndataset: orders\ntype: table\n"
    chart += "columns:\n  - status\n  - Customer_ID\n"
    assert _check(tmp_path, **{"c.yml": chart}) == [
        ("c.yml", 8, "column 'Customer_ID' is not in the columns of dataset 'orders'")
    ]


def test_dataset_without_a_column_list_exposes_every_column(tmp_path: Path) -> None:
    dataset = HEAD + "kind: dataset\nname: all\nmodel: orders\n"
    chart = HEAD + "kind: chart\nname: c\ndataset: all\ntype: table\ncolumns: [anything]\n"
    assert _check(tmp_path, **{"all.yml": dataset, "c.yml": chart}) == []


def test_dashboard_naming_missing_charts_in_both_cell_forms(tmp_path: Path) -> None:
    dash = HEAD + "kind: dashboard\nname: d\nrows:\n  - charts:\n      - gone\n"
    dash += "      - {chart: also_gone, width: 6}\n"
    assert _check(tmp_path, **{"d.yml": dash}) == [
        ("d.yml", 6, "chart 'gone' does not exist"),
        ("d.yml", 7, "chart 'also_gone' does not exist"),
    ]


def test_field_line_follows_a_location(tmp_path: Path) -> None:
    path = _write(tmp_path, {"orders.yml": DATASET}) / "orders.yml"
    assert field_line(path, ("metrics", 0, "column")) == 7
    assert field_line(path, ("columns", 1)) == 5
    assert field_line(path, ("nope",)) == 1  # the deepest part found: the document
    assert field_line(tmp_path / "missing.yml", ("model",)) is None


def test_warnings_say_so_and_errors_keep_their_format(tmp_path: Path) -> None:
    error = SpecError(path=Path("a.yml"), line=3, message="m")
    warning = SpecError(path=Path("a.yml"), line=3, message="m", severity="warning")
    assert str(error) == "a.yml:3: m"
    assert str(warning) == "a.yml:3: warning: m"


def test_field_line_cache_parses_each_file_once(tmp_path: Path) -> None:
    path = _write(tmp_path, {"orders.yml": DATASET}) / "orders.yml"
    cache: dict = {}
    assert field_line(path, ("model",), cache) == 4
    path.unlink()  # a second lookup must come from the cache
    assert field_line(path, ("metrics", 0, "column"), cache) == 7
    assert field_line(path, ("model",)) is None  # without the cache the file is gone


def test_references_to_a_spec_that_failed_to_load_are_not_repeated(tmp_path: Path) -> None:
    """A typo in the dataset is one error, not one more per chart that uses it."""
    broken = DATASET.replace("agg: sum", "agg: total")
    chart = HEAD + "kind: chart\nname: c\ndataset: orders\ntype: big_number\nmetric: revenue\n"
    dash = HEAD + "kind: dashboard\nname: d\nrows:\n  - charts: [c, gone]\n"
    specs, errors = load_specs(
        _write(tmp_path, {"orders.yml": broken, "c.yml": chart, "d.yml": dash})
    )
    assert [e.path.name for e in errors] == ["orders.yml"]
    assert specs.unloaded == {"dataset:orders"}
    assert [e.message for e in check_references(specs)] == ["chart 'gone' does not exist"]
