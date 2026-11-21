"""The published JSON Schema for spec files.

The committed file ``schemas/v1alpha1.json`` is generated from the models in ``spec.py``;
a test fails when it goes stale. Regenerate it with ``make bi-schema``, which runs
``python -m puxti_bi_core.schema <output path>``.
"""

from __future__ import annotations

import json
import sys
from importlib.resources import files
from pathlib import Path
from typing import Any

from puxti_bi_core.spec import API_VERSION, SPEC_ADAPTER

SCHEMA_ID = (
    "https://raw.githubusercontent.com/puxti-labs/puxti/main/"
    "packages/puxti-bi-core/src/puxti_bi_core/schemas/v1alpha1.json"
)


def json_schema() -> dict[str, Any]:
    """The JSON Schema for one spec file, generated from the models."""
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": SCHEMA_ID,
        "title": f"Puxti BI spec ({API_VERSION})",
        **_drop_discriminators(SPEC_ADAPTER.json_schema()),
    }


def _drop_discriminators(node: Any) -> Any:
    """Remove pydantic's OpenAPI ``discriminator`` hints.

    They are not JSON Schema keywords, and for the nested chart union pydantic writes a
    mapping value that is an object, which OpenAPI tools reject. The ``oneOf`` branches
    already pin ``kind`` and ``type`` with ``const``, so validation is unchanged.
    """
    if isinstance(node, dict):
        return {k: _drop_discriminators(v) for k, v in node.items() if k != "discriminator"}
    if isinstance(node, list):
        return [_drop_discriminators(v) for v in node]
    return node


def render() -> str:
    return json.dumps(json_schema(), indent=2, sort_keys=True) + "\n"


def committed_schema() -> str:
    """The schema file shipped in the package."""
    return files("puxti_bi_core").joinpath("schemas/v1alpha1.json").read_text(encoding="utf-8")


def main(argv: list[str]) -> int:
    if len(argv) > 1:
        sys.stderr.write("usage: python -m puxti_bi_core.schema [output.json]\n")
        return 2
    if argv:
        Path(argv[0]).write_text(render(), encoding="utf-8")
    else:
        sys.stdout.write(render())
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
