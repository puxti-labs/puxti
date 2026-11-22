"""Connector for BI specs: datasets, charts and dashboards as YAML (puxti-bi-core).

Reads the spec folder (default `bi/` inside the project) with puxti_bi_core.load_specs.
Every asset becomes an entity, `<kind>.bi.<name>`, and every reference becomes a
DEPENDS_ON edge from the asset to what it reads:

- a dashboard to each chart on it
- a chart to its dataset, and to each dbt column it reads (its x axis, group_by and
  table columns, and the columns behind the dataset metrics it shows)
- a dataset to its dbt model, to the columns it exposes or aggregates, and to the
  dbt metrics it names

References to dbt are emitted as `sqlref.` placeholders (`orders`, `orders#amount`,
`metric:revenue`) and resolved at scan time by puxti.core.resolution, so this module
never inspects another connector's entities. References between specs resolve here.

Files that fail to load are skipped with a warning naming the file and line, and so
are references to specs that do not exist; `puxti validate` reports both in full.
BI specs are never rewritten: a change that reaches them is reported for manual review.
"""

import logging
from pathlib import Path
from typing import Any

from puxti.connectors.base import BaseConnector, fingerprint_files
from puxti.core.resolution import SQLREF_PREFIX
from puxti.models import (
    Edge,
    EdgeType,
    Entity,
    EntityType,
    FileDiff,
    SemanticChangeEvent,
    SourceFingerprint,
    UnverifiedEntity,
    UnverifiedReason,
)
from puxti_bi_core import Dashboard, Dataset, SpecSet, load_specs

logger = logging.getLogger(__name__)

NAMESPACE = "bi"
SPEC_SUFFIXES = (".yml", ".yaml")

_KIND_TYPES = {
    "dataset": EntityType.DATASET,
    "chart": EntityType.CHART,
    "dashboard": EntityType.DASHBOARD,
}


def bi_entity_id(kind: str, name: str) -> str:
    return f"{kind}.{NAMESPACE}.{name}"


class BiSpecsConnector(BaseConnector):
    """Connector for a folder of puxti-bi-core spec files.

    Config keys:
        project_dir - repo root; entity paths are relative to it
        specs_dir   - spec folder, relative to project_dir (default: "bi")
    """

    name = "bi"

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.project_dir = Path(config["project_dir"])
        self.specs_dir = self.project_dir / config.get("specs_dir", "bi")
        self._specs: SpecSet | None = None

    # ── Internal ──────────────────────────────────────────────────────────────

    def _load(self) -> SpecSet:
        if self._specs is None:
            specs, errors = load_specs(self.specs_dir)
            for error in errors:
                logger.warning("Skipping BI spec: %s", error)
            self._specs = specs
        return self._specs

    def _rel_path(self, file: Path) -> str:
        try:
            return str(file.relative_to(self.project_dir))
        except ValueError:
            return str(file)

    def _assets(self) -> list[tuple[str, Any]]:
        specs = self._load()
        return (
            [("dataset", s) for s in specs.datasets.values()]
            + [("chart", s) for s in specs.charts.values()]
            + [("dashboard", s) for s in specs.dashboards.values()]
        )

    # ── BaseConnector ─────────────────────────────────────────────────────────

    async def health_check(self) -> bool:
        return self.specs_dir.is_dir()

    def get_project_name(self) -> str:
        return NAMESPACE

    def source_fingerprint(self) -> SourceFingerprint | None:
        """Hash of every spec file under specs_dir, hidden folders included: the
        loader skips them, so hashing them only costs a needless rescan."""
        if not self.specs_dir.is_dir():
            return None
        files = (
            p
            for p in self.specs_dir.rglob("*")
            if p.suffix.lower() in SPEC_SUFFIXES and p.is_file()
        )
        return fingerprint_files(self.specs_dir, files)

    async def extract_entities(self) -> list[Entity]:
        specs = self._load()
        entities: list[Entity] = []
        for kind, spec in self._assets():
            metadata: dict[str, Any] = {
                "path": self._rel_path(specs.paths[f"{kind}:{spec.name}"]),
                "title": spec.title or "",
                "description": spec.description or "",
            }
            if isinstance(spec, Dataset):
                metadata["model"] = spec.model
                metadata["metrics"] = [m.name for m in spec.metrics]
            elif isinstance(spec, Dashboard):
                metadata["charts"] = spec.chart_names()
            else:
                metadata["chart_type"] = spec.type
                metadata["dataset"] = spec.dataset
            entities.append(
                Entity(
                    id=bi_entity_id(kind, spec.name),
                    name=spec.name,
                    type=_KIND_TYPES[kind],
                    source_connector=self.name,
                    project=NAMESPACE,
                    metadata=metadata,
                )
            )
        return entities

    async def extract_lineage(self) -> list[Edge]:
        specs = self._load()
        edges: list[Edge] = []
        missing: list[str] = []

        def add(from_id: str, target: str) -> None:
            edge = Edge(
                from_entity_id=from_id,
                to_entity_id=target,
                type=EdgeType.DEPENDS_ON,
                connector=self.name,
            )
            if target.startswith(SQLREF_PREFIX):
                edge.metadata["raw_reference"] = target[len(SQLREF_PREFIX) :]
            edges.append(edge)

        def dbt_ref(key: str) -> str:
            return f"{SQLREF_PREFIX}{key}"

        for dataset in specs.datasets.values():
            from_id = bi_entity_id("dataset", dataset.name)
            add(from_id, dbt_ref(dataset.model))
            for column in dict.fromkeys(_dataset_columns(dataset)):
                add(from_id, dbt_ref(f"{dataset.model}#{column}"))
            for metric in dataset.metrics:
                if metric.dbt_metric:
                    add(from_id, dbt_ref(f"metric:{metric.dbt_metric}"))

        for chart in specs.charts.values():
            from_id = bi_entity_id("chart", chart.name)
            dataset = specs.datasets.get(chart.dataset)
            if dataset is None:
                missing.append(f"chart '{chart.name}' → dataset '{chart.dataset}'")
                continue
            add(from_id, bi_entity_id("dataset", dataset.name))
            for column in _chart_columns(chart, dataset):
                add(from_id, dbt_ref(f"{dataset.model}#{column}"))

        for dashboard in specs.dashboards.values():
            from_id = bi_entity_id("dashboard", dashboard.name)
            for chart_name in dict.fromkeys(dashboard.chart_names()):
                if chart_name not in specs.charts:
                    missing.append(f"dashboard '{dashboard.name}' → chart '{chart_name}'")
                    continue
                add(from_id, bi_entity_id("chart", chart_name))

        for reference in missing:
            logger.warning("BI spec reference to a spec that does not exist: %s", reference)
        return edges

    async def generate_changes(
        self, event: SemanticChangeEvent
    ) -> tuple[list[FileDiff], list[UnverifiedEntity]]:
        """List every BI asset the change reaches for manual review; never a diff.

        Two sources, so an incomplete affected list from capture's LLM never hides
        a spec that reads the changed entity:
        - specs that read the changed entity itself. A model change reaches every
          dataset on it and their charts; a column change reaches the datasets and
          charts that name the column (directly or through a dataset metric).
        - BI assets capture names as affected, with every chart on an affected
          dataset.
        Dashboards holding a reached chart are listed too.
        """
        specs = self._load()
        affected = set(event.affected_entity_ids)
        whole = {  # datasets whose every chart is reached
            bi_entity_id("dataset", d.name)
            for d in specs.datasets.values()
            if bi_entity_id("dataset", d.name) in affected
        }
        reached = {
            bi_entity_id(kind, spec.name)
            for kind, spec in self._assets()
            if bi_entity_id(kind, spec.name) in affected
        }
        model, column = _changed_model_and_column(event.entity_id)
        charts_on: dict[str, list[Any]] = {}
        for chart in specs.charts.values():
            charts_on.setdefault(chart.dataset, []).append(chart)
        for dataset in specs.datasets.values():
            # A dataset may qualify its model with a schema (`analytics.orders`);
            # entity IDs carry the bare name, so compare that.
            if dataset.model.lower().rsplit(".", 1)[-1] != model:
                continue
            dataset_id = bi_entity_id("dataset", dataset.name)
            if column is None:
                whole.add(dataset_id)
            elif column in {c.lower() for c in _dataset_columns(dataset)}:
                reached.add(dataset_id)
            for chart in charts_on.get(dataset.name, []):
                if column in {c.lower() for c in _chart_columns(chart, dataset)}:
                    reached.add(bi_entity_id("chart", chart.name))
        reached |= whole
        # Walk up: dataset → charts → dashboards.
        for chart in specs.charts.values():
            if bi_entity_id("dataset", chart.dataset) in whole:
                reached.add(bi_entity_id("chart", chart.name))
        for dashboard in specs.dashboards.values():
            if any(bi_entity_id("chart", c) in reached for c in dashboard.chart_names()):
                reached.add(bi_entity_id("dashboard", dashboard.name))

        unverified = [
            UnverifiedEntity(
                entity_id=entity_id,
                reason=UnverifiedReason.BI_SPEC_MANUAL,
                detail=self._rel_path(specs.paths[_path_key(entity_id)]),
                is_source=False,
            )
            for entity_id in sorted(reached)
        ]
        return [], unverified


def _changed_model_and_column(entity_id: str) -> tuple[str, str | None]:
    """The model and column a changed entity's ID names, lowercased. Model and view
    IDs have three parts with the name last ("model.shop.orders"); their column IDs
    add the column ("model.shop.orders.amount"). The specs name dbt models, so an ID
    of another shape simply matches no dataset."""
    parts = entity_id.lower().split(".")
    if len(parts) >= 4:
        return parts[-2], parts[-1]
    return parts[-1], None


def _dataset_columns(dataset: Dataset) -> list[str]:
    """The columns a dataset names: the ones it exposes and the ones it aggregates."""
    return list(dataset.columns or []) + [m.column for m in dataset.metrics if m.column]


def _path_key(entity_id: str) -> str:
    kind, _, name = entity_id.split(".", 2)
    return f"{kind}:{name}"


def _chart_columns(chart: Any, dataset: Dataset) -> list[str]:
    """The dbt columns a chart reads: its own column fields, plus the column behind
    each dataset metric it shows. Metrics the dataset does not define are skipped."""
    columns: list[str] = []
    x = getattr(chart, "x", None)
    if x is not None:
        columns.append(x.column)
    columns += getattr(chart, "group_by", None) or []
    columns += getattr(chart, "columns", None) or []
    metric_names = list(getattr(chart, "metrics", None) or [])
    single = getattr(chart, "metric", None)
    if single:
        metric_names.append(single)
    by_name = {m.name: m for m in dataset.metrics}
    for name in metric_names:
        metric = by_name.get(name)
        if metric is not None and metric.column:
            columns.append(metric.column)
    return list(dict.fromkeys(columns))
