"""Checks between specs: what each spec names must exist in the spec it names.

These need no dbt project. Checks against dbt (models, columns, types, dbt metrics) belong
to the caller that knows dbt; Puxti's ``validate`` runs both.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from puxti_bi_core.loader import SpecError, SpecSet, field_line
from puxti_bi_core.spec import Dashboard, DashboardCell, Dataset


def check_references(specs: SpecSet) -> list[SpecError]:
    """Every broken reference between loaded specs, with the line of the field.

    - a chart's dataset exists
    - the metrics a chart shows are defined on its dataset
    - the columns a chart reads are exposed by its dataset, when the dataset lists them
    - a dashboard's charts exist

    A reference to a spec whose file failed to load is not reported: that file's own
    errors already say what is wrong.
    """
    errors: list[SpecError] = []
    parsed: dict = {}

    def error(kind: str, name: str, loc: Sequence[str | int], message: str) -> None:
        path = specs.paths[f"{kind}:{name}"]
        errors.append(SpecError(path=path, line=field_line(path, loc, parsed), message=message))

    for chart in specs.charts.values():
        dataset = specs.datasets.get(chart.dataset)
        if dataset is None:
            if f"dataset:{chart.dataset}" not in specs.unloaded:  # else its own errors say why
                message = f"dataset '{chart.dataset}' does not exist"
                error("chart", chart.name, ("dataset",), message)
            continue
        defined = {m.name for m in dataset.metrics}
        for loc, metric in _chart_metrics(chart):
            if metric not in defined:
                message = f"metric '{metric}' is not defined on dataset '{dataset.name}'"
                error("chart", chart.name, loc, message)
        if dataset.columns is None:
            continue
        exposed = {c.lower() for c in dataset.columns}
        for loc, column in chart_column_fields(chart):
            if column.lower() not in exposed:
                message = f"column '{column}' is not in the columns of dataset '{dataset.name}'"
                error("chart", chart.name, loc, message)

    for dashboard in specs.dashboards.values():
        for loc, chart_name in _dashboard_charts(dashboard):
            if chart_name not in specs.charts and f"chart:{chart_name}" not in specs.unloaded:
                error("dashboard", dashboard.name, loc, f"chart '{chart_name}' does not exist")

    return errors


def chart_column_fields(chart: Any) -> list[tuple[tuple[str | int, ...], str]]:
    """Each dbt column a chart names itself, with its field location."""
    fields: list[tuple[tuple[str | int, ...], str]] = []
    x = getattr(chart, "x", None)
    if x is not None:
        fields.append((("x", "column"), x.column))
    for name in ("group_by", "columns"):
        for i, column in enumerate(getattr(chart, name, None) or []):
            fields.append(((name, i), column))
    return fields


def dataset_column_fields(dataset: Dataset) -> list[tuple[tuple[str | int, ...], str]]:
    """Each dbt column a dataset names, with its field location."""
    fields: list[tuple[tuple[str | int, ...], str]] = [
        (("columns", i), column) for i, column in enumerate(dataset.columns or [])
    ]
    for i, metric in enumerate(dataset.metrics):
        if metric.column:
            fields.append((("metrics", i, "column"), metric.column))
    return fields


def _chart_metrics(chart: Any) -> list[tuple[tuple[str | int, ...], str]]:
    metrics: list[tuple[tuple[str | int, ...], str]] = [
        (("metrics", i), name) for i, name in enumerate(getattr(chart, "metrics", None) or [])
    ]
    single = getattr(chart, "metric", None)
    if single:
        metrics.append((("metric",), single))
    return metrics


def _dashboard_charts(dashboard: Dashboard) -> list[tuple[tuple[str | int, ...], str]]:
    charts: list[tuple[tuple[str | int, ...], str]] = []
    for r, row in enumerate(dashboard.rows):
        for c, cell in enumerate(row.charts):
            if isinstance(cell, DashboardCell):
                charts.append((("rows", r, "charts", c, "chart"), cell.chart))
            else:
                charts.append((("rows", r, "charts", c), cell))
    return charts
