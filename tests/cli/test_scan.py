"""`puxti scan` — argument validation and summary output."""

from unittest.mock import AsyncMock, MagicMock, patch

from puxti.cli import app
from tests.cli._helpers import plain, runner

# ── scan ──────────────────────────────────────────────────────────────────────

def test_scan_shows_help():
    result = runner.invoke(app, ["scan", "--help"])
    assert result.exit_code == 0
    assert "--interactive" in plain(result.output)
    assert "--dbt-project-dir" in plain(result.output)


def test_scan_exits_when_no_dbt_project_dir():
    with patch("puxti.cli.scan.settings") as mock_settings:
        mock_settings.dbt_project_dir = None
        result = runner.invoke(app, ["scan"])
    assert result.exit_code == 1
    assert "dbt project directory" in result.output.lower() or "dbt_project_dir" in result.output.lower()


def test_scan_happy_path_prints_summary():
    from puxti.core.scanner import ScanResult

    mock_graph = MagicMock()
    mock_graph.connect = AsyncMock()
    mock_graph.close = AsyncMock()

    mock_scanner = MagicMock()
    mock_scanner.scan = AsyncMock(return_value=ScanResult(
        entities_upserted=12,
        lineage_edges_upserted=8,
        definitions_written=5,
        semantic_edges_written=3,
    ))

    with (
        patch("puxti.cli.scan.settings") as mock_settings,
        patch("puxti.cli.scan.KnowledgeGraph", return_value=mock_graph),
        patch("puxti.cli.scan.SemanticScanner", return_value=mock_scanner),
        patch("puxti.cli.scan.DbtConnector"),
    ):
        mock_settings.dbt_project_dir = "/some/dbt"

        result = runner.invoke(app, ["scan"])

    assert result.exit_code == 0
    assert "12" in result.output   # entities
    assert "5" in result.output    # definitions
    assert "3" in result.output    # semantic edges
