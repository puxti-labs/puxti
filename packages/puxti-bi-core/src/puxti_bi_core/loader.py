"""Load spec files from a folder, reporting every problem with its file and line."""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

from puxti_bi_core.spec import SPEC_ADAPTER, Dashboard, Dataset

SPEC_SUFFIXES = (".yml", ".yaml")

_BOOL = "tag:yaml.org,2002:bool"


class _SpecLoader(yaml.SafeLoader):
    """PyYAML's safe loader with two changes for hand-written specs.

    Only ``true`` and ``false`` are booleans, as in YAML 1.2, so ``title: yes`` or a key
    ``on:`` stay text. Aliases are rejected: specs have no use for them, and expanding
    them can hang the loader (a recursive alias) or take exponential time.
    """

    yaml_implicit_resolvers = {
        first: [(tag, regexp) for tag, regexp in resolvers if tag != _BOOL]
        for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }

    def compose_node(self, parent: Node | None, index: Any) -> Node | None:
        if self.check_event(yaml.AliasEvent):
            event = self.peek_event()
            raise yaml.composer.ComposerError(
                None, None, "aliases (*name) are not supported in specs", event.start_mark
            )
        return super().compose_node(parent, index)


_SpecLoader.add_implicit_resolver(
    _BOOL, re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"), list("tTfF")
)


class SpecError(BaseModel):
    """A problem in one spec file. ``line`` is 1-based, None when the file has no position."""

    model_config = ConfigDict(frozen=True)

    path: Path
    line: int | None = None
    message: str

    def __str__(self) -> str:
        where = f"{self.path}:{self.line}" if self.line else str(self.path)
        return f"{where}: {self.message}"


class SpecSet(BaseModel):
    """Every spec that loaded, keyed by name within its kind, with the file each came from."""

    datasets: dict[str, Dataset] = Field(default_factory=dict)
    charts: dict[str, Any] = Field(default_factory=dict)  # name -> one of the chart models
    dashboards: dict[str, Dashboard] = Field(default_factory=dict)
    paths: dict[str, Path] = Field(default_factory=dict)  # "<kind>:<name>" -> file


def load_specs(root: Path) -> tuple[SpecSet, list[SpecError]]:
    """Load every ``.yml``/``.yaml`` file under ``root``, skipping hidden folders.

    Files that fail to parse or validate are left out of the returned set and reported
    as errors; the rest still load, so one bad file does not hide problems in others.
    """
    specs = SpecSet()
    errors: list[SpecError] = []
    if not root.is_dir():
        return specs, [SpecError(path=root, message="spec folder not found")]
    for path in _spec_files(root):
        spec, file_errors = load_spec_file(path)
        errors.extend(file_errors)
        if spec is None:
            continue
        key = f"{spec.kind}:{spec.name}"
        if key in specs.paths:
            errors.append(
                SpecError(
                    path=path,
                    line=1,
                    message=f"{spec.kind} '{spec.name}' is also defined in {specs.paths[key]}",
                )
            )
            continue
        specs.paths[key] = path
        {"dataset": specs.datasets, "chart": specs.charts, "dashboard": specs.dashboards}[
            spec.kind
        ][spec.name] = spec
    return specs, errors


def load_spec_file(path: Path) -> tuple[Any | None, list[SpecError]]:
    """Parse and validate one spec file. Returns the spec, or None with the errors."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return None, [SpecError(path=path, message=f"cannot read file: {exc}")]
    try:
        nodes = list(yaml.compose_all(text, Loader=_SpecLoader))
    except yaml.MarkedYAMLError as exc:
        mark = exc.problem_mark or exc.context_mark
        line = mark.line + 1 if mark else None
        return None, [SpecError(path=path, line=line, message=f"invalid YAML: {exc.problem}")]
    except yaml.YAMLError as exc:
        return None, [SpecError(path=path, message=f"invalid YAML: {exc}")]

    if not nodes:
        return None, [SpecError(path=path, message="file is empty")]
    if len(nodes) > 1:
        line = nodes[1].start_mark.line + 1
        return None, [SpecError(path=path, line=line, message="one asset per file")]
    if not isinstance(nodes[0], MappingNode):
        return None, [SpecError(path=path, line=1, message="a spec must be a YAML mapping")]
    duplicates = [
        SpecError(path=path, line=line, message=f"{key}: key appears twice")
        for line, key in _duplicate_keys(nodes[0])
    ]
    if duplicates:
        return None, duplicates

    try:
        data = _SpecLoader("").construct_document(nodes[0])
    except yaml.MarkedYAMLError as exc:  # e.g. an unknown tag such as !foo
        mark = exc.problem_mark or exc.context_mark
        line = mark.line + 1 if mark else None
        return None, [SpecError(path=path, line=line, message=f"invalid YAML: {exc.problem}")]
    try:
        return SPEC_ADAPTER.validate_python(data), []
    except ValidationError as exc:
        return None, [_spec_error(path, nodes[0], err) for err in exc.errors()]


def _spec_files(root: Path) -> list[Path]:
    return sorted(
        p
        for p in root.rglob("*")
        if p.suffix.lower() in SPEC_SUFFIXES
        and p.is_file()
        and not any(part.startswith(".") for part in p.relative_to(root).parts)
    )


def _duplicate_keys(node: Node) -> list[tuple[int, str]]:
    """Keys repeated within one mapping, which PyYAML would silently collapse to the last."""
    found: list[tuple[int, str]] = []
    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, MappingNode):
            seen: set[str] = set()
            for key, value in current.value:
                if isinstance(key, ScalarNode):
                    if key.value in seen:
                        found.append((key.start_mark.line + 1, key.value))
                    seen.add(key.value)
                stack.append(value)
        elif isinstance(current, SequenceNode):
            stack.extend(current.value)
    return sorted(found)


def _spec_error(path: Path, root: Node, err: Any) -> SpecError:
    line, field = _locate(root, err["loc"], missing=err["type"] == "missing")
    message = err["msg"].removeprefix("Value error, ")
    if field:
        message = f"{field}: {message}"
    return SpecError(path=path, line=line, message=message)


def _locate(node: Node, loc: Sequence[str | int], missing: bool) -> tuple[int, str]:
    """Follow a pydantic error location through the YAML nodes.

    Location parts that name no key are union tags added by pydantic (the ``kind`` or
    chart ``type`` value) and are skipped. For a missing field, the last part is the
    field's name: it is kept in the path and the line is its parent mapping's.
    """
    line = node.start_mark.line + 1
    path: list[str] = []
    for i, part in enumerate(loc):
        if isinstance(node, MappingNode) and isinstance(part, str):
            for key, value in node.value:
                if isinstance(key, ScalarNode) and key.value == part:
                    node, line = value, value.start_mark.line + 1
                    path.append(part)
                    break
            else:
                if missing and i == len(loc) - 1:
                    path.append(part)
        elif isinstance(node, SequenceNode) and isinstance(part, int):
            if 0 <= part < len(node.value):
                node = node.value[part]
                line = node.start_mark.line + 1
            path.append(f"[{part}]")
    return line, ".".join(path).replace(".[", "[")
