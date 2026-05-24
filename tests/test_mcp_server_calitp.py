"""Realistic MCP tool scenarios using cal-itp/data-infra structure.

All entity IDs, model names, and lineage reflect the actual cal-itp warehouse:
  https://github.com/cal-itp/data-infra/tree/main/warehouse

Lineage being exercised (payments domain):

  stg_littlepay__micropayments  (staging)
    └─► int_payments__micropayments_adjustments_refunds_joined  (intermediate)
          └─► fct_payments_rides_v2  (mart)

  stg_littlepay__authorisations  (staging)
    └─► int_payments__authorisations_deduped  (intermediate)
          └─► fct_payments_aggregations  (mart)
          └─► fct_payments_authorisations  (mart)

Semantic edges modelled:
  fct_payments_rides_v2      DERIVED_FROM  stg_littlepay__micropayments
  fct_payments_authorisations DERIVED_FROM stg_littlepay__authorisations
"""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from puxti.models import Definition, EdgeType, SemanticEdge

# ── helpers ───────────────────────────────────────────────────────────────────

PROJECT = "cal_itp"


def _model(name: str, etype: str = "model") -> MagicMock:
    e = MagicMock()
    e.id = f"model.{PROJECT}.{name}"
    e.name = name
    e.type = MagicMock(value=etype)
    e.source_connector = "dbt"
    e.project = PROJECT
    return e


def _graph(**kwargs) -> AsyncMock:
    g = AsyncMock()
    g.get_entity_by_id = AsyncMock(return_value=kwargs.get("entity"))
    g.get_semantic_dependents_with_depth = AsyncMock(return_value=kwargs.get("semantic_deps", []))
    g.get_structural_dependents = AsyncMock(return_value=kwargs.get("structural_deps", []))
    g.get_latest_definition = AsyncMock(return_value=kwargs.get("definition"))
    g.get_entity_semantic_edges = AsyncMock(return_value=kwargs.get("edges", []))
    g.get_definition_history = AsyncMock(return_value=kwargs.get("history", []))
    return g


def _definition(entity_name: str, description: str, version: int, created_by: str = "llm") -> Definition:
    return Definition(
        entity_id=f"model.{PROJECT}.{entity_name}",
        description=description,
        version=version,
        created_by=created_by,
    )


# ── Scenario 1: impact of renaming a staging model ────────────────────────────
# An engineer plans to rename `charge_amount` → `fare_amount` in
# stg_littlepay__micropayments. Before touching the SQL they run impact_of_change
# to understand the blast radius.
#
# Expected: the intermediate model at hop-1 and the mart model at hop-2 both
# appear as structural dependents. fct_payments_rides_v2 also appears as a
# semantic dependent because it derives its "fare revenue" concept from the
# staging model.


async def test_rename_charge_amount_in_staging_shows_cascade():
    micropayments = _model("stg_littlepay__micropayments")
    int_joined = _model("int_payments__micropayments_adjustments_refunds_joined")
    fct_rides = _model("fct_payments_rides_v2")

    # Structural: int_joined reads the staging model directly (hop-1),
    # fct_rides reads int_joined (hop-2 — puxti walks recursively).
    # Semantic: fct_rides derives its "fare revenue per ride" concept from micropayments.
    graph = _graph(
        entity=micropayments,
        structural_deps=[int_joined, fct_rides],
        semantic_deps=[(fct_rides, 1)],
    )

    with patch("puxti.mcp_server._graph_connect", new=AsyncMock(return_value=graph)):
        from puxti.mcp_server import impact_of_change
        result = json.loads(
            await impact_of_change(
                entity_id="model.cal_itp.stg_littlepay__micropayments",
                change_type="rename",
            )
        )

    assert result["change_type"] == "rename"
    assert result["total_count"] == 2

    ids = {d["entity_id"] for d in result["dependents"]}
    assert "model.cal_itp.int_payments__micropayments_adjustments_refunds_joined" in ids
    assert "model.cal_itp.fct_payments_rides_v2" in ids

    # fct_payments_rides_v2 appears in both relationships — structural AND semantic
    rides_row = next(d for d in result["dependents"] if d["name"] == "fct_payments_rides_v2")
    assert "semantic" in rides_row["relationship"]
    assert "structural" in rides_row["relationship"]


async def test_rename_shows_intermediate_as_structural_only():
    """int_payments__micropayments_adjustments_refunds_joined has no semantic edge to stg_littlepay__micropayments,
    so it should appear as structural-only."""
    micropayments = _model("stg_littlepay__micropayments")
    int_joined = _model("int_payments__micropayments_adjustments_refunds_joined")
    fct_rides = _model("fct_payments_rides_v2")

    graph = _graph(
        entity=micropayments,
        structural_deps=[int_joined, fct_rides],
        semantic_deps=[(fct_rides, 1)],
    )

    with patch("puxti.mcp_server._graph_connect", new=AsyncMock(return_value=graph)):
        from puxti.mcp_server import impact_of_change
        result = json.loads(
            await impact_of_change(entity_id="model.cal_itp.stg_littlepay__micropayments", change_type="rename")
        )

    int_row = next(d for d in result["dependents"] if d["name"] == "int_payments__micropayments_adjustments_refunds_joined")
    assert int_row["relationship"] == "structural"


# ── Scenario 2: redefine ripples through semantic graph only ──────────────────
# An engineer wants to update the definition of stg_littlepay__micropayments
# to clarify that `type = 'DEBIT'` rows are the only ones representing actual
# fare revenue (CREDIT rows are adjustments). This is a semantic-only change —
# no SQL changes — so only semantic dependents matter.


async def test_redefine_micropayments_only_semantic_ripple():
    micropayments = _model("stg_littlepay__micropayments")
    fct_rides = _model("fct_payments_rides_v2")
    fct_agg = _model("fct_payments_aggregations")

    # Semantic dependents at hop-1: two mart models whose meaning directly
    # depends on the concept of "what a micropayment is".
    graph = _graph(
        entity=micropayments,
        structural_deps=[],          # no SQL change, no structural risk
        semantic_deps=[(fct_rides, 1), (fct_agg, 1)],
    )

    with patch("puxti.mcp_server._graph_connect", new=AsyncMock(return_value=graph)):
        from puxti.mcp_server import impact_of_change
        result = json.loads(
            await impact_of_change(
                entity_id="model.cal_itp.stg_littlepay__micropayments",
                change_type="redefine",
            )
        )

    assert result["change_type"] == "redefine"
    assert result["total_count"] == 2
    names = {d["name"] for d in result["dependents"]}
    assert names == {"fct_payments_rides_v2", "fct_payments_aggregations"}
    for dep in result["dependents"]:
        assert dep["relationship"] == "semantic"
        assert dep["hop"] == 1


# ── Scenario 3: consumers of the key intermediate model ───────────────────────
# int_payments__micropayments_adjustments_refunds_joined is the central
# intermediate model in the payments pipeline. Understanding who reads it
# directly is critical before any schema change.


async def test_consumers_of_micropayments_intermediate():
    int_joined = _model("int_payments__micropayments_adjustments_refunds_joined")
    fct_rides = _model("fct_payments_rides_v2")

    graph = _graph(entity=int_joined, structural_deps=[fct_rides])

    with patch("puxti.mcp_server._graph_connect", new=AsyncMock(return_value=graph)):
        from puxti.mcp_server import consumers
        result = json.loads(
            await consumers(entity_id="model.cal_itp.int_payments__micropayments_adjustments_refunds_joined")
        )

    assert result["total_count"] == 1
    assert result["consumers"][0]["name"] == "fct_payments_rides_v2"
    assert result["consumers"][0]["project"] == PROJECT


async def test_consumers_of_authorisations_intermediate_has_two_mart_models():
    int_auth = _model("int_payments__authorisations_deduped")
    fct_auth = _model("fct_payments_authorisations")
    fct_agg = _model("fct_payments_aggregations")

    graph = _graph(entity=int_auth, structural_deps=[fct_auth, fct_agg])

    with patch("puxti.mcp_server._graph_connect", new=AsyncMock(return_value=graph)):
        from puxti.mcp_server import consumers
        result = json.loads(
            await consumers(entity_id="model.cal_itp.int_payments__authorisations_deduped")
        )

    assert result["total_count"] == 2
    names = {c["name"] for c in result["consumers"]}
    assert names == {"fct_payments_authorisations", "fct_payments_aggregations"}


# ── Scenario 4: describe the core mart model ──────────────────────────────────
# A new engineer joins the team and wants to understand what fct_payments_rides_v2
# actually represents, how it was defined, and what semantic relationships it has.


async def test_describe_fct_payments_authorisations():
    fct_auth = _model("fct_payments_authorisations")
    definition = _definition(
        "fct_payments_authorisations",
        description=(
            "One row per authorisation attempt. An authorisation is a request to "
            "reserve funds for a tap-on transit payment. Includes both successful "
            "(Authorised) and declined attempts. Use charge_amount and status to "
            "measure approval rates by operator and funding source type."
        ),
        version=2,
        created_by="user",
    )
    # Incoming edge: this model is derived from the authorisations staging model
    incoming_edge = SemanticEdge(
        from_entity_id="model.cal_itp.stg_littlepay__authorisations",
        to_entity_id="model.cal_itp.fct_payments_authorisations",
        type=EdgeType.DERIVED_FROM,
        description="fct_payments_authorisations derives its authorisation records from the littlepay staging layer",
        created_by="scan",
    )
    graph = _graph(entity=fct_auth, definition=definition, edges=[incoming_edge])

    with patch("puxti.mcp_server._graph_connect", new=AsyncMock(return_value=graph)):
        from puxti.mcp_server import describe_entity
        result = json.loads(
            await describe_entity(entity_id="model.cal_itp.fct_payments_authorisations")
        )

    assert result["name"] == "fct_payments_authorisations"
    assert result["type"] == "model"
    assert result["project"] == PROJECT
    assert result["definition"]["version"] == 2
    assert result["definition"]["created_by"] == "user"
    assert "authorisation attempt" in result["definition"]["description"]

    assert len(result["semantic_edges"]) == 1
    edge = result["semantic_edges"][0]
    assert edge["direction"] == "incoming"
    assert edge["type"] == "derived_from"
    assert edge["from_entity_id"] == "model.cal_itp.stg_littlepay__authorisations"


async def test_describe_fct_payments_rides_v2_shows_outgoing_feeds_edge():
    fct_rides = _model("fct_payments_rides_v2")
    # fct_payments_rides_v2 FEEDS a downstream reporting model
    outgoing_edge = SemanticEdge(
        from_entity_id="model.cal_itp.fct_payments_rides_v2",
        to_entity_id="model.cal_itp.elavon_littlepay__daily_history_transactions_deposits_billing",
        type=EdgeType.FEEDS,
        description="fct_payments_rides_v2 feeds the daily reconciliation report",
        created_by="scan",
    )
    graph = _graph(entity=fct_rides, edges=[outgoing_edge])

    with patch("puxti.mcp_server._graph_connect", new=AsyncMock(return_value=graph)):
        from puxti.mcp_server import describe_entity
        result = json.loads(await describe_entity(entity_id="model.cal_itp.fct_payments_rides_v2"))

    edge = result["semantic_edges"][0]
    assert edge["direction"] == "outgoing"
    assert edge["type"] == "feeds"


# ── Scenario 5: definition history of the micropayments staging model ─────────
# stg_littlepay__micropayments has been iteratively defined over time:
#   v1 - auto-generated by puxti scan
#   v2 - engineer adds context about DEBIT vs CREDIT distinction
#   v3 - engineer adds note about pending vs completed payment states


async def test_definition_history_of_micropayments_shows_three_versions():
    micropayments = _model("stg_littlepay__micropayments")
    history = [
        _definition(
            "stg_littlepay__micropayments",
            description="One row per micropayment record from the Littlepay tap payment system.",
            version=1,
            created_by="llm",
        ),
        _definition(
            "stg_littlepay__micropayments",
            description=(
                "One row per micropayment record from the Littlepay tap payment system. "
                "type='DEBIT' rows represent a charge to the rider; type='CREDIT' rows represent "
                "adjustments or refunds applied after an aggregation is settled."
            ),
            version=2,
            created_by="user",
        ),
        _definition(
            "stg_littlepay__micropayments",
            description=(
                "One row per micropayment record from the Littlepay tap payment system. "
                "type='DEBIT' rows represent a charge to the rider; type='CREDIT' rows represent "
                "adjustments. charge_type indicates whether a payment is pending (in an open "
                "aggregation) or completed (settlement issued). Join to stg_littlepay__settlements "
                "to resolve final financial state."
            ),
            version=3,
            created_by="user",
        ),
    ]
    graph = _graph(entity=micropayments, history=history)

    with patch("puxti.mcp_server._graph_connect", new=AsyncMock(return_value=graph)):
        from puxti.mcp_server import definition_history
        result = json.loads(
            await definition_history(entity_id="model.cal_itp.stg_littlepay__micropayments")
        )

    assert result["total_versions"] == 3
    assert result["history"][0]["version"] == 1
    assert result["history"][0]["created_by"] == "llm"
    assert result["history"][1]["version"] == 2
    assert "DEBIT" in result["history"][1]["description"]
    assert result["history"][2]["version"] == 3
    assert "stg_littlepay__settlements" in result["history"][2]["description"]
    assert result["history"][2]["created_by"] == "user"


async def test_definition_history_authorisations_initial_scan_only():
    """Authorisations model was scanned but never manually refined — one version, created by llm."""
    auth = _model("stg_littlepay__authorisations")
    history = [
        _definition(
            "stg_littlepay__authorisations",
            description="One row per authorisation request sent to the payment acquirer for a Littlepay aggregation.",
            version=1,
            created_by="llm",
        )
    ]
    graph = _graph(entity=auth, history=history)

    with patch("puxti.mcp_server._graph_connect", new=AsyncMock(return_value=graph)):
        from puxti.mcp_server import definition_history
        result = json.loads(
            await definition_history(entity_id="model.cal_itp.stg_littlepay__authorisations")
        )

    assert result["total_versions"] == 1
    assert result["history"][0]["created_by"] == "llm"
    assert "acquirer" in result["history"][0]["description"]


# ── Scenario 6: entity-not-found for a plausible but misspelled model ────────
# Engineer types the wrong model name — typo in the prefix.


async def test_impact_misspelled_model_returns_error():
    graph = _graph(entity=None)

    with patch("puxti.mcp_server._graph_connect", new=AsyncMock(return_value=graph)):
        from puxti.mcp_server import impact_of_change
        result = json.loads(
            await impact_of_change(entity_id="model.cal_itp.stg_littlepay__microp4yments")
        )

    assert "error" in result
    assert "puxti scan" in result["error"]


async def test_consumers_gtfs_model_not_yet_scanned_returns_error():
    graph = _graph(entity=None)

    with patch("puxti.mcp_server._graph_connect", new=AsyncMock(return_value=graph)):
        from puxti.mcp_server import consumers
        result = json.loads(
            await consumers(entity_id="model.cal_itp.fct_observed_trips")
        )

    assert "error" in result
