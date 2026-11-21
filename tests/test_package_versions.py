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
