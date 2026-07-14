"""`puxti telemetry` — opt-in/opt-out subcommands."""

from unittest.mock import patch

from puxti.cli import app
from tests.cli._helpers import plain, runner

# ── telemetry subcommands ─────────────────────────────────────────────────────

def test_telemetry_shows_help():
    result = runner.invoke(app, ["telemetry", "--help"])
    assert result.exit_code == 0
    assert "on" in result.output
    assert "off" in result.output
    assert "show" in result.output


def test_telemetry_show_disabled_by_default(tmp_path):
    config_file = tmp_path / "config.toml"
    with patch("puxti.telemetry._CONFIG_PATH", config_file):
        result = runner.invoke(app, ["telemetry", "show"])
    assert result.exit_code == 0
    assert "disabled" in plain(result.output).lower()


def test_telemetry_on_enables_and_shows_install_id(tmp_path):
    config_file = tmp_path / "config.toml"
    with patch("puxti.telemetry._CONFIG_PATH", config_file):
        result = runner.invoke(app, ["telemetry", "on"])
    assert result.exit_code == 0
    assert "enabled" in plain(result.output).lower()
    # Install ID (UUID format) should be printed
    assert "-" in plain(result.output)


def test_telemetry_off_disables(tmp_path):
    config_file = tmp_path / "config.toml"
    with patch("puxti.telemetry._CONFIG_PATH", config_file):
        runner.invoke(app, ["telemetry", "on"])
        result = runner.invoke(app, ["telemetry", "off"])
    assert result.exit_code == 0
    assert "disabled" in plain(result.output).lower()


def test_telemetry_show_after_opt_in_shows_install_id(tmp_path):
    config_file = tmp_path / "config.toml"
    with patch("puxti.telemetry._CONFIG_PATH", config_file):
        runner.invoke(app, ["telemetry", "on"])
        result = runner.invoke(app, ["telemetry", "show"])
    assert result.exit_code == 0
    assert "enabled" in plain(result.output).lower()
    assert "Install ID" in result.output
