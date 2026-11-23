"""dbt's target/ files (manifest.json, catalog.json), read defensively for BI checks.

Shared by `puxti validate` and `puxti impact --state`. Nothing here raises on a malformed
file: every unusable section becomes an entry in ``DbtFacts.problems``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Nodes kept for change detection and lineage. Datasets resolve to models only.
_TRACKED = ("model", "seed", "snapshot")


def load_json(path: Path) -> tuple[dict | None, str | None]:
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
class DbtModel:
    """A manifest model, seed or snapshot, reduced to checked fields. unique_id is the
    manifest's key."""

    unique_id: str
    name: str
    schema: str
    package: str | None
    version: str | None
    latest_version: str | None
    declared: dict[str, str | None]  # lowercase column name -> YAML data_type
    checksum: str | None = None
    resource_type: str = "model"
    depends_on: list[str] = field(default_factory=list)  # unique_ids of upstream nodes


@dataclass
class ModelColumns:
    """What is known about a model's columns, by lowercase name.

    ``catalog`` holds the columns the warehouse has, None when the catalog has no usable
    entry for the model; ``declared`` holds the ones its YAML declares. A type comes from
    the catalog first, then the YAML's ``data_type``; None when neither records one."""

    catalog: set[str] | None
    declared: set[str]
    types: dict[str, str | None]
    catalog_loaded: bool
    catalog_types: dict[str, str | None] = field(default_factory=dict)

    @property
    def known(self) -> bool:
        return self.catalog is not None or bool(self.declared)


class DbtFacts:
    """The manifest and catalog, read defensively. Every malformed section becomes an
    entry in ``problems`` (file name, message), reported as a warning; it never raises."""

    def __init__(self, manifest: dict, catalog: dict | None) -> None:
        self.problems: list[tuple[str, str]] = []
        self.by_id: dict[str, DbtModel] = {}
        self._models: dict[str, list[DbtModel]] = {}
        for node_id, node in self._section(manifest, "nodes", "manifest.json").items():
            if not isinstance(node, dict) or node.get("resource_type") not in _TRACKED:
                continue
            name = _text(node.get("name"))
            if name is None:
                continue
            columns = node.get("columns")
            declared = {
                str(col).lower(): _text(meta.get("data_type")) if isinstance(meta, dict) else None
                for col, meta in (columns.items() if isinstance(columns, dict) else [])
            }
            checksum = node.get("checksum")
            depends_on = node.get("depends_on")
            upstream = depends_on.get("nodes") if isinstance(depends_on, dict) else None
            model = DbtModel(
                unique_id=str(node_id),
                name=name,
                schema=(_text(node.get("schema")) or "").lower(),
                package=_text(node.get("package_name")),
                version=_version(node.get("version")),
                latest_version=_version(node.get("latest_version")),
                declared=declared,
                checksum=_text(checksum.get("checksum")) if isinstance(checksum, dict) else None,
                depends_on=[u for u in upstream if isinstance(u, str)]
                if isinstance(upstream, list)
                else [],
                resource_type=node["resource_type"],
            )
            self.by_id[model.unique_id] = model
            if model.resource_type == "model":  # datasets read models only
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

    def model(self, name: str) -> tuple[DbtModel | None, str | None]:
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

    def columns(self, model: DbtModel) -> ModelColumns:
        types = dict(model.declared)
        catalog: set[str] | None = None
        catalog_types: dict[str, str | None] = {}
        entry = self._catalog.get(model.unique_id)
        if entry is not None:
            columns = entry.get("columns") if isinstance(entry, dict) else None
            if isinstance(columns, dict):
                catalog = set()
                for col, meta in columns.items():
                    name = str(col).lower()
                    catalog.add(name)
                    catalog_type = _text(meta.get("type")) if isinstance(meta, dict) else None
                    catalog_types[name] = catalog_type
                    types[name] = catalog_type or types.get(name)
            else:
                problem = (
                    "catalog.json",
                    f"the entry for {model.unique_id} has no 'columns' object, so the "
                    "catalog was not used for that model",
                )
                if problem not in self.problems:
                    self.problems.append(problem)
        return ModelColumns(
            catalog, set(model.declared), types, self._catalog_loaded, catalog_types
        )

    def metric(self, name: str) -> tuple[bool, str | None]:
        """(exists, type) for a dbt metric; type None when the manifest records none."""
        key = name.lower()
        return key in self._metrics, self._metrics.get(key)
