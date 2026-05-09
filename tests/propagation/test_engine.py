from unittest.mock import AsyncMock, MagicMock

import pytest

from puxti.models import ChangeType, FileDiff, PropagationResult, SemanticChangeEvent
from puxti.propagation.engine import PropagationEngine


# ── Fixtures ──────────────────────────────────────────────────────────────────

EVENT = SemanticChangeEvent(
    change_event_id="evt-001",
    entity_id="model.jaffle_shop.orders.order_date",
    change_type=ChangeType.STRUCTURAL,
    semantic_context="order_date renamed to recorded_date for clarity.",
    affected_entity_ids=["model.jaffle_shop.orders", "model.jaffle_shop.revenue"],
    reasoning="Column rename cascades to all referencing models.",
    change={"before": {"name": "order_date"}, "after": {"name": "recorded_date"}},
)

ORDERS_DIFF = FileDiff(
    file_path="models/orders.sql",
    before="select order_date from stg_orders",
    after="select recorded_date from stg_orders",
    connector="dbt",
    description="Renamed order_date → recorded_date in orders",
)

REVENUE_DIFF = FileDiff(
    file_path="models/revenue.sql",
    before="select date_trunc('month', order_date) as month from orders",
    after="select date_trunc('month', recorded_date) as month from orders",
    connector="dbt",
    description="Renamed order_date → recorded_date in revenue",
)


def _make_connector(diffs: list[FileDiff], unverified: list[str] | None = None) -> MagicMock:
    connector = MagicMock()
    connector.generate_changes = AsyncMock(return_value=(diffs, unverified or []))
    return connector


# ── propagate ─────────────────────────────────────────────────────────────────

async def test_propagate_returns_result_per_connector_with_diffs():
    engine = PropagationEngine(connectors=[_make_connector([ORDERS_DIFF, REVENUE_DIFF])])
    results = await engine.propagate(EVENT)

    assert len(results) == 1
    result = results[0]
    assert isinstance(result, PropagationResult)
    assert result.change_event_id == EVENT.change_event_id
    assert result.target_entity_id == EVENT.entity_id
    assert result.connector == "dbt"
    assert result.diffs == [ORDERS_DIFF, REVENUE_DIFF]


async def test_propagate_skips_connector_with_no_diffs():
    engine = PropagationEngine(
        connectors=[
            _make_connector([]),
            _make_connector([ORDERS_DIFF]),
        ]
    )
    results = await engine.propagate(EVENT)

    assert len(results) == 1
    assert results[0].diffs == [ORDERS_DIFF]


async def test_propagate_returns_empty_when_all_connectors_produce_no_diffs():
    engine = PropagationEngine(
        connectors=[_make_connector([]), _make_connector([])]
    )
    results = await engine.propagate(EVENT)

    assert results == []


async def test_propagate_returns_empty_when_no_connectors_registered():
    engine = PropagationEngine(connectors=[])
    results = await engine.propagate(EVENT)

    assert results == []


async def test_propagate_multiple_connectors_each_produce_result():
    dbt_diff = FileDiff(
        file_path="models/orders.sql",
        before="select order_date from stg_orders",
        after="select recorded_date from stg_orders",
        connector="dbt",
        description="dbt diff",
    )
    airflow_diff = FileDiff(
        file_path="dags/orders_dag.py",
        before="order_date = ...",
        after="recorded_date = ...",
        connector="airflow",
        description="airflow diff",
    )

    engine = PropagationEngine(
        connectors=[
            _make_connector([dbt_diff]),
            _make_connector([airflow_diff]),
        ]
    )
    results = await engine.propagate(EVENT)

    assert len(results) == 2
    connectors = {r.connector for r in results}
    assert connectors == {"dbt", "airflow"}


async def test_propagate_calls_generate_changes_with_event():
    connector = _make_connector([ORDERS_DIFF])
    engine = PropagationEngine(connectors=[connector])
    await engine.propagate(EVENT)

    connector.generate_changes.assert_awaited_once_with(EVENT)


async def test_propagate_result_has_pending_status_by_default():
    engine = PropagationEngine(connectors=[_make_connector([ORDERS_DIFF])])
    results = await engine.propagate(EVENT)

    assert results[0].status == "pending"
    assert results[0].pr_url is None
