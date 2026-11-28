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

import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from puxti.core.dbt_artifacts import DbtFacts, ModelColumns, load_json
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
    facts, findings = read_target(
        target,
        "run `dbt parse` so dbt references can be checked (set the dbt project with "
        "--dbt-project-dir)",
    )
    report.findings += findings
    if facts is not None:
        report.findings += dbt_findings(specs, facts)
        report.findings += facts_problems(target, facts)
    return _sorted(report)


def read_target(target: Path, missing_hint: str) -> tuple[DbtFacts | None, list[SpecError]]:
    """The dbt facts in a target/-style folder (manifest.json, optional catalog.json).
    None with an error when the manifest is missing or unreadable; an unusable catalog is
    a warning and the manifest is used alone."""
    manifest_path = target / "manifest.json"
    manifest, problem = load_json(manifest_path)
    if manifest is None:
        message = f"dbt manifest {problem or 'not found'}; {missing_hint}"
        return None, [SpecError(path=manifest_path, message=message)]
    findings: list[SpecError] = []
    catalog, problem = load_json(target / "catalog.json")
    if problem:
        findings.append(
            SpecError(
                path=target / "catalog.json",
                message=f"catalog.json {problem}, so it was not used for columns and types",
                severity="warning",
            )
        )
    return DbtFacts(manifest, catalog), findings


def dbt_findings(specs: SpecSet, facts: DbtFacts) -> list[SpecError]:
    """Every check of the specs against one dbt project's facts."""
    return _DbtChecks(specs, facts).run()


def facts_problems(target: Path, facts: DbtFacts) -> list[SpecError]:
    """What was malformed in the target/ files, as warnings."""
    return [
        SpecError(path=target / file, message=message, severity="warning")
        for file, message in dict.fromkeys(facts.problems)
    ]


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


# ── dbt checks ────────────────────────────────────────────────────────────────


class _DbtChecks:
    def __init__(self, specs: SpecSet, facts: DbtFacts) -> None:
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

    def _check_dataset(self, dataset: Dataset, path: Path, columns: ModelColumns) -> None:
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

    def _check_chart(self, chart: Any, dataset: Dataset, columns: ModelColumns) -> None:
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
        self, path: Path, loc: tuple, column: str, model: str, columns: ModelColumns
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
        columns: ModelColumns,
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
