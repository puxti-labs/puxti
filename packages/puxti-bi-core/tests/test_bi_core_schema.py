"""The published JSON Schema: current with the models, valid, and in agreement with them."""

import json
from pathlib import Path

import pytest
import yaml

from puxti_bi_core import load_specs
from puxti_bi_core.schema import committed_schema, json_schema, render

FIXTURES = Path(__file__).parent / "fixtures"


def test_committed_schema_matches_the_models() -> None:
    assert committed_schema() == render(), (
        "schemas/v1alpha1.json is stale: run `make bi-schema` and commit the result"
    )


def test_schema_is_valid_draft_2020_12() -> None:
    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.Draft202012Validator.check_schema(json_schema())


def test_schema_accepts_the_valid_fixtures() -> None:
    jsonschema = pytest.importorskip("jsonschema")
    validator = jsonschema.Draft202012Validator(json.loads(committed_schema()))
    files = sorted(load_specs(FIXTURES / "valid")[0].paths.values())
    assert len(files) == 4
    for path in files:
        errors = list(validator.iter_errors(yaml.safe_load(path.read_text())))
        assert errors == [], f"{path}: {[e.message for e in errors]}"


@pytest.mark.parametrize(
    "spec",
    [
        {"kind": "dataset", "name": "d", "model": "m"},  # no apiVersion
        {"apiVersion": "puxti/v1alpha1", "kind": "report", "name": "r"},
        {"apiVersion": "puxti/v1alpha1", "kind": "chart", "name": "c", "dataset": "d"},
        {
            "apiVersion": "puxti/v1alpha1",
            "kind": "chart",
            "name": "c",
            "dataset": "d",
            "type": "big_number",
            "metric": "m",
            "extra": 1,
        },
    ],
)
def test_schema_rejects_what_the_models_reject(spec: dict) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    validator = jsonschema.Draft202012Validator(json_schema())
    assert list(validator.iter_errors(spec))
