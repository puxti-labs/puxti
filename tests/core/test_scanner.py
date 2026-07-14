"""Tests for SemanticScanner — Knowledge Graph bootstrapping."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from puxti.core.scanner import SemanticScanner, ScanResult
from puxti.models import Definition, Edge, EdgeType, Entity, EntityType, SemanticEdge


# ── Fixtures ──────────────────────────────────────────────────────────────────

ORDERS_ENTITY = Entity(
    id="model.demo_shop.orders",
    name="orders",
    type=EntityType.MODEL,
    source_connector="dbt",
)

ORDERS_ENTITY_WITH_YML_DESC = Entity(
    id="model.demo_shop.orders",
    name="orders",
    type=EntityType.MODEL,
    source_connector="dbt",
    metadata={"description": "One row per order placed by a customer."},
)

CUSTOMERS_ENTITY = Entity(
    id="model.demo_shop.customers",
    name="customers",
    type=EntityType.MODEL,
    source_connector="dbt",
)

ORDERS_SQL = "select id, gross_revenue from stg_orders"
CUSTOMERS_SQL = "select customer_id, sum(gross_revenue) as lifetime_value from orders group by 1"


def _make_llm_response(payload: dict) -> MagicMock:
    block = MagicMock()
    block.text = json.dumps(payload)
    response = MagicMock()
    response.content = [block]
    return response


def _make_client(payload: dict) -> MagicMock:
    client = MagicMock()
    client.messages = MagicMock()
    client.messages.create = AsyncMock(return_value=_make_llm_response(payload))
    return client


def _make_graph() -> MagicMock:
    graph = MagicMock()
    graph.upsert_entity = AsyncMock()
    graph.upsert_edge = AsyncMock()
    graph.upsert_definition = AsyncMock()
    graph.upsert_semantic_edge = AsyncMock()
    graph.get_latest_definition = AsyncMock(return_value=None)
    return graph


def _make_connector(
    entities: list[Entity] | None = None,
    lineage: list[Edge] | None = None,
    sql_map: dict[str, str] | None = None,
) -> MagicMock:
    connector = MagicMock()
    connector.extract_entities = AsyncMock(return_value=entities or [ORDERS_ENTITY, CUSTOMERS_ENTITY])
    connector.extract_lineage = AsyncMock(return_value=lineage or [])
    connector.get_model_sql_map = MagicMock(return_value=sql_map or {
        ORDERS_ENTITY.id: ORDERS_SQL,
        CUSTOMERS_ENTITY.id: CUSTOMERS_SQL,
    })
    return connector


def _make_console(inputs: list[str] | None = None) -> MagicMock:
    console = MagicMock()
    console.status = MagicMock(return_value=MagicMock(__enter__=MagicMock(return_value=None), __exit__=MagicMock(return_value=False)))
    if inputs:
        console.input = MagicMock(side_effect=inputs)
    else:
        console.input = MagicMock(return_value="y")
    return console


# ── infer_definition ──────────────────────────────────────────────────────────

async def test_infer_definition_calls_llm_with_sql():
    payload = {"definition": "Orders mart with gross revenue per order."}
    client = _make_client(payload)
    scanner = SemanticScanner(client=client)

    result = await scanner.infer_definition(ORDERS_ENTITY, ORDERS_SQL, [])

    assert result == "Orders mart with gross revenue per order."
    client.messages.create.assert_awaited_once()
    call_kwargs = client.messages.create.call_args.kwargs
    assert ORDERS_SQL in call_kwargs["messages"][0]["content"]


async def test_infer_definition_includes_upstream_context():
    payload = {"definition": "Customer LTV aggregated from orders."}
    client = _make_client(payload)
    scanner = SemanticScanner(client=client)

    await scanner.infer_definition(CUSTOMERS_ENTITY, CUSTOMERS_SQL, ["orders"])

    content = client.messages.create.call_args.kwargs["messages"][0]["content"]
    assert "orders" in content


async def test_infer_definition_includes_yml_description():
    payload = {"definition": "Orders mart with gross revenue per order."}
    client = _make_client(payload)
    scanner = SemanticScanner(client=client)

    await scanner.infer_definition(ORDERS_ENTITY_WITH_YML_DESC, ORDERS_SQL, [])

    content = client.messages.create.call_args.kwargs["messages"][0]["content"]
    assert "One row per order placed by a customer." in content


async def test_infer_definition_omits_yml_context_when_empty():
    payload = {"definition": "Orders mart."}
    client = _make_client(payload)
    scanner = SemanticScanner(client=client)

    await scanner.infer_definition(ORDERS_ENTITY, ORDERS_SQL, [])

    content = client.messages.create.call_args.kwargs["messages"][0]["content"]
    assert "dbt yml description" not in content


async def test_infer_definition_strips_markdown_fence():
    block = MagicMock()
    block.text = f'```json\n{json.dumps({"definition": "Clean definition."})}\n```'
    response = MagicMock()
    response.content = [block]
    client = MagicMock()
    client.messages = MagicMock()
    client.messages.create = AsyncMock(return_value=response)

    scanner = SemanticScanner(client=client)
    result = await scanner.infer_definition(ORDERS_ENTITY, ORDERS_SQL, [])
    assert result == "Clean definition."


# ── estimate_scan_cost ────────────────────────────────────────────────────────

async def test_estimate_scan_cost_returns_breakdown():
    count_response = MagicMock()
    count_response.input_tokens = 200

    client = MagicMock()
    client.messages = MagicMock()
    client.messages.count_tokens = AsyncMock(return_value=count_response)

    scanner = SemanticScanner(client=client)
    connector = _make_connector()

    estimate = await scanner.estimate_scan_cost(connector)

    # count_tokens called once per model + once for edges = 3 calls
    assert client.messages.count_tokens.await_count == 3
    assert estimate["models"] == 2
    assert estimate["def_input_tokens"] == 400  # 2 models × 200
    assert estimate["edges_input_tokens"] == 200
    assert estimate["total_input_tokens"] == 600
    assert estimate["estimated_cost_usd"] > 0


async def test_estimate_scan_cost_skips_models_with_no_sql():
    count_response = MagicMock()
    count_response.input_tokens = 150

    client = MagicMock()
    client.messages = MagicMock()
    client.messages.count_tokens = AsyncMock(return_value=count_response)

    scanner = SemanticScanner(client=client)
    # connector returns one model with SQL, one without
    connector = _make_connector(sql_map={ORDERS_ENTITY.id: ORDERS_SQL})

    estimate = await scanner.estimate_scan_cost(connector)

    # 1 definition call + 1 edges call
    assert client.messages.count_tokens.await_count == 2
    assert estimate["def_input_tokens"] == 150


# ── propose_semantic_edges ────────────────────────────────────────────────────

async def test_propose_semantic_edges_returns_edges():
    payload = {"edges": [{
        "from_entity_id": CUSTOMERS_ENTITY.id,
        "to_entity_id": ORDERS_ENTITY.id,
        "type": "derived_from",
        "description": "CLV is derived from gross_revenue in orders.",
    }]}
    client = _make_client(payload)
    scanner = SemanticScanner(client=client)

    definitions = {
        ORDERS_ENTITY.id: "Orders with gross revenue.",
        CUSTOMERS_ENTITY.id: "Customer lifetime value from orders.",
    }
    edges, truncated = await scanner.propose_semantic_edges(
        [ORDERS_ENTITY, CUSTOMERS_ENTITY], definitions
    )

    assert len(edges) == 1
    assert edges[0].from_entity_id == CUSTOMERS_ENTITY.id
    assert edges[0].to_entity_id == ORDERS_ENTITY.id
    assert edges[0].type == EdgeType.DERIVED_FROM
    assert truncated == 0


async def test_propose_semantic_edges_drops_hallucinated_ids():
    payload = {"edges": [{
        "from_entity_id": "model.demo_shop.nonexistent",
        "to_entity_id": ORDERS_ENTITY.id,
        "type": "derived_from",
        "description": "Hallucinated.",
    }]}
    client = _make_client(payload)
    scanner = SemanticScanner(client=client)

    edges, truncated = await scanner.propose_semantic_edges(
        [ORDERS_ENTITY, CUSTOMERS_ENTITY],
        {ORDERS_ENTITY.id: "def", CUSTOMERS_ENTITY.id: "def"},
    )
    assert edges == []
    assert truncated == 0


async def test_propose_semantic_edges_drops_edges_not_from_source_batch():
    """LLM returning edges where FROM is not in the source batch must be dropped.

    This guards the bounded-output contract: each call only accepts edges
    FROM the declared source entities, not edges between arbitrary entities.
    """
    payload = {"edges": [
        # Valid — from CUSTOMERS (source) to ORDERS (context)
        {
            "from_entity_id": CUSTOMERS_ENTITY.id,
            "to_entity_id": ORDERS_ENTITY.id,
            "type": "derived_from",
            "description": "CLV from orders.",
        },
        # Invalid — ORDERS is not in the source batch for this call
        {
            "from_entity_id": ORDERS_ENTITY.id,
            "to_entity_id": CUSTOMERS_ENTITY.id,
            "type": "derived_from",
            "description": "Should be dropped.",
        },
    ]}
    # Simulate: source batch = [CUSTOMERS_ENTITY] only
    block = MagicMock()
    block.text = json.dumps(payload)
    response = MagicMock()
    response.content = [block]
    response.stop_reason = "end_turn"
    client = MagicMock()
    client.messages = MagicMock()
    client.messages.create = AsyncMock(return_value=response)

    scanner = SemanticScanner(client=client)
    known_ids = {ORDERS_ENTITY.id, CUSTOMERS_ENTITY.id}
    context_block = f"- {ORDERS_ENTITY.id}: orders\n- {CUSTOMERS_ENTITY.id}: customers"

    edges, truncated = await scanner._edges_for_sources(
        source_entities=[CUSTOMERS_ENTITY],  # ORDERS is NOT a source here
        definitions={CUSTOMERS_ENTITY.id: "Customer lifetime value."},
        context_block=context_block,
        known_ids=known_ids,
    )

    assert len(edges) == 1
    assert edges[0].from_entity_id == CUSTOMERS_ENTITY.id
    assert truncated is False


async def test_propose_semantic_edges_recovers_from_max_tokens():
    """When a batch hits max_tokens, return empty for that batch and increment truncated count."""
    block = MagicMock()
    block.text = '{"edges": [{"from_entity_id": "model.demo_shop.customers", "to'  # truncated
    response = MagicMock()
    response.content = [block]
    response.stop_reason = "max_tokens"
    client = MagicMock()
    client.messages = MagicMock()
    client.messages.create = AsyncMock(return_value=response)

    scanner = SemanticScanner(client=client)
    edges, truncated = await scanner.propose_semantic_edges(
        [ORDERS_ENTITY, CUSTOMERS_ENTITY],
        {ORDERS_ENTITY.id: "def", CUSTOMERS_ENTITY.id: "def"},
    )

    assert edges == []
    assert truncated == 1


# ── scan — auto mode ──────────────────────────────────────────────────────────

async def test_scan_auto_upserts_entities_and_lineage():
    client = MagicMock()
    client.messages = MagicMock()
    client.messages.create = AsyncMock(side_effect=[
        _make_llm_response({"definition": "Orders model."}),
        _make_llm_response({"definition": "Customers model."}),
        _make_llm_response({"edges": []}),
    ])
    scanner = SemanticScanner(client=client)
    graph = _make_graph()
    connector = _make_connector()
    console = _make_console(inputs=["y", "n"])  # confirm defs, skip edges

    result = await scanner.scan(connector, graph, interactive=False, console=console)

    assert graph.upsert_entity.await_count == 2
    assert isinstance(result, ScanResult)
    assert result.entities_upserted == 2


async def test_scan_auto_writes_confirmed_definitions():
    client = MagicMock()
    client.messages = MagicMock()
    client.messages.create = AsyncMock(side_effect=[
        _make_llm_response({"definition": "Orders model."}),
        _make_llm_response({"definition": "Customers model."}),
        _make_llm_response({"edges": []}),
    ])
    scanner = SemanticScanner(client=client)
    graph = _make_graph()
    connector = _make_connector()
    console = _make_console(inputs=["y", "n"])

    result = await scanner.scan(connector, graph, interactive=False, console=console)

    assert graph.upsert_definition.await_count == 2
    assert result.definitions_written == 2


async def test_scan_auto_writes_no_definitions_when_cancelled():
    client = MagicMock()
    client.messages = MagicMock()
    client.messages.create = AsyncMock(side_effect=[
        _make_llm_response({"definition": "Orders model."}),
        _make_llm_response({"definition": "Customers model."}),
    ])
    scanner = SemanticScanner(client=client)
    graph = _make_graph()
    connector = _make_connector()
    console = _make_console(inputs=["n"])  # cancel

    result = await scanner.scan(connector, graph, interactive=False, console=console)

    graph.upsert_definition.assert_not_awaited()
    assert result.definitions_written == 0


async def test_scan_auto_increments_version_for_existing_definition():
    existing = Definition(
        entity_id=ORDERS_ENTITY.id,
        description="Old definition.",
        version=2,
        created_by="user",
    )
    client = MagicMock()
    client.messages = MagicMock()
    client.messages.create = AsyncMock(side_effect=[
        _make_llm_response({"definition": "Updated orders model."}),
        _make_llm_response({"definition": "Customers model."}),
        _make_llm_response({"edges": []}),
    ])
    scanner = SemanticScanner(client=client)
    graph = _make_graph()
    graph.get_latest_definition = AsyncMock(side_effect=lambda eid: existing if eid == ORDERS_ENTITY.id else None)
    connector = _make_connector()
    console = _make_console(inputs=["y", "n"])

    await scanner.scan(connector, graph, interactive=False, console=console)

    definitions_written = [
        call.args[0] for call in graph.upsert_definition.call_args_list
    ]
    orders_def = next(d for d in definitions_written if d.entity_id == ORDERS_ENTITY.id)
    assert orders_def.version == 3


# ── scan — semantic edges ─────────────────────────────────────────────────────

async def test_scan_writes_confirmed_semantic_edges():
    edge_payload = {"edges": [{
        "from_entity_id": CUSTOMERS_ENTITY.id,
        "to_entity_id": ORDERS_ENTITY.id,
        "type": "derived_from",
        "description": "CLV from orders.",
    }]}
    client = MagicMock()
    client.messages = MagicMock()
    client.messages.create = AsyncMock(side_effect=[
        _make_llm_response({"definition": "Orders."}),
        _make_llm_response({"definition": "Customers."}),
        _make_llm_response(edge_payload),
    ])
    scanner = SemanticScanner(client=client)
    graph = _make_graph()
    connector = _make_connector()
    console = _make_console(inputs=["y", "y"])  # confirm defs, confirm edges

    result = await scanner.scan(connector, graph, interactive=False, console=console)

    assert result.semantic_edges_written == 1


# ── concurrency ───────────────────────────────────────────────────────────────

def _make_entities(n: int) -> list[Entity]:
    return [
        Entity(
            id=f"model.demo_shop.m{i}",
            name=f"m{i}",
            type=EntityType.MODEL,
            source_connector="dbt",
        )
        for i in range(n)
    ]


async def test_auto_definitions_respect_concurrency_cap(monkeypatch):
    """Definition calls overlap, but never more than llm_concurrency in flight."""
    import asyncio

    from puxti.core import scanner as scanner_module

    monkeypatch.setattr(scanner_module.settings, "llm_concurrency", 3)

    in_flight = 0
    max_in_flight = 0

    async def _create(**kwargs):
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return _make_llm_response({"definition": "A definition."})

    client = MagicMock()
    client.messages = MagicMock()
    client.messages.create = AsyncMock(side_effect=_create)
    scanner = SemanticScanner(client=client)

    entities = _make_entities(8)
    sql_map = {e.id: f"select 1 as c{i}" for i, e in enumerate(entities)}
    console = _make_console(inputs=["y"])

    generated = await scanner._auto_definitions(entities, sql_map, {}, console)

    assert len(generated) == 8
    assert max_in_flight == 3  # parallel, but capped


async def test_auto_definitions_fail_fast_with_original_error(monkeypatch):
    """First API error propagates unwrapped (not as ExceptionGroup)."""
    import asyncio

    from puxti.core import scanner as scanner_module

    monkeypatch.setattr(scanner_module.settings, "llm_concurrency", 4)

    calls = 0

    async def _create(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("boom")
        await asyncio.sleep(0.05)
        return _make_llm_response({"definition": "A definition."})

    client = MagicMock()
    client.messages = MagicMock()
    client.messages.create = AsyncMock(side_effect=_create)
    scanner = SemanticScanner(client=client)

    entities = _make_entities(6)
    sql_map = {e.id: "select 1" for e in entities}
    console = _make_console(inputs=["y"])

    with pytest.raises(RuntimeError, match="boom"):
        await scanner._auto_definitions(entities, sql_map, {}, console)


async def test_propose_semantic_edges_merges_parallel_batches_in_batch_order(monkeypatch):
    """With >_EDGES_SOURCE_BATCH entities, batches run in parallel but the
    merged edge list follows batch order even when the second batch finishes first."""
    import asyncio

    from puxti.core import scanner as scanner_module

    monkeypatch.setattr(scanner_module.settings, "llm_concurrency", 2)

    entities = _make_entities(12)  # 2 batches: e0..e9, e10..e11
    definitions = {e.id: "A definition." for e in entities}
    batch1_edge = {
        "from_entity_id": entities[0].id,
        "to_entity_id": entities[1].id,
        "type": "derived_from",
        "description": "from batch 1",
    }
    batch2_edge = {
        "from_entity_id": entities[10].id,
        "to_entity_id": entities[0].id,
        "type": "derived_from",
        "description": "from batch 2",
    }

    async def _create(**kwargs):
        content = kwargs["messages"][0]["content"]
        # The FROM line names the source entities of this batch
        if f"FROM these source entities only: {entities[0].id}" in content:
            await asyncio.sleep(0.03)  # batch 1 finishes LAST
            return _make_llm_response({"edges": [batch1_edge]})
        return _make_llm_response({"edges": [batch2_edge]})

    client = MagicMock()
    client.messages = MagicMock()
    client.messages.create = AsyncMock(side_effect=_create)
    scanner = SemanticScanner(client=client)

    edges, truncated = await scanner.propose_semantic_edges(entities, definitions)

    assert truncated == 0
    assert [e.description for e in edges] == ["from batch 1", "from batch 2"]
    assert client.messages.create.await_count == 2


# ── explicit confirmation — blank input never accepts ─────────────────────────

async def test_auto_definitions_blank_input_cancels():
    payload = {"definition": "Orders model."}
    client = _make_client(payload)
    scanner = SemanticScanner(client=client)
    console = _make_console(inputs=[""])  # blank at "Confirm all definitions?"

    confirmed = await scanner._auto_definitions(
        [ORDERS_ENTITY], {ORDERS_ENTITY.id: ORDERS_SQL}, {}, console
    )

    assert confirmed == {}


async def test_interactive_definitions_blank_input_skips():
    payload = {"definition": "Orders model."}
    client = _make_client(payload)
    scanner = SemanticScanner(client=client)
    console = _make_console(inputs=[""])  # blank at per-model "Confirm?"

    confirmed = await scanner._interactive_definitions(
        [ORDERS_ENTITY], {ORDERS_ENTITY.id: ORDERS_SQL}, {}, console
    )

    assert confirmed == {}


async def test_confirm_edges_blank_input_cancels():
    scanner = SemanticScanner(client=_make_client({}))
    edge = SemanticEdge(
        from_entity_id=CUSTOMERS_ENTITY.id,
        to_entity_id=ORDERS_ENTITY.id,
        type=EdgeType.DERIVED_FROM,
        description="CLV from orders.",
        created_by="scan",
    )
    console = _make_console(inputs=[""])  # blank at "Confirm all edges?"

    confirmed = await scanner._confirm_edges([edge], console)

    assert confirmed == []
