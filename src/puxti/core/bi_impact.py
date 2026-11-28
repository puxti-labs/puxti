"""`puxti impact --state`: what a dbt change does to the BI specs, offline.

Compares two dbt builds, each a target/-style folder: the base (``--state``, usually the
production run's artifacts) and the current project's ``target/``. Both are read the
same way `puxti validate` reads them; dbt is never run and no Knowledge Graph or LLM is
used.

- **broken**: a spec check that passes against the base and fails against the current
  build (a removed model, column or dbt metric, or a type that no longer fits), and
  every chart and dashboard built on a broken spec. A check that already failed on the
  base is left to `puxti validate`: impact reports what the change introduced.
- **affected**: a dataset whose model changed (removed, SQL changed, a column removed or
  retyped, its ``depends_on`` changed) or sits downstream of one through ``depends_on``,
  and the charts and dashboards built on it, while every reference still works.

What cannot be compared is a warning, never a silent pass.
"""

from __future__ import annotations

import re
from collections import deque
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from puxti.core.bi_validate import (
    dbt_findings,
    display_path,
    facts_problems,
    read_target,
)
from puxti.core.dbt_artifacts import DbtFacts, DbtModel
from puxti_bi_core import SpecError, SpecSet, load_specs

Status = Literal["broken", "affected"]
_KIND_ORDER = {"dataset": 0, "chart": 1, "dashboard": 2}


class ModelChange(BaseModel):
    unique_id: str
    changes: list[str]


class ImpactedAsset(BaseModel):
    kind: str
    name: str
    path: Path
    status: Status
    reasons: list[str] = Field(default_factory=list)


class ImpactReport(BaseModel):
    specs_dir: Path
    state_dir: Path
    changed_models: list[ModelChange] = Field(default_factory=list)
    assets: list[ImpactedAsset] = Field(default_factory=list)
    findings: list[SpecError] = Field(default_factory=list)

    @property
    def broken(self) -> int:
        return sum(1 for a in self.assets if a.status == "broken")

    @property
    def affected(self) -> int:
        return sum(1 for a in self.assets if a.status == "affected")

    @property
    def errors(self) -> int:
        return sum(1 for f in self.findings if f.severity == "error")

    @property
    def warnings(self) -> int:
        return sum(1 for f in self.findings if f.severity == "warning")

    @property
    def ok(self) -> bool:
        return self.broken == 0 and self.errors == 0

    def to_dict(self) -> dict[str, Any]:
        """The JSON shape of `puxti impact --state --json`."""
        return {
            "ok": self.ok,
            "specs_dir": str(self.specs_dir),
            "state_dir": str(self.state_dir),
            "broken": self.broken,
            "affected": self.affected,
            "errors": self.errors,
            "warnings": self.warnings,
            "changed_models": [c.model_dump() for c in self.changed_models],
            "assets": [
                {
                    "kind": a.kind,
                    "name": a.name,
                    "path": display_path(a.path),
                    "status": a.status,
                    "reasons": a.reasons,
                }
                for a in self.assets
            ],
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


def impact_against_state(
    specs_dir: Path, dbt_project_dir: Path | None, state_dir: Path
) -> ImpactReport:
    report = ImpactReport(specs_dir=specs_dir, state_dir=state_dir)
    specs, load_errors = load_specs(specs_dir)
    if not specs_dir.is_dir():
        report.findings += load_errors
        return report
    report.findings += [
        e.model_copy(
            update={
                "message": f"{e.message} (this spec was not checked for impact; run "
                "`puxti validate`)",
                "severity": "warning",
            }
        )
        for e in load_errors
    ]

    target = (dbt_project_dir or Path(".")) / "target"
    base, findings = read_target(
        state_dir,
        "pass --state the folder that holds the base run's manifest.json "
        "(for example the production job's target/)",
    )
    report.findings += findings
    current, findings = read_target(
        target,
        "run `dbt parse` for the current build (set the dbt project with --dbt-project-dir)",
    )
    report.findings += findings
    if base is None or current is None:
        return _sorted(report)

    changes = _model_changes(base, current)
    report.changed_models = [ModelChange(unique_id=u, changes=c) for u, c in changes.items()]
    upstream = _affected_models(changes, base, current)

    _Impact(report, specs, base, current, upstream).run()
    # Last: reading columns during the checks can add problems.
    report.findings += facts_problems(state_dir, base) + facts_problems(target, current)
    return _sorted(report)


# ── dbt diff ──────────────────────────────────────────────────────────────────


def _model_changes(base: DbtFacts, current: DbtFacts) -> dict[str, list[str]]:
    """What changed in each base model, seed or snapshot."""
    changes: dict[str, list[str]] = {}
    for uid, before in sorted(base.by_id.items()):
        after = current.by_id.get(uid)
        if after is None:
            changes[uid] = ["removed"]
            continue
        found: list[str] = []
        kind = before.resource_type
        if "path" in (before.checksum_name, after.checksum_name):
            # dbt checksums a seed over 1 MiB by its path, so edited rows look unchanged.
            found.append(
                "checksum is the file path (dbt does this for seeds over 1 MiB), so data "
                "edits could not be ruled out"
            )
        elif before.checksum and after.checksum:
            if before.checksum != after.checksum:
                found.append("SQL changed" if kind != "seed" else "data changed")
        elif before.checksum or after.checksum:
            # One build records no checksum: an edit cannot be ruled out.
            found.append("checksum recorded in one build only, so edits could not be ruled out")
        # An unpinned ref() follows a model's latest version, which can move without the
        # file (and so the checksum) changing.
        parents_before, parents_after = set(before.depends_on), set(after.depends_on)
        if added := sorted(parents_after - parents_before):
            found.append(f"now depends on {', '.join(added)}")
        if dropped := sorted(parents_before - parents_after):
            found.append(f"no longer depends on {', '.join(dropped)}")
        columns = _comparable_columns(base, before, current, after)
        if columns is not None:
            (cols_before, types_before), (cols_after, types_after) = columns
            found += [f"column '{c}' removed" for c in sorted(cols_before - cols_after)]
            for col in sorted(cols_before & cols_after):
                old, new = types_before.get(col), types_after.get(col)
                if old and new and old.lower() != new.lower():
                    found.append(f"column '{col}' type {old} → {new}")
        if found:
            changes[uid] = found
    return changes


_ColumnSide = tuple[set[str], dict[str, str | None]]


def _comparable_columns(
    base: DbtFacts, before: DbtModel, current: DbtFacts, after: DbtModel
) -> tuple[_ColumnSide, _ColumnSide] | None:
    """Columns and types from the same source on both sides: the catalog when both builds
    have an entry for the model, else the YAML. Mixing them would report every column the
    YAML does not declare as removed."""
    old, new = base.columns(before), current.columns(after)
    if old.catalog is not None and new.catalog is not None:
        return (old.catalog, old.catalog_types), (new.catalog, new.catalog_types)
    if old.declared and new.declared:
        return (old.declared, before.declared), (new.declared, after.declared)
    return None


def _affected_models(
    changes: dict[str, list[str]], base: DbtFacts, current: DbtFacts
) -> dict[str, str]:
    """Every changed node and everything downstream of one, with the reason. Lineage
    comes from both builds, so the dependents of a removed model are found too, and runs
    through seeds and snapshots as well as models."""
    children: dict[str, set[str]] = {}
    for facts in (base, current):
        for model in facts.by_id.values():
            for parent in model.depends_on:
                children.setdefault(parent, set()).add(model.unique_id)
    nodes = {**current.by_id, **base.by_id}
    reasons = {
        uid: f"dbt {nodes[uid].resource_type} {uid}: {', '.join(found)}"
        for uid, found in changes.items()
    }
    queue = deque((uid, uid) for uid in changes)
    while queue:
        uid, origin = queue.popleft()
        for child in sorted(children.get(uid, ())):
            if child not in reasons:
                reasons[child] = f"upstream dbt {nodes[origin].resource_type} {origin} changed"
                queue.append((child, origin))
    return reasons


# ── BI assets ─────────────────────────────────────────────────────────────────


class _Impact:
    def __init__(
        self,
        report: ImpactReport,
        specs: SpecSet,
        base: DbtFacts,
        current: DbtFacts,
        upstream: dict[str, str],
    ) -> None:
        self.report = report
        self.specs = specs
        self.base = base
        self.current = current
        self.upstream = upstream
        self.assets: dict[str, ImpactedAsset] = {}
        self.by_path = {path: key for key, path in specs.paths.items()}

    def run(self) -> None:
        self._broken_checks()
        for dataset in self.specs.datasets.values():
            now, before = self.current.model(dataset.model)[0], self.base.model(dataset.model)[0]
            reasons: list[str] = []
            if now and before and now.unique_id != before.unique_id:
                reasons.append(
                    f"dbt model '{dataset.model}' now resolves to {now.unique_id} instead "
                    f"of {before.unique_id}"
                )
            model = now or before
            if model is not None and model.unique_id in self.upstream:
                reasons.append(self.upstream[model.unique_id])
            for reason in reasons:
                self._mark("dataset", dataset.name, "affected", reason)
            # A change upstream can remove a column here too (`select *`), so a dataset
            # whose model's columns cannot be compared gets a warning, not a silent pass.
            if (
                reasons
                and now
                and before
                and _comparable_columns(self.base, before, self.current, now) is None
            ):
                self.report.findings.append(
                    SpecError(
                        path=self.specs.paths[f"dataset:{dataset.name}"],
                        message=f"dbt model {now.unique_id} changed or sits downstream of a "
                        "change, and its columns could not be compared, so a removed or "
                        "retyped column may be missing here: give both builds a "
                        "catalog.json from built models, or declare the columns in the "
                        "model's YAML",
                        severity="warning",
                    )
                )
        for chart in self.specs.charts.values():
            if (dataset := self.assets.get(f"dataset:{chart.dataset}")) is not None:
                reason = f"dataset '{chart.dataset}' is {dataset.status}"
                self._mark("chart", chart.name, dataset.status, reason)
        for dashboard in self.specs.dashboards.values():
            for row in dashboard.rows:
                for cell in row.charts:
                    name = cell if isinstance(cell, str) else cell.chart
                    if (chart := self.assets.get(f"chart:{name}")) is not None:
                        reason = f"chart '{name}' is {chart.status}"
                        self._mark("dashboard", dashboard.name, chart.status, reason)
        self.report.assets = sorted(
            self.assets.values(),
            key=lambda a: (a.status != "broken", _KIND_ORDER.get(a.kind, 3), a.name),
        )

    def _broken_checks(self) -> None:
        """Errors against the current build that the base build did not have. An error
        with the same file, line and message on the base counts as already there; the
        message is compared without the column's type, so a still-unfit column retyped
        (VARCHAR to TEXT) is not new. Several errors can share a line (a flow list), so
        the line alone would hide a new one behind an old one."""
        old = {_place(e) for e in dbt_findings(self.specs, self.base) if e.severity == "error"}
        for finding in dbt_findings(self.specs, self.current):
            if finding.severity != "error" or _place(finding) in old:
                continue
            self.report.findings.append(finding)
            key = self.by_path.get(finding.path)
            if key is not None:
                kind, _, name = key.partition(":")
                self._mark(kind, name, "broken", finding.message)

    def _mark(self, kind: str, name: str, status: Status, reason: str) -> None:
        key = f"{kind}:{name}"
        asset = self.assets.get(key)
        if asset is None:
            path = self.specs.paths[key]
            asset = self.assets[key] = ImpactedAsset(kind=kind, name=name, path=path, status=status)
        elif status == "broken":
            asset.status = "broken"
        if reason not in asset.reasons:
            asset.reasons.append(reason)


_TYPE_SUFFIX = re.compile(r"(; '[^']*' is ).*$")
# The three ways DbtFacts.model fails. Their detail (the matching unique_ids, the
# versions) can change while the model still fails to resolve, which is no new break.
_UNRESOLVED = re.compile(
    r"^dbt model (?:name )?('[^']*') (?:does not exist|matches version |is ambiguous: )"
)


def _place(finding: SpecError) -> tuple[Path, int | None, str]:
    if match := _UNRESOLVED.match(finding.message):
        return finding.path, finding.line, f"dbt model {match.group(1)} does not resolve"
    return finding.path, finding.line, _TYPE_SUFFIX.sub(r"\1", finding.message)


def _sorted(report: ImpactReport) -> ImpactReport:
    report.findings.sort(key=lambda f: (str(f.path), f.line or 0, f.message))
    return report
