"""`puxti --help` — top-level command registration."""

from puxti.cli import app
from tests.cli._helpers import runner

# ── help / command registration ───────────────────────────────────────────────

def test_app_shows_help():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "capture" in result.output
    assert "scan" in result.output
    assert "redefine" in result.output
    assert "health" in result.output
    assert "describe" in result.output
    assert "purge" in result.output
    assert "correct" in result.output
    assert "impact" in result.output
    assert "telemetry" in result.output
    assert "mcp" in result.output
