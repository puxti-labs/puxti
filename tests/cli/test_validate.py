"""`puxti validate` CLI: exit codes, line-numbered output, --json, literal spec text."""

import json
from pathlib import Path

from puxti.cli import app
from tests.cli._helpers import plain, runner
from tests.core.test_bi_validate import CHART, _project


def _invoke(project: Path, monkeypatch, *args: str):
    monkeypatch.chdir(project)
    return runner.invoke(app, ["validate", "--dbt-project-dir", ".", "--specs-dir", "bi", *args])


def test_valid_specs_exit_0(tmp_path: Path, monkeypatch) -> None:
    result = _invoke(_project(tmp_path), monkeypatch)
    assert result.exit_code == 0, result.output
    assert "Specs are valid (0 warning(s)) in bi" in plain(result.output)


def test_errors_exit_1_with_file_and_line(tmp_path: Path, monkeypatch) -> None:
    project = _project(tmp_path, specs={"bi/chart.yml": CHART.replace("orders", "nope", 1)})
    result = _invoke(project, monkeypatch)
    assert result.exit_code == 1
    output = plain(result.output)
    assert "bi/chart.yml:4: error: dataset 'nope' does not exist" in output
    assert "1 error(s)" in output


def test_warnings_alone_exit_0(tmp_path: Path, monkeypatch) -> None:
    result = _invoke(_project(tmp_path, catalog=None), monkeypatch)
    assert result.exit_code == 0, result.output
    assert "warning:" in plain(result.output)


def test_json_output(tmp_path: Path, monkeypatch) -> None:
    project = _project(tmp_path, specs={"bi/chart.yml": CHART.replace("orders", "nope", 1)})
    result = _invoke(project, monkeypatch, "--json")
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False and payload["errors"] == 1
    assert payload["findings"][0] == {
        "path": "bi/chart.yml",
        "line": 4,
        "severity": "error",
        "message": "dataset 'nope' does not exist",
    }


def test_spec_text_is_printed_literally(tmp_path: Path, monkeypatch) -> None:
    chart = CHART.replace("group_by: [status]", "group_by: ['[bold]x']")
    result = _invoke(_project(tmp_path, specs={"bi/chart.yml": chart}), monkeypatch)
    assert "column '[bold]x'" in plain(result.output)
