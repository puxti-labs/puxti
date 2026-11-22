"""`puxti validate`: check BI specs against each other and against the dbt project.

Offline and deterministic: dbt facts come from the project's `target/manifest.json` and,
when present, `target/catalog.json` (written by `dbt docs generate`), never from the
Knowledge Graph or an LLM. Checks between specs run in puxti_bi_core; this module adds the
dbt ones:

- a dataset's model exists (a versioned model resolves to its latest version)
- every column a dataset or chart names exists on the model
- `sum` and `avg` aggregate a numeric column; an x axis with a grain is a date or time
- a `dbt_metric` exists and is a simple metric (v1alpha1 supports no others)

What cannot be known is a warning, never a silent pass: a model with no catalog entry and
no columns declared in YAML, or a column whose type nothing records.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from puxti.settings import settings
from puxti.workspace import WorkspaceConfig
from puxti_bi_core import (
    Aggregation,
    Dataset,
    SpecError,
    SpecSet,
    chart_column_fields,
    check_references,
    dataset_column_fields,
    field_line,
    load_specs,
)

_NUMERIC_AGGS = {Aggregation.SUM, Aggregation.AVG}

# Whole type names, after wrappers and parameters are stripped (see _base_type).
_NUMERIC_TYPE = re.compile(
    r"u?int\d*|integer|u?(big|small|tiny|medium|huge)int|uinteger|number|numeric"
    r"|big(numeric|decimal)|decimal\d*|dec|float\d*|double|real|money|smallmoney"
)
_TEMPORAL_TYPE = re.compile(
    r"date\d*|datetime\d*|datetime2|datetimeoffset|smalldatetime|time|timetz"
    r"|timestamp\w*"
)
_WRAPPER = re.compile(r"(nullable|lowcardinality)\((.*)\)")
_CONTAINER = re.compile(r"(array|struct|map|record|tuple|row|json|variant|object|list)\b")


class ValidationReport(BaseModel):
    specs_dir: Path
    findings: list[SpecError] = Field(default_factory=list)

    @property
    def errors(self) -> int:
        return sum(1 for f in self.findings if f.severity == "error")

    @property
    def warnings(self) -> int:
        return sum(1 for f in self.findings if f.severity == "warning")

    def to_dict(self) -> dict[str, Any]:
        """The JSON shape shared by `puxti validate --json` and validate_bi_specs."""
        return {
            "ok": self.errors == 0,
            "specs_dir": str(self.specs_dir),
            "errors": self.errors,
            "warnings": self.warnings,
            "findings": [
                {
                    "path": display_path(f.path),
                    "line": f.line,
                    "severity": f.severity,
                    "message": f.message,
                }
                for f in self.findings
            ],
        }


def resolve_dirs(
    ws: WorkspaceConfig, specs_dir: str | None = None, dbt_project_dir: str | None = None
) -> tuple[Path, Path | None]:
    """The spec folder and dbt project to validate. Flags win, then `.puxti.yml` (the `bi`
    and `dbt` connectors), then DBT_PROJECT_DIR. Without a `bi` connector the specs are
    looked for in `bi/` inside the dbt project, or in the current directory."""
    dbt_dir = dbt_project_dir or (ws.dbt.project_dir if ws.dbt else None)
    dbt_dir = dbt_dir or settings.dbt_project_dir or None
    if specs_dir is None and ws.bi and ws.bi.project_dir:
        specs_dir = str(Path(ws.bi.project_dir) / ws.bi.extras.get("specs_dir", "bi"))
    if specs_dir is None:
        specs_dir = str(Path(dbt_dir or ".") / "bi")
    return Path(specs_dir), Path(dbt_dir) if dbt_dir else None


def validate(specs_dir: Path, dbt_project_dir: Path | None) -> ValidationReport:
    report = ValidationReport(specs_dir=specs_dir)
    specs, load_errors = load_specs(specs_dir)
    report.findings += load_errors
    report.findings += check_references(specs)
    if not specs_dir.is_dir():
        return _sorted(report)

    target = (dbt_project_dir or Path(".")) / "target"
    manifest_path = target / "manifest.json"
    manifest, problem = _load_json(manifest_path)
    if manifest is None:
        reason = problem or "not found"
        report.findings.append(
            SpecError(
                path=manifest_path,
                message=f"dbt manifest {reason}; run `dbt parse` so dbt references can "
                "be checked (set the dbt project with --dbt-project-dir)",
            )
        )
        return _sorted(report)

    catalog, problem = _load_json(target / "catalog.json")
    if problem:
        report.findings.append(
            SpecError(
                path=target / "catalog.json",
                message=f"catalog.json {problem}, so it was not used for columns and types",
                severity="warning",
            )
        )
    facts = _DbtFacts(manifest, catalog)
    report.findings += _DbtChecks(specs, facts).run()
    report.findings += [
        SpecError(path=target / file, message=message, severity="warning")
        for file, message in dict.fromkeys(facts.problems)
    ]
    return _sorted(report)


def config_error_report(exc: Exception) -> ValidationReport:
    """A report whose one error is an unreadable .puxti.yml, in the usual JSON shape."""
    return ValidationReport(
        specs_dir=Path("."),
        findings=[SpecError(path=Path(".puxti.yml"), message=f"invalid .puxti.yml: {exc}")],
    )


def display_path(path: Path) -> str:
    """A finding's path, relative to the current directory when it is under it."""
    try:
        return str(path.resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(path)


# ── dbt facts ─────────────────────────────────────────────────────────────────


def _load_json(path: Path) -> tuple[dict | None, str | None]:
    """A JSON object from a file, or None with what is wrong ("not found" is None, None)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, None
    except (OSError, ValueError) as exc:
        return None, f"is unreadable: {exc}"
    if not isinstance(data, dict):
        return None, f"is not a JSON object (got {type(data).__name__})"
    return data, None


def _text(value: Any) -> str | None:
    """value when it is a nonempty string; any other JSON value records nothing."""
    return value if isinstance(value, str) and value else None


def _version(value: Any) -> str | None:
    """A dbt model version, which may be a number or a string, as text."""
    if isinstance(value, bool) or value is None:
        return None
    return str(value) if isinstance(value, (str, int, float)) else None


@dataclass
class _Model:
    """A manifest model, reduced to checked fields. unique_id is the manifest's key."""

    unique_id: str
    schema: str
    package: str | None
    version: str | None
    latest_version: str | None
    declared: dict[str, str | None]  # lowercase column name -> YAML data_type


@dataclass
class _Columns:
    """What is known about a model's columns, by lowercase name.

    ``catalog`` holds the columns the warehouse has, None when the catalog has no usable
    entry for the model; ``declared`` holds the ones its YAML declares. A type comes from
    the catalog first, then the YAML's ``data_type``; None when neither records one."""

    catalog: set[str] | None
    declared: set[str]
    types: dict[str, str | None]
    catalog_loaded: bool

    @property
    def known(self) -> bool:
        return self.catalog is not None or bool(self.declared)


class _DbtFacts:
    """The manifest and catalog, read defensively. Every malformed section becomes an
    entry in ``problems`` (file name, message), reported as a warning; it never raises."""

    def __init__(self, manifest: dict, catalog: dict | None) -> None:
        self.problems: list[tuple[str, str]] = []
        self._models: dict[str, list[_Model]] = {}
        for node_id, node in self._section(manifest, "nodes", "manifest.json").items():
            if not isinstance(node, dict) or node.get("resource_type") != "model":
                continue
            name = _text(node.get("name"))
            if name is None:
                continue
            columns = node.get("columns")
            declared = {
                str(col).lower(): _text(meta.get("data_type")) if isinstance(meta, dict) else None
                for col, meta in (columns.items() if isinstance(columns, dict) else [])
            }
            model = _Model(
                unique_id=str(node_id),
                schema=(_text(node.get("schema")) or "").lower(),
                package=_text(node.get("package_name")),
                version=_version(node.get("version")),
                latest_version=_version(node.get("latest_version")),
                declared=declared,
            )
            self._models.setdefault(name.lower(), []).append(model)
        self._metrics: dict[str, str | None] = {}
        for metric in self._section(manifest, "metrics", "manifest.json").values():
            if isinstance(metric, dict) and (name := _text(metric.get("name"))):
                self._metrics[name.lower()] = _text(metric.get("type"))
        self._catalog_loaded = catalog is not None
        self._catalog = self._section(catalog, "nodes", "catalog.json") if catalog else {}

    def _section(self, data: dict, key: str, file: str) -> dict:
        value = data.get(key)
        if value is None:
            return {}
        if not isinstance(value, dict):
            self.problems.append((file, f"'{key}' is not a JSON object, so it was ignored"))
            return {}
        return value

    def model(self, name: str) -> tuple[_Model | None, str | None]:
        """The model a dataset names, or None with the reason. A schema-qualified name
        (`analytics.orders`) matches the model of that name in that schema."""
        schema, _, bare = name.lower().rpartition(".")
        models = self._models.get(bare, [])
        if schema:
            models = [m for m in models if m.schema == schema]
        if not models:
            return None, f"dbt model '{name}' does not exist"
        if len(models) == 1:
            (model,) = models
            if model.version and model.latest_version and model.version != model.latest_version:
                return None, (
                    f"dbt model '{name}' matches version {model.version} "
                    f"({model.unique_id}), not its latest version {model.latest_version}"
                )
            return model, None
        latest = [m for m in models if m.version and m.version == m.latest_version]
        packages = {m.package for m in models}
        if len(latest) == 1 and len(packages) == 1 and None not in packages:
            return latest[0], None
        ids = ", ".join(sorted(m.unique_id for m in models))
        return None, f"dbt model name '{name}' is ambiguous: {ids}"

    def columns(self, model: _Model) -> _Columns:
        types = dict(model.declared)
        catalog: set[str] | None = None
        entry = self._catalog.get(model.unique_id)
        if entry is not None:
            columns = entry.get("columns") if isinstance(entry, dict) else None
            if isinstance(columns, dict):
                catalog = set()
                for col, meta in columns.items():
                    name = str(col).lower()
                    catalog.add(name)
                    catalog_type = _text(meta.get("type")) if isinstance(meta, dict) else None
                    types[name] = catalog_type or types.get(name)
            else:
                self.problems.append(
                    (
                        "catalog.json",
                        f"the entry for {model.unique_id} has no 'columns' object, so the "
                        "catalog was not used for that model",
                    )
                )
        return _Columns(catalog, set(model.declared), types, self._catalog_loaded)

    def metric(self, name: str) -> tuple[bool, str | None]:
        """(exists, type) for a dbt metric; type None when the manifest records none."""
        key = name.lower()
        return key in self._metrics, self._metrics.get(key)


# ── dbt checks ────────────────────────────────────────────────────────────────


class _DbtChecks:
    def __init__(self, specs: SpecSet, facts: _DbtFacts) -> None:
        self.specs = specs
        self.facts = facts
        self.findings: list[SpecError] = []
        self._warned: set[tuple[Path, str]] = set()
        self._parsed: dict = {}  # field_line's per-run parse cache
        self._charts_on: dict[str, list[Any]] = {}
        for chart in specs.charts.values():
            self._charts_on.setdefault(chart.dataset, []).append(chart)

    def run(self) -> list[SpecError]:
        for dataset in self.specs.datasets.values():
            path = self._path("dataset", dataset.name)
            for i, metric in enumerate(dataset.metrics):  # needs no model, so always checked
                if metric.dbt_metric:
                    self._check_dbt_metric(path, ("metrics", i, "dbt_metric"), metric.dbt_metric)
            node, problem = self.facts.model(dataset.model)
            if node is None:
                self._add(path, ("model",), problem or "", "error")
                continue
            columns = self.facts.columns(node)
            self._check_dataset(dataset, path, columns)
            for chart in self._charts_on.get(dataset.name, []):
                self._check_chart(chart, dataset, columns)
        return self.findings

    def _check_dataset(self, dataset: Dataset, path: Path, columns: _Columns) -> None:
        for loc, column in dataset_column_fields(dataset):
            self._check_column(path, loc, column, dataset.model, columns)
        for i, metric in enumerate(dataset.metrics):
            if metric.column and metric.agg in _NUMERIC_AGGS:
                self._check_type(
                    path,
                    ("metrics", i, "column"),
                    metric.column,
                    dataset.model,
                    columns,
                    need="numeric",
                    why=f"`{metric.agg.value}`",
                )

    def _check_chart(self, chart: Any, dataset: Dataset, columns: _Columns) -> None:
        path = self._path("chart", chart.name)
        # A dataset that lists its columns has them checked against dbt, and the chart
        # against that list (check_references), so only an open dataset needs this.
        if dataset.columns is None:
            for loc, column in chart_column_fields(chart):
                self._check_column(path, loc, column, dataset.model, columns)
        x = getattr(chart, "x", None)
        if x is not None and x.grain is not None:
            self._check_type(
                path,
                ("x", "column"),
                x.column,
                dataset.model,
                columns,
                need="temporal",
                why=f"grain `{x.grain.value}`",
            )

    def _check_column(
        self, path: Path, loc: tuple, column: str, model: str, columns: _Columns
    ) -> None:
        name = column.lower()
        if not columns.known:
            if columns.catalog_loaded:
                reason = (
                    "catalog.json has no entry for it (it may not be built yet, or be "
                    "ephemeral) and its YAML declares none"
                )
            else:
                reason = (
                    "run `dbt docs generate` for target/catalog.json, or declare them in "
                    "the model's YAML"
                )
            self._warn_once(
                path,
                model,
                f"columns of dbt model '{model}' are unknown, so they were not checked: " + reason,
            )
        elif columns.catalog is not None and name not in columns.catalog:
            if name in columns.declared:
                self._add(
                    path,
                    loc,
                    f"column '{column}' is declared in the YAML of dbt model '{model}' but "
                    "is not in catalog.json; the model may not have been rebuilt since",
                    "warning",
                )
            else:
                self._add(path, loc, f"column '{column}' is not in dbt model '{model}'", "error")
        elif columns.catalog is None and name not in columns.declared:
            self._add(
                path,
                loc,
                f"column '{column}' is not declared in the YAML of dbt model '{model}', "
                "and there is no catalog.json entry to confirm it exists",
                "warning",
            )

    def _check_type(
        self,
        path: Path,
        loc: tuple,
        column: str,
        model: str,
        columns: _Columns,
        need: str,
        why: str,
    ) -> None:
        if column.lower() not in columns.types:
            return  # unknown or missing: already reported by _check_column
        dtype = columns.types[column.lower()]
        if dtype is None:
            self._add(
                path,
                loc,
                f"type of column '{column}' on dbt model '{model}' is unknown, so {why} was "
                "not checked; run `dbt docs generate` for target/catalog.json",
                "warning",
            )
            return
        ok = _is_numeric(dtype) if need == "numeric" else _is_temporal(dtype)
        if not ok:
            kind = "a numeric" if need == "numeric" else "a date or time"
            self._add(path, loc, f"{why} needs {kind} column; '{column}' is {dtype}", "error")

    def _check_dbt_metric(self, path: Path, loc: tuple, name: str) -> None:
        exists, metric_type = self.facts.metric(name)
        if not exists:
            self._add(path, loc, f"dbt metric '{name}' does not exist", "error")
        elif metric_type is None:
            self._add(
                path,
                loc,
                f"the manifest records no type for dbt metric '{name}', so whether it is a "
                "simple metric was not checked",
                "warning",
            )
        elif metric_type != "simple":
            self._add(
                path,
                loc,
                f"dbt metric '{name}' is a {metric_type} metric; v1alpha1 supports simple "
                "metrics only",
                "error",
            )

    def _path(self, kind: str, name: str) -> Path:
        return self.specs.paths[f"{kind}:{name}"]

    def _add(self, path: Path, loc: tuple, message: str, severity: str) -> None:
        self.findings.append(
            SpecError(
                path=path,
                line=field_line(path, loc, self._parsed),
                message=message,
                severity=severity,
            )
        )

    def _warn_once(self, path: Path, model: str, message: str) -> None:
        if (path, model) not in self._warned:
            self._warned.add((path, model))
            self.findings.append(SpecError(path=path, message=message, severity="warning"))


def _base_type(dtype: str) -> str:
    """The type's own name: lowercase, wrappers such as Nullable(...) removed, parameters
    and modifiers dropped ("NUMBER(38,2)" → "number", "timestamp with time zone" →
    "timestamp"). Empty for a container (array, struct, map, ...), which is neither
    numeric nor temporal."""
    base = dtype.strip().lower()
    while (match := _WRAPPER.fullmatch(base)) is not None:
        base = match.group(2).strip()
    if base.endswith("[]") or _CONTAINER.match(base) or re.search(r"\barray\b", base):
        return ""
    return re.split(r"[\s(<]", base, maxsplit=1)[0]


def _is_numeric(dtype: str) -> bool:
    return _NUMERIC_TYPE.fullmatch(_base_type(dtype)) is not None


def _is_temporal(dtype: str) -> bool:
    return _TEMPORAL_TYPE.fullmatch(_base_type(dtype)) is not None


def _sorted(report: ValidationReport) -> ValidationReport:
    report.findings.sort(key=lambda f: (str(f.path), f.line or 0, f.message))
    return report
