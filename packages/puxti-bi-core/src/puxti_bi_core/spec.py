"""Spec models for BI assets as code, apiVersion ``puxti/v1alpha1``.

One asset per YAML file. Each asset names the layer below it: a dataset names a dbt
model, a chart names a dataset, a dashboard names charts. The models describe what an
asset shows and stay neutral about the BI tool; options only one tool has go in that
tool's escape hatch block (``superset:``), which its library merges into its output.
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Any, Literal, Union

from pydantic import (
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    StringConstraints,
    Tag,
    TypeAdapter,
    model_validator,
)

API_VERSION = "puxti/v1alpha1"

GRID_COLUMNS = 12
"""Dashboard widths are in twelfths of the dashboard width; each tool library converts them."""

Name = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z][a-z0-9_]*$", max_length=100),
    Field(description="Lowercase slug: letters, digits and underscores, starting with a letter."),
]
"""The name of a spec or of a metric defined in a dataset spec."""

Ident = Annotated[
    str,
    StringConstraints(min_length=1, max_length=255, pattern=r"^\S+$"),
    Field(description="A dbt model, column or metric name, as dbt spells it."),
]
"""A name defined in dbt, resolved by Puxti against the manifest."""

ToolId = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z0-9_-]+$", max_length=512),
    Field(
        description=(
            "An existing asset ID to keep, set when an asset is adopted from a live BI tool. "
            "Without it the ID is derived from the kind and name."
        )
    ),
]

EscapeHatch = Annotated[
    dict[str, Any],
    Field(
        description=(
            "Raw Superset fields merged into the compiled Superset asset last, for options "
            "the spec does not cover yet. Ignored by other BI tools."
        )
    ),
]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class _Spec(_Model):
    api_version: Literal["puxti/v1alpha1"] = Field(alias="apiVersion")
    name: Name
    title: str | None = None
    description: str | None = None
    id: ToolId | None = None


# ── Dataset ───────────────────────────────────────────────────────────────────


class Aggregation(str, Enum):
    SUM = "sum"
    COUNT = "count"
    COUNT_DISTINCT = "count_distinct"
    AVG = "avg"
    MIN = "min"
    MAX = "max"


class Metric(_Model):
    """A metric on a dataset: an aggregation of one column, or a dbt metric by name."""

    name: Name
    title: str | None = None
    description: str | None = None
    agg: Aggregation | None = None
    column: Ident | None = None
    dbt_metric: Ident | None = Field(
        default=None, description="A dbt semantic layer metric. v1alpha1 supports simple metrics."
    )

    @model_validator(mode="after")
    def _one_definition(self) -> Metric:
        if self.dbt_metric is not None:
            if self.agg is not None or self.column is not None:
                raise ValueError("set either dbt_metric, or agg and column, not both")
            return self
        if self.agg is None:
            raise ValueError("set agg (with a column), or dbt_metric")
        if self.column is None and self.agg is not Aggregation.COUNT:
            raise ValueError(f"agg {self.agg.value} needs a column")
        return self


class Dataset(_Spec):
    """A dbt model exposed to the BI tool, with the metrics charts can use."""

    kind: Literal["dataset"]
    model: Ident
    columns: list[Ident] | None = Field(
        default=None, description="Columns to expose. Defaults to every column dbt knows."
    )
    metrics: list[Metric] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_names(self) -> Dataset:
        _require_unique([m.name for m in self.metrics], "metric")
        _require_unique(self.columns or [], "column")
        return self


# ── Chart ─────────────────────────────────────────────────────────────────────


class Grain(str, Enum):
    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    QUARTER = "quarter"
    YEAR = "year"


class XAxis(_Model):
    column: Ident
    grain: Grain | None = Field(default=None, description="Time grain, for a time column.")


MetricList = Annotated[list[Name], Field(min_length=1)]


class _Chart(_Spec):
    kind: Literal["chart"]
    dataset: Name
    superset: EscapeHatch | None = None


class LineChart(_Chart):
    type: Literal["line"]
    x: XAxis
    metrics: MetricList
    group_by: list[Ident] = Field(default_factory=list)


class BarChart(_Chart):
    type: Literal["bar"]
    x: XAxis
    metrics: MetricList
    group_by: list[Ident] = Field(default_factory=list)


class TableChart(_Chart):
    """Raw rows (``columns``) or an aggregate (``metrics``, optionally ``group_by``)."""

    type: Literal["table"]
    columns: list[Ident] = Field(default_factory=list)
    metrics: list[Name] = Field(default_factory=list)
    group_by: list[Ident] = Field(default_factory=list)

    @model_validator(mode="after")
    def _one_mode(self) -> TableChart:
        if self.columns and (self.metrics or self.group_by):
            raise ValueError("a table lists either columns, or metrics with group_by, not both")
        if not self.columns and not self.metrics:
            raise ValueError("a table needs columns, or metrics")
        return self


class BigNumberChart(_Chart):
    type: Literal["big_number"]
    metric: Name


class PieChart(_Chart):
    type: Literal["pie"]
    metric: Name
    group_by: Annotated[list[Ident], Field(min_length=1)]


Chart = Annotated[
    Union[LineChart, BarChart, TableChart, BigNumberChart, PieChart],
    Field(discriminator="type"),
]


# ── Dashboard ─────────────────────────────────────────────────────────────────


class DashboardCell(_Model):
    chart: Name
    width: int | None = Field(
        default=None, ge=1, le=GRID_COLUMNS, description="Width in twelfths of the dashboard."
    )


def _cell_form(value: Any) -> str:
    return "name" if isinstance(value, str) else "cell"


Cell = Annotated[
    Union[Annotated[Name, Tag("name")], Annotated[DashboardCell, Tag("cell")]],
    Discriminator(_cell_form),
]
"""A chart name, or a chart with a width. Choosing the form first keeps errors to one form."""


class DashboardRow(_Model):
    charts: Annotated[list[Cell], Field(min_length=1)]
    height: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Relative height; each BI tool maps it to its own units. Defaults per chart type."
        ),
    )

    @model_validator(mode="after")
    def _fits_grid(self) -> DashboardRow:
        self.layout()
        return self

    def layout(self) -> list[tuple[str, int]]:
        """Each chart with its width. Charts without a width share what is left evenly."""
        cells = [c if isinstance(c, DashboardCell) else DashboardCell(chart=c) for c in self.charts]
        given = sum(c.width for c in cells if c.width is not None)
        open_cells = sum(1 for c in cells if c.width is None)
        left = GRID_COLUMNS - given
        if left < 0:
            raise ValueError(f"widths add up to {given}, more than {GRID_COLUMNS}")
        share = 0
        if open_cells:
            if left < open_cells or left % open_cells:
                raise ValueError(
                    f"{left} columns left for {open_cells} charts without a width; "
                    "set widths so they divide evenly"
                )
            share = left // open_cells
        return [(c.chart, c.width if c.width is not None else share) for c in cells]


class Dashboard(_Spec):
    kind: Literal["dashboard"]
    rows: Annotated[list[DashboardRow], Field(min_length=1)]
    superset: EscapeHatch | None = None

    def chart_names(self) -> list[str]:
        return [name for row in self.rows for name, _ in row.layout()]


# ── Any spec ──────────────────────────────────────────────────────────────────

Spec = Annotated[Union[Dataset, Chart, Dashboard], Field(discriminator="kind")]

SPEC_ADAPTER: TypeAdapter[Any] = TypeAdapter(Spec)
"""Validates any spec, choosing the model by ``kind`` and, for charts, ``type``."""


def _require_unique(names: list[str], what: str) -> None:
    seen: set[str] = set()
    dupes = sorted({n for n in names if n in seen or seen.add(n)})
    if dupes:
        raise ValueError(f"duplicate {what} name: {', '.join(dupes)}")
