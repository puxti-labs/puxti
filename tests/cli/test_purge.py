"""`puxti purge` — flags, confirmation, and deletion paths."""

from unittest.mock import AsyncMock, MagicMock, patch

from puxti.cli import app
from tests.cli._helpers import plain, runner

# ── purge — argument validation and confirmation ──────────────────────────────

def test_purge_shows_help():
    result = runner.invoke(app, ["purge", "--help"])
    assert result.exit_code == 0
    assert "--project" in plain(result.output)
    assert "--all" in plain(result.output)


def test_purge_exits_when_no_flags():
    result = runner.invoke(app, ["purge"])
    assert result.exit_code == 1


def test_purge_exits_when_both_flags():
    result = runner.invoke(app, ["purge", "--project", "jaffle_shop", "--all"])
    assert result.exit_code == 1


def test_purge_project_cancels_on_non_yes():
    mock_graph = MagicMock()
    mock_graph.connect = AsyncMock()
    mock_graph.close = AsyncMock()
    mock_graph.get_projects = AsyncMock(return_value=["jaffle_shop"])
    mock_graph.purge_project = AsyncMock(return_value=10)

    with patch("puxti.cli.purge.KnowledgeGraph", return_value=mock_graph):
        result = runner.invoke(app, ["purge", "--project", "jaffle_shop"], input="n\n")

    assert result.exit_code == 0
    assert "cancelled" in result.output.lower()
    mock_graph.purge_project.assert_not_called()


def test_purge_project_deletes_on_yes():
    mock_graph = MagicMock()
    mock_graph.connect = AsyncMock()
    mock_graph.close = AsyncMock()
    mock_graph.get_projects = AsyncMock(return_value=["jaffle_shop"])
    mock_graph.purge_project = AsyncMock(return_value=10)

    with patch("puxti.cli.purge.KnowledgeGraph", return_value=mock_graph):
        result = runner.invoke(app, ["purge", "--project", "jaffle_shop"], input="yes\n")

    assert result.exit_code == 0
    mock_graph.purge_project.assert_called_once_with("jaffle_shop")
    assert "jaffle_shop" in result.output


def test_purge_project_exits_when_project_not_in_graph():
    mock_graph = MagicMock()
    mock_graph.connect = AsyncMock()
    mock_graph.close = AsyncMock()
    mock_graph.get_projects = AsyncMock(return_value=["other_project"])

    with patch("puxti.cli.purge.KnowledgeGraph", return_value=mock_graph):
        result = runner.invoke(app, ["purge", "--project", "jaffle_shop"])

    assert result.exit_code == 1


def test_purge_all_cancels_on_non_yes():
    mock_graph = MagicMock()
    mock_graph.connect = AsyncMock()
    mock_graph.close = AsyncMock()
    mock_graph.get_projects = AsyncMock(return_value=["jaffle_shop"])
    mock_graph.purge_all = AsyncMock(return_value=42)

    with patch("puxti.cli.purge.KnowledgeGraph", return_value=mock_graph):
        result = runner.invoke(app, ["purge", "--all"], input="no\n")

    assert result.exit_code == 0
    assert "cancelled" in result.output.lower()
    mock_graph.purge_all.assert_not_called()


def test_purge_all_deletes_on_yes():
    mock_graph = MagicMock()
    mock_graph.connect = AsyncMock()
    mock_graph.close = AsyncMock()
    mock_graph.get_projects = AsyncMock(return_value=["jaffle_shop", "sports_sims"])
    mock_graph.purge_all = AsyncMock(return_value=42)

    with patch("puxti.cli.purge.KnowledgeGraph", return_value=mock_graph):
        result = runner.invoke(app, ["purge", "--all"], input="yes\n")

    assert result.exit_code == 0
    mock_graph.purge_all.assert_called_once()
    assert "42" in result.output
