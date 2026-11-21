"""Spec model rules: what each kind and chart type accepts and rejects."""

from typing import Any

import pytest
from pydantic import ValidationError

from puxti_bi_core.spec import (
    SPEC_ADAPTER,
    BigNumberChart,
    Dashboard,
    DashboardRow,
    Dataset,
    LineChart,
    TableChart,
)


def _spec(**fields: Any) -> dict[str, Any]:
    return {"apiVersion": "puxti/v1alpha1", **fields}


def _chart(**fields: Any) -> dict[str, Any]:
    return _spec(kind="chart", name="c", dataset="orders", **fields)


def _validate(data: dict[str, Any]) -> Any:
    return SPEC_ADAPTER.validate_python(data)


def _error(data: dict[str, Any]) -> str:
    with pytest.raises(ValidationError) as exc:
        _validate(data)
    return str(exc.value)


# ── Common fields ─────────────────────────────────────────────────────────────


def test_kind_and_type_choose_the_model() -> None:
    assert isinstance(_validate(_spec(kind="dataset", name="d", model="m")), Dataset)
    line = _validate(_chart(type="line", x={"column": "ts"}, metrics=["revenue"]))
    assert isinstance(line, LineChart)
    assert isinstance(_validate(_chart(type="big_number", metric="revenue")), BigNumberChart)


def test_api_version_is_required_and_pinned() -> None:
    data = _spec(kind="dataset", name="d", model="m")
    del data["apiVersion"]
    assert "apiVersion" in _error(data)
    assert "puxti/v1alpha1" in _error({**data, "apiVersion": "puxti/v1"})


def test_unknown_kind_and_chart_type_are_rejected() -> None:
    assert "kind" in _error(_spec(kind="report", name="r"))
    assert "type" in _error(_chart(type="sankey"))


def test_unknown_fields_are_rejected() -> None:
    assert "colour" in _error(_chart(type="big_number", metric="revenue", colour="red"))


@pytest.mark.parametrize("name", ["Revenue", "1st", "sales-overview", "", "a b"])
def test_names_are_lowercase_slugs(name: str) -> None:
    _error(_spec(kind="dataset", name=name, model="m"))


def test_id_accepts_uuid_and_tool_ids_but_not_paths() -> None:
    uuid = "8f14e45f-ceea-467f-a0e6-c5c2b3b3c3a1"
    assert _validate(_spec(kind="dataset", name="d", model="m", id=uuid)).id == uuid
    _error(_spec(kind="dataset", name="d", model="m", id="../etc"))


def test_escape_hatch_only_on_charts_and_dashboards() -> None:
    chart = _validate(_chart(type="big_number", metric="r", superset={"params": {"a": 1}}))
    assert chart.superset == {"params": {"a": 1}}
    assert "superset" in _error(_spec(kind="dataset", name="d", model="m", superset={}))


# ── Dataset ───────────────────────────────────────────────────────────────────


def test_metric_needs_one_definition() -> None:
    def ds(metric: dict[str, Any]) -> dict[str, Any]:
        return _spec(kind="dataset", name="d", model="m", metrics=[{"name": "x", **metric}])

    assert _validate(ds({"agg": "sum", "column": "amount"}))
    assert _validate(ds({"agg": "count"}))
    assert _validate(ds({"dbt_metric": "revenue"}))
    assert "needs a column" in _error(ds({"agg": "sum"}))
    assert "not both" in _error(ds({"agg": "sum", "column": "a", "dbt_metric": "r"}))
    assert "set agg" in _error(ds({}))
    _error(ds({"agg": "median", "column": "a"}))


def test_dataset_names_are_unique() -> None:
    metrics = [{"name": "x", "agg": "count"}, {"name": "x", "agg": "count"}]
    assert "duplicate metric name: x" in _error(
        _spec(kind="dataset", name="d", model="m", metrics=metrics)
    )
    assert "duplicate column name: a" in _error(
        _spec(kind="dataset", name="d", model="m", columns=["a", "b", "a"])
    )


# ── Charts ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("chart_type", ["line", "bar"])
def test_line_and_bar_need_x_and_a_metric(chart_type: str) -> None:
    assert "x" in _error(_chart(type=chart_type, metrics=["revenue"]))
    assert "metrics" in _error(_chart(type=chart_type, x={"column": "ts"}, metrics=[]))
    assert "grain" in _error(
        _chart(type=chart_type, x={"column": "ts", "grain": "minute"}, metrics=["r"])
    )


def test_big_number_takes_exactly_one_metric() -> None:
    assert "metric" in _error(_chart(type="big_number", metrics=["a", "b"]))


def test_pie_needs_a_group_by() -> None:
    assert "group_by" in _error(_chart(type="pie", metric="revenue", group_by=[]))


def test_table_is_either_raw_columns_or_an_aggregate() -> None:
    assert isinstance(_validate(_chart(type="table", columns=["a", "b"])), TableChart)
    assert _validate(_chart(type="table", metrics=["revenue"], group_by=["region"]))
    assert "not both" in _error(_chart(type="table", columns=["a"], metrics=["revenue"]))
    assert "needs columns, or metrics" in _error(_chart(type="table"))
    assert "needs columns, or metrics" in _error(_chart(type="table", group_by=["region"]))


# ── Dashboard ─────────────────────────────────────────────────────────────────


def test_row_widths_default_to_an_even_split() -> None:
    assert DashboardRow(charts=["a", "b", "c"]).layout() == [("a", 4), ("b", 4), ("c", 4)]
    row = DashboardRow(charts=[{"chart": "a", "width": 6}, "b", "c"])
    assert row.layout() == [("a", 6), ("b", 3), ("c", 3)]


@pytest.mark.parametrize(
    ("charts", "message"),
    [
        ([{"chart": "a", "width": 8}, {"chart": "b", "width": 6}], "more than 12"),
        (["a", "b", "c", "d", "e"], "divide evenly"),
        ([{"chart": "a", "width": 12}, "b"], "divide evenly"),
        ([], "at least 1"),
    ],
)
def test_rows_that_do_not_fit_the_grid_are_rejected(charts: list[Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        DashboardRow(charts=charts)


def test_dashboard_lists_its_charts_in_layout_order() -> None:
    dash = _validate(
        _spec(kind="dashboard", name="sales", rows=[{"charts": ["a"]}, {"charts": ["b", "c"]}])
    )
    assert isinstance(dash, Dashboard)
    assert dash.chart_names() == ["a", "b", "c"]


def test_id_is_ascii_so_the_json_schema_agrees() -> None:
    _error(_spec(kind="dataset", name="d", model="m", id="café_dash"))
