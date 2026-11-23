"""`puxti impact --state`: exit codes, table and --json output, flag combinations."""

import copy
import json
from pathlib import Path
from unittest.mock import patch

from puxti.cli import app
from tests.cli._helpers import plain, runner
from tests.core.test_bi_impact import CATALOG, _run


def _invoke(
    tmp_path: Path,
    monkeypatch,
    *args: str,
    catalog: dict | str = "default",
    base_catalog: dict | str = "default",
):
    # Writes the project, the PR build and prod/.
    _run(tmp_path, catalog=catalog, base_catalog=base_catalog)
    monkeypatch.chdir(tmp_path / "shop")
    flags = ["--state", "../prod", "--dbt-project-dir", ".", "--specs-dir", "bi"]
    return runner.invoke(app, ["impact", *flags, *args])


def _removed_amount() -> dict:
    catalog = copy.deepcopy(CATALOG)
    del catalog["model.shop.orders"]["columns"]["AMOUNT"]
    return catalog


def test_no_change_exits_0(tmp_path: Path, monkeypatch) -> None:
    result = _invoke(tmp_path, monkeypatch)
    assert result.exit_code == 0, result.output
    assert "No BI spec is affected" in plain(result.output)


def test_broken_asset_exits_1_with_table_and_line(tmp_path: Path, monkeypatch) -> None:
    result = _invoke(tmp_path, monkeypatch, catalog=_removed_amount())
    out = plain(result.output)
    assert result.exit_code == 1
    assert "model.shop.orders: column 'amount' removed" in out
    assert "revenue_by_month" in out and "broken" in out
    assert "bi/orders.yml:6: error: column 'amount' is not in dbt model 'orders'" in out
    assert "3 broken, 0 affected" in out


def test_json_output(tmp_path: Path, monkeypatch) -> None:
    result = _invoke(tmp_path, monkeypatch, "--json", catalog=_removed_amount())
    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["ok"] is False and data["broken"] == 3
    assert data["assets"][0]["path"] == "bi/orders.yml"


def test_state_never_touches_the_graph(tmp_path: Path, monkeypatch) -> None:
    with patch("puxti.cli.impact.KnowledgeGraph", side_effect=AssertionError("graph used")):
        result = _invoke(tmp_path, monkeypatch)
    assert result.exit_code == 0, result.output


def test_state_rejects_an_entity_or_change_type() -> None:
    for args in (["model.shop.orders"], ["--change-type", "drop"]):
        result = runner.invoke(app, ["impact", "--state", "prod", *args])
        assert result.exit_code == 1
        assert "takes no ENTITY or --change-type" in plain(result.output)


def test_entity_or_state_is_required() -> None:
    result = runner.invoke(app, ["impact"])
    assert result.exit_code == 1
    assert "Pass an ENTITY ID, or --state DIR" in plain(result.output)


def test_state_only_flags_need_state() -> None:
    result = runner.invoke(app, ["impact", "model.shop.orders", "--specs-dir", "bi"])
    assert result.exit_code == 1
    assert "need --state" in plain(result.output)


def test_manifest_text_is_printed_literally(tmp_path: Path, monkeypatch) -> None:
    """A column named like Rich markup, removed by the change, prints as written."""
    base = copy.deepcopy(CATALOG)
    base["model.shop.orders"]["columns"]["[BOLD]X"] = {"type": "VARCHAR"}
    result = _invoke(tmp_path, monkeypatch, base_catalog=base)
    assert result.exit_code == 0, result.output
    assert "column '[bold]x' removed" in plain(result.output)


def test_json_output_for_a_broken_workspace_file(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / ".puxti.yml").write_text("version: 7\n")
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["impact", "--state", "prod", "--json"])
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False and payload["findings"][0]["path"] == ".puxti.yml"
