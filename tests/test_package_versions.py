"""Workspace packages are released together with puxti, so their versions must match."""

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _version(pyproject: Path) -> str:
    return tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"]


def test_workspace_packages_share_the_puxti_version() -> None:
    expected = _version(ROOT / "pyproject.toml")
    members = sorted((ROOT / "packages").glob("*/pyproject.toml"))
    assert members, "no workspace packages found"
    mismatched = {p.parent.name: _version(p) for p in members if _version(p) != expected}
    assert mismatched == {}, f"bump these to {expected}: {mismatched}"


def test_puxti_pins_its_workspace_packages_to_its_own_version() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    pins = {
        dep.split("==")[0]: dep.split("==")[1]
        for dep in project["dependencies"]
        if dep.startswith("puxti-")
    }
    assert pins == {"puxti-bi-core": project["version"]}
