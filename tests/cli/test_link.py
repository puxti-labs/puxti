"""`puxti link` — FEEDS edge creation and scan→link→capture regression."""

import re
from unittest.mock import AsyncMock, MagicMock, patch

from puxti.cli import app
from tests.cli._helpers import plain, runner

# ── puxti link ────────────────────────────────────────────────────────────────

def test_link_shows_help():
    result = runner.invoke(app, ["link", "--help"])
    assert result.exit_code == 0
    plain = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
    assert "--from" in plain
    assert "--to" in plain
    assert "--description" in plain


def test_link_missing_args_exits_nonzero():
    result = runner.invoke(app, ["link"])
    assert result.exit_code != 0


def test_link_invalid_from_entity_exits_1():
    result = runner.invoke(app, [
        "link",
        "--from", "invalid.entity.id",
        "--to", "source.clariva.raw_opportunities",
        "--description", "test",
    ])
    assert result.exit_code == 1
    assert "Unrecognized entity ID" in plain(result.output)


def test_link_invalid_to_entity_exits_1():
    result = runner.invoke(app, [
        "link",
        "--from", "task.airflow.salesforce_sync.extract_opportunities",
        "--to", "bad_prefix.table",
        "--description", "test",
    ])
    assert result.exit_code == 1
    assert "Unrecognized entity ID" in plain(result.output)


def test_link_happy_path_writes_feeds_edge():
    """link creates both entities under their canonical IDs and a FEEDS edge."""
    mock_graph = MagicMock()
    mock_graph.connect = AsyncMock()
    mock_graph.close = AsyncMock()
    mock_graph.get_entity_by_id = AsyncMock(return_value=None)
    mock_graph.upsert_entity = AsyncMock()
    mock_graph.upsert_semantic_edge = AsyncMock()

    with patch("puxti.cli.link.KnowledgeGraph", return_value=mock_graph):
        result = runner.invoke(app, [
            "link",
            "--from", "task.airflow.salesforce_sync.extract_opportunities",
            "--to", "source.clariva.raw_opportunities",
            "--description", "Extracts Salesforce opportunities. amount is a roll-up.",
        ])

    assert result.exit_code == 0, result.output
    assert "FEEDS" in result.output
    assert "task.airflow.salesforce_sync.extract_opportunities" in result.output
    assert "source.clariva.raw_opportunities" in result.output

    # Entities are created under their canonical string IDs — not random UUIDs —
    # so get_feeds_producers() can find the edge during capture.
    created = [call.args[0] for call in mock_graph.upsert_entity.await_args_list]
    assert [e.id for e in created] == [
        "task.airflow.salesforce_sync.extract_opportunities",
        "source.clariva.raw_opportunities",
    ]
    assert [e.name for e in created] == ["extract_opportunities", "raw_opportunities"]

    mock_graph.upsert_semantic_edge.assert_awaited_once()
    edge_call = mock_graph.upsert_semantic_edge.call_args[0][0]
    from puxti.models import EdgeType
    assert edge_call.type == EdgeType.FEEDS
    assert edge_call.from_entity_id == "task.airflow.salesforce_sync.extract_opportunities"
    assert edge_call.to_entity_id == "source.clariva.raw_opportunities"
    assert edge_call.created_by == "user"


def test_link_reuses_scan_created_entity_and_feeds_edge_is_discoverable(tmp_path):
    """Regression: the documented scan → link → capture flow must connect.

    The FEEDS edge written by link must be discoverable by get_feeds_producers()
    (what capture calls), and link must not duplicate scan-created entities.
    """
    import asyncio

    from puxti.cli.link import _run_link
    from puxti.core.graph import KnowledgeGraph
    from puxti.models import Entity, EntityType

    db_path = tmp_path / "graph.db"

    async def scenario():
        # Simulate `puxti scan`: dbt source entity under its manifest ID
        kg = KnowledgeGraph(db_path=db_path)
        await kg.connect()
        await kg.upsert_entity(Entity(
            id="source.clariva.raw_opportunities",
            name="raw_opportunities",
            type=EntityType.TABLE,
            source_connector="dbt",
            project="clariva",
        ))
        await kg.close()

        with patch("puxti.cli.link.KnowledgeGraph", lambda: KnowledgeGraph(db_path=db_path)):
            await _run_link(
                from_entity="task.airflow.salesforce_sync.extract_opportunities",
                to_entity="source.clariva.raw_opportunities",
                description="Extracts Salesforce opportunities.",
            )

        kg = KnowledgeGraph(db_path=db_path)
        await kg.connect()
        try:
            producers = await kg.get_feeds_producers("source.clariva.raw_opportunities.amount")
            all_ids = await kg.get_all_entity_ids()
            existing = await kg.get_entity_by_id("source.clariva.raw_opportunities")
        finally:
            await kg.close()
        return producers, all_ids, existing

    producers, all_ids, existing = asyncio.run(scenario())

    # Capture can now find the Airflow producer through the FEEDS edge
    assert [p.id for p in producers] == ["task.airflow.salesforce_sync.extract_opportunities"]
    # No duplicate entity was created for the scan-registered source
    assert sorted(all_ids) == [
        "source.clariva.raw_opportunities",
        "task.airflow.salesforce_sync.extract_opportunities",
    ]
    assert existing.name == "raw_opportunities"
