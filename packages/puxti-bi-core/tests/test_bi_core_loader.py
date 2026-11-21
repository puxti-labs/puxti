"""Loading spec folders: what loads, and where each error points."""

from pathlib import Path

import pytest

from puxti_bi_core import load_spec_file, load_specs

FIXTURES = Path(__file__).parent / "fixtures"


def _write(tmp_path: Path, text: str, name: str = "spec.yml") -> Path:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_loads_a_folder_of_specs() -> None:
    specs, errors = load_specs(FIXTURES / "valid")
    assert errors == []
    assert sorted(specs.datasets) == ["orders"]
    assert sorted(specs.charts) == ["revenue_by_month", "total_revenue"]
    assert sorted(specs.dashboards) == ["sales"]
    assert specs.paths["chart:total_revenue"].name == "total_revenue.yaml"


def test_hidden_folders_and_other_files_are_skipped() -> None:
    specs, _ = load_specs(FIXTURES / "valid")
    assert all(".hidden" not in str(p) for p in specs.paths.values())


def test_missing_folder_is_an_error(tmp_path: Path) -> None:
    _, errors = load_specs(tmp_path / "nope")
    assert [e.message for e in errors] == ["spec folder not found"]


def test_error_points_at_the_offending_line(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "apiVersion: puxti/v1alpha1\n"
        "kind: chart\n"
        "name: c\n"
        "dataset: orders\n"
        "type: line\n"
        "x:\n"
        "  column: ts\n"
        "  grain: minute\n"
        "metrics: [revenue]\n",
    )
    spec, errors = load_spec_file(path)
    assert spec is None
    assert len(errors) == 1
    assert errors[0].line == 8
    assert errors[0].message.startswith("x.grain: ")
    assert str(errors[0]).startswith(f"{path}:8: x.grain: ")


def test_missing_field_points_at_its_parent(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "apiVersion: puxti/v1alpha1\nkind: chart\nname: c\ndataset: orders\ntype: line\n"
        "metrics: [revenue]\n",
    )
    _, errors = load_spec_file(path)
    assert [(e.line, e.message.split(":")[0]) for e in errors] == [(1, "x")]


def test_list_items_are_located(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "apiVersion: puxti/v1alpha1\n"
        "kind: dataset\n"
        "name: d\n"
        "model: m\n"
        "metrics:\n"
        "  - name: ok\n"
        "    agg: count\n"
        "  - name: bad\n"
        "    agg: sum\n",
    )
    _, errors = load_spec_file(path)
    assert [(e.line, e.message) for e in errors] == [(8, "metrics[1]: agg sum needs a column")]


def test_model_rules_point_at_their_object(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "apiVersion: puxti/v1alpha1\n"
        "kind: dashboard\n"
        "name: d\n"
        "rows:\n"
        "  - charts: [a]\n"
        "  - charts: [a, b, c, d, e]\n",
    )
    _, errors = load_spec_file(path)
    assert len(errors) == 1
    assert errors[0].line == 6
    assert errors[0].message.startswith("rows[1]: ")
    assert "divide evenly" in errors[0].message


@pytest.mark.parametrize(
    ("text", "line", "message"),
    [
        ("", None, "file is empty"),
        ("- a\n- b\n", 1, "a spec must be a YAML mapping"),
        ("kind: dataset\n---\nkind: chart\n", 3, "one asset per file"),
        ("kind: [unclosed\n", 2, "invalid YAML"),
    ],
)
def test_files_that_are_not_one_mapping(
    tmp_path: Path, text: str, line: int | None, message: str
) -> None:
    _, errors = load_spec_file(_write(tmp_path, text))
    assert len(errors) == 1
    assert errors[0].line == line
    assert errors[0].message.startswith(message)


def test_duplicate_keys_are_errors(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "apiVersion: puxti/v1alpha1\n"
        "kind: chart\n"
        "name: c\n"
        "dataset: orders\n"
        "type: line\n"
        "x: {column: ts, column: ts2}\n"
        "metrics: [revenue]\n"
        "metrics: [cost]\n",
    )
    spec, errors = load_spec_file(path)
    assert spec is None
    assert [(e.line, e.message) for e in errors] == [
        (6, "column: key appears twice"),
        (8, "metrics: key appears twice"),
    ]


def test_unknown_kind_is_reported(tmp_path: Path) -> None:
    _, errors = load_spec_file(_write(tmp_path, "apiVersion: puxti/v1alpha1\nkind: report\n"))
    assert len(errors) == 1
    assert "dataset" in errors[0].message and "dashboard" in errors[0].message


def test_duplicate_names_within_a_kind(tmp_path: Path) -> None:
    body = "apiVersion: puxti/v1alpha1\nkind: dataset\nname: orders\nmodel: m\n"
    first = _write(tmp_path, body, "a.yml")
    second = _write(tmp_path, body, "b/orders.yml")
    specs, errors = load_specs(tmp_path)
    assert specs.paths["dataset:orders"] == first
    assert len(errors) == 1
    assert errors[0].path == second
    assert str(first) in errors[0].message


def test_same_name_in_different_kinds_is_fine(tmp_path: Path) -> None:
    _write(tmp_path, "apiVersion: puxti/v1alpha1\nkind: dataset\nname: sales\nmodel: m\n", "d.yml")
    _write(
        tmp_path,
        "apiVersion: puxti/v1alpha1\nkind: dashboard\nname: sales\nrows: [{charts: [a]}]\n",
        "s.yml",
    )
    specs, errors = load_specs(tmp_path)
    assert errors == []
    assert "sales" in specs.datasets and "sales" in specs.dashboards


def test_one_bad_file_does_not_hide_the_others(tmp_path: Path) -> None:
    _write(tmp_path, "apiVersion: puxti/v1alpha1\nkind: dataset\nname: ok\nmodel: m\n", "ok.yml")
    _write(tmp_path, "apiVersion: puxti/v1alpha1\nkind: dataset\nname: Bad\n", "bad.yml")
    specs, errors = load_specs(tmp_path)
    assert list(specs.datasets) == ["ok"]
    assert {e.path.name for e in errors} == {"bad.yml"}
    assert len(errors) == 2  # name pattern, and model missing


def test_unknown_yaml_tag_is_an_error_and_later_files_still_load(tmp_path: Path) -> None:
    _write(tmp_path, "apiVersion: puxti/v1alpha1\nkind: dataset\nname: a\ntitle: !foo x\n", "a.yml")
    _write(tmp_path, "apiVersion: puxti/v1alpha1\nkind: dataset\nname: ok\nmodel: m\n", "b.yml")
    specs, errors = load_specs(tmp_path)
    assert list(specs.datasets) == ["ok"]
    assert [(e.path.name, e.line) for e in errors] == [("a.yml", 4)]
    assert errors[0].message.startswith("invalid YAML:")


SPEC_HEAD = "apiVersion: puxti/v1alpha1\nkind: chart\nname: c\ndataset: orders\n"


@pytest.mark.parametrize(
    ("text", "line"),
    [
        (SPEC_HEAD + "type: table\ncolumns: &x [*x]\n", 6),  # recursive alias
        (SPEC_HEAD + "type: big_number\nmetric: r\nsuperset: {a: &a [1, 2], b: [*a, *a]}\n", 7),
    ],
)
def test_aliases_are_rejected_without_expanding_them(tmp_path: Path, text: str, line: int) -> None:
    spec, errors = load_spec_file(_write(tmp_path, text))
    assert spec is None
    assert len(errors) == 1
    assert errors[0].line == line
    assert "aliases (*name) are not supported" in errors[0].message


def test_only_true_and_false_are_booleans(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        SPEC_HEAD + "type: big_number\nmetric: r\ntitle: yes\nsuperset: {on: Off, flag: true}\n",
    )
    spec, errors = load_spec_file(path)
    assert errors == []
    assert spec.title == "yes"
    assert spec.superset == {"on": "Off", "flag": True}


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("type: table\ncolumns: [a]\nmetrics: [r]\n", "a table lists either columns"),
        ("metric: r\n", "Unable to extract tag using discriminator 'type'"),
    ],
)
def test_model_level_errors_have_no_made_up_field(tmp_path: Path, body: str, message: str) -> None:
    _, errors = load_spec_file(_write(tmp_path, SPEC_HEAD + body))
    assert len(errors) == 1
    assert errors[0].message.startswith(message)
    assert errors[0].line == 1


def test_dataset_rule_errors_have_no_made_up_field(tmp_path: Path) -> None:
    text = (
        "apiVersion: puxti/v1alpha1\nkind: dataset\nname: d\nmodel: m\n"
        "metrics: [{name: a, agg: count}, {name: a, agg: count}]\n"
    )
    _, errors = load_spec_file(_write(tmp_path, text))
    assert [e.message for e in errors] == ["duplicate metric name: a"]


def test_dashboard_cell_errors_come_from_one_form(tmp_path: Path) -> None:
    text = (
        "apiVersion: puxti/v1alpha1\nkind: dashboard\nname: d\n"
        "rows:\n  - charts: [{chart: a, width: 20}]\n"
    )
    _, errors = load_spec_file(_write(tmp_path, text))
    assert len(errors) == 1
    assert errors[0].message.startswith("rows[0].charts[0].width: ")
    assert errors[0].line == 5


def test_spec_suffixes_match_in_any_case(tmp_path: Path) -> None:
    _write(tmp_path, "apiVersion: puxti/v1alpha1\nkind: dataset\nname: d\nmodel: m\n", "D.YML")
    specs, errors = load_specs(tmp_path)
    assert errors == []
    assert list(specs.datasets) == ["d"]
