"""Tests for SemanticScanner — Knowledge Graph bootstrapping."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from puxti.core.graph import SqliteKnowledgeGraph
from puxti.core.scanner import ScanResult, SemanticScanner
from puxti.llm import INPUT_COST_PER_MTOK, OUTPUT_COST_PER_MTOK, LLMResponse, TokenCount
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


def _make_llm_response(payload: dict) -> LLMResponse:
    return LLMResponse(text=json.dumps(payload), truncated=False)


def _make_backend(payload: dict) -> MagicMock:
    backend = MagicMock()
    backend.input_cost_per_mtok = INPUT_COST_PER_MTOK
    backend.output_cost_per_mtok = OUTPUT_COST_PER_MTOK
    backend.complete = AsyncMock(return_value=_make_llm_response(payload))
    return backend


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
    seeds: dict[str, str] | None = None,
) -> MagicMock:
    connector = MagicMock()
    connector.extract_entities = AsyncMock(
        return_value=entities or [ORDERS_ENTITY, CUSTOMERS_ENTITY]
    )
    connector.extract_lineage = AsyncMock(return_value=lineage or [])
    connector.get_model_sql_map = MagicMock(return_value=sql_map or {
        ORDERS_ENTITY.id: ORDERS_SQL,
        CUSTOMERS_ENTITY.id: CUSTOMERS_SQL,
    })
    connector.get_seed_definitions = MagicMock(return_value=seeds or {})
    return connector


def _make_console(inputs: list[str] | None = None) -> MagicMock:
    console = MagicMock()
    console.status = MagicMock(
        return_value=MagicMock(
            __enter__=MagicMock(return_value=None),
            __exit__=MagicMock(return_value=False),
        )
    )
    if inputs:
        console.input = MagicMock(side_effect=inputs)
    else:
        console.input = MagicMock(return_value="y")
    return console


# ── infer_definition ──────────────────────────────────────────────────────────

async def test_infer_definition_calls_llm_with_sql():
    payload = {"definition": "Orders mart with gross revenue per order."}
    backend = _make_backend(payload)
    scanner = SemanticScanner(backend=backend)

    result = await scanner.infer_definition(ORDERS_ENTITY, ORDERS_SQL, [])

    assert result == "Orders mart with gross revenue per order."
    backend.complete.assert_awaited_once()
    call_kwargs = backend.complete.call_args.kwargs
    assert ORDERS_SQL in call_kwargs["user_message"]


async def test_infer_definition_includes_upstream_context():
    payload = {"definition": "Customer LTV aggregated from orders."}
    backend = _make_backend(payload)
    scanner = SemanticScanner(backend=backend)

    await scanner.infer_definition(CUSTOMERS_ENTITY, CUSTOMERS_SQL, ["orders"])

    content = backend.complete.call_args.kwargs["user_message"]
    assert "orders" in content


async def test_infer_definition_includes_yml_description():
    payload = {"definition": "Orders mart with gross revenue per order."}
    backend = _make_backend(payload)
    scanner = SemanticScanner(backend=backend)

    await scanner.infer_definition(ORDERS_ENTITY_WITH_YML_DESC, ORDERS_SQL, [])

    content = backend.complete.call_args.kwargs["user_message"]
    assert "One row per order placed by a customer." in content


async def test_infer_definition_omits_yml_context_when_empty():
    payload = {"definition": "Orders mart."}
    backend = _make_backend(payload)
    scanner = SemanticScanner(backend=backend)

    await scanner.infer_definition(ORDERS_ENTITY, ORDERS_SQL, [])

    content = backend.complete.call_args.kwargs["user_message"]
    assert "dbt yml description" not in content


async def test_infer_definition_strips_markdown_fence():
    backend = MagicMock()
    backend.complete = AsyncMock(return_value=LLMResponse(
        text=f'```json\n{json.dumps({"definition": "Clean definition."})}\n```',
        truncated=False,
    ))

    scanner = SemanticScanner(backend=backend)
    result = await scanner.infer_definition(ORDERS_ENTITY, ORDERS_SQL, [])
    assert result == "Clean definition."


# ── estimate_scan_cost ────────────────────────────────────────────────────────

async def test_estimate_scan_cost_returns_breakdown():
    backend = _make_backend({})
    backend.count_input_tokens = AsyncMock(return_value=TokenCount(tokens=200, exact=True))

    scanner = SemanticScanner(backend=backend)
    connector = _make_connector()

    estimate = await scanner.estimate_scan_cost(connector)

    # count_tokens called once per model + once for edges = 3 calls
    assert backend.count_input_tokens.await_count == 3
    assert estimate["models"] == 2
    assert estimate["def_input_tokens"] == 400  # 2 models × 200
    assert estimate["edges_input_tokens"] == 200
    assert estimate["total_input_tokens"] == 600
    assert estimate["estimated_cost_usd"] > 0


async def test_estimate_scan_cost_skips_models_with_no_sql():
    backend = _make_backend({})
    backend.count_input_tokens = AsyncMock(return_value=TokenCount(tokens=150, exact=True))

    scanner = SemanticScanner(backend=backend)
    # connector returns one model with SQL, one without
    connector = _make_connector(sql_map={ORDERS_ENTITY.id: ORDERS_SQL})

    estimate = await scanner.estimate_scan_cost(connector)

    # 1 definition call + 1 edges call
    assert backend.count_input_tokens.await_count == 2
    assert estimate["def_input_tokens"] == 150


# ── propose_semantic_edges ────────────────────────────────────────────────────

async def test_propose_semantic_edges_returns_edges():
    payload = {"edges": [{
        "from_entity_id": CUSTOMERS_ENTITY.id,
        "to_entity_id": ORDERS_ENTITY.id,
        "type": "derived_from",
        "description": "CLV is derived from gross_revenue in orders.",
    }]}
    backend = _make_backend(payload)
    scanner = SemanticScanner(backend=backend)

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


@pytest.mark.parametrize("invalid_type", ["invented", None, 42, ["derived_from"], {}])
async def test_propose_semantic_edges_skips_invalid_types(
    invalid_type: object, caplog: pytest.LogCaptureFixture,
) -> None:
    first = {
        "from_entity_id": CUSTOMERS_ENTITY.id,
        "to_entity_id": ORDERS_ENTITY.id,
        "type": "derived_from",
        "description": "CLV from orders.",
    }
    last = {
        "from_entity_id": ORDERS_ENTITY.id,
        "to_entity_id": CUSTOMERS_ENTITY.id,
        "type": "references",
        "description": "Orders refer to customers.",
    }
    backend = _make_backend({"edges": [first, {**first, "type": invalid_type}, last]})
    scanner = SemanticScanner(backend=backend)

    edges, truncated = await scanner.propose_semantic_edges(
        [ORDERS_ENTITY, CUSTOMERS_ENTITY],
        {ORDERS_ENTITY.id: "Orders.", CUSTOMERS_ENTITY.id: "Customers."},
    )

    assert [(edge.from_entity_id, edge.to_entity_id, edge.type) for edge in edges] == [
        (CUSTOMERS_ENTITY.id, ORDERS_ENTITY.id, EdgeType.DERIVED_FROM),
        (ORDERS_ENTITY.id, CUSTOMERS_ENTITY.id, EdgeType.REFERENCES),
    ]
    assert truncated == 0
    assert "Skipping semantic edge with invalid type" in caplog.text


@pytest.mark.parametrize("bad_description", ["<missing>", None, "", "   ", 42, ["CLV"]])
async def test_propose_semantic_edges_skips_missing_descriptions(
    bad_description: object, caplog: pytest.LogCaptureFixture,
) -> None:
    valid = {
        "from_entity_id": CUSTOMERS_ENTITY.id,
        "to_entity_id": ORDERS_ENTITY.id,
        "type": "derived_from",
        "description": "CLV from orders.",
    }
    bad = {**valid, "from_entity_id": ORDERS_ENTITY.id, "to_entity_id": CUSTOMERS_ENTITY.id}
    if bad_description == "<missing>":
        del bad["description"]
    else:
        bad["description"] = bad_description
    backend = _make_backend({"edges": [bad, valid]})

    edges, truncated = await SemanticScanner(backend=backend).propose_semantic_edges(
        [ORDERS_ENTITY, CUSTOMERS_ENTITY],
        {ORDERS_ENTITY.id: "Orders.", CUSTOMERS_ENTITY.id: "Customers."},
    )

    assert [(edge.from_entity_id, edge.to_entity_id, edge.description) for edge in edges] == [
        (CUSTOMERS_ENTITY.id, ORDERS_ENTITY.id, "CLV from orders."),
    ]
    assert truncated == 0
    assert "Skipping semantic edge without a description" in caplog.text


@pytest.mark.parametrize("bad_entry", [None, "derived_from", 42, ["edge"]])
async def test_propose_semantic_edges_skips_non_object_entries(
    bad_entry: object, caplog: pytest.LogCaptureFixture,
) -> None:
    valid = {
        "from_entity_id": CUSTOMERS_ENTITY.id,
        "to_entity_id": ORDERS_ENTITY.id,
        "type": "derived_from",
        "description": "CLV from orders.",
    }
    backend = _make_backend({"edges": [bad_entry, valid]})

    edges, truncated = await SemanticScanner(backend=backend).propose_semantic_edges(
        [ORDERS_ENTITY, CUSTOMERS_ENTITY],
        {ORDERS_ENTITY.id: "Orders.", CUSTOMERS_ENTITY.id: "Customers."},
    )

    assert [(edge.from_entity_id, edge.to_entity_id) for edge in edges] == [
        (CUSTOMERS_ENTITY.id, ORDERS_ENTITY.id),
    ]
    assert truncated == 0
    assert "Skipping semantic edge proposal that is not an object" in caplog.text


@pytest.mark.parametrize("payload", [{"edges": None}, {}])
async def test_propose_semantic_edges_treats_null_edges_as_none(payload: dict) -> None:
    edges, truncated = await SemanticScanner(backend=_make_backend(payload)).propose_semantic_edges(
        [ORDERS_ENTITY, CUSTOMERS_ENTITY],
        {ORDERS_ENTITY.id: "Orders.", CUSTOMERS_ENTITY.id: "Customers."},
    )

    assert edges == []
    assert truncated == 0


_UNUSABLE_SHAPES = [None, [], [{"edges": []}], "edges", {"edges": "none"}, {"edges": {}}]


@pytest.mark.parametrize("payload", _UNUSABLE_SHAPES)
async def test_propose_semantic_edges_skips_batch_with_unusable_shape(
    payload: object, caplog: pytest.LogCaptureFixture,
) -> None:
    backend = _make_backend({})
    backend.complete = AsyncMock(
        return_value=LLMResponse(text=json.dumps(payload), truncated=False)
    )

    edges, truncated = await SemanticScanner(backend=backend).propose_semantic_edges(
        [ORDERS_ENTITY, CUSTOMERS_ENTITY],
        {ORDERS_ENTITY.id: "Orders.", CUSTOMERS_ENTITY.id: "Customers."},
    )

    assert edges == []
    assert truncated == 0
    assert 'not an object with an "edges" list' in caplog.text


@pytest.mark.parametrize("payload", _UNUSABLE_SHAPES)
async def test_propose_semantic_edges_counts_truncated_unusable_shape(payload: object) -> None:
    backend = _make_backend({})
    backend.complete = AsyncMock(
        return_value=LLMResponse(text=json.dumps(payload), truncated=True)
    )

    edges, truncated = await SemanticScanner(backend=backend).propose_semantic_edges(
        [ORDERS_ENTITY, CUSTOMERS_ENTITY],
        {ORDERS_ENTITY.id: "Orders.", CUSTOMERS_ENTITY.id: "Customers."},
    )

    assert edges == []
    assert truncated == 1


async def test_propose_semantic_edges_keeps_other_batches_after_unusable_shape() -> None:
    entities = _make_entities(2 * SemanticScanner._EDGES_SOURCE_BATCH)
    batch_size = SemanticScanner._EDGES_SOURCE_BATCH
    valid = {
        "from_entity_id": entities[batch_size].id,
        "to_entity_id": entities[0].id,
        "type": "derived_from",
        "description": "From the first batch.",
    }

    async def complete(*, system: str, user_message: str, max_tokens: int) -> LLMResponse:
        if entities[batch_size].id in user_message.split("FROM these source entities only:")[1]:
            return _make_llm_response({"edges": [valid]})
        return LLMResponse(text="[]", truncated=False)

    backend = _make_backend({})
    backend.complete = AsyncMock(side_effect=complete)

    edges, truncated = await SemanticScanner(backend=backend).propose_semantic_edges(
        entities, {e.id: "Def." for e in entities},
    )

    assert [(edge.from_entity_id, edge.to_entity_id) for edge in edges] == [
        (entities[batch_size].id, entities[0].id),
    ]
    assert truncated == 0


@pytest.mark.parametrize("field", ["from_entity_id", "to_entity_id"])
async def test_propose_semantic_edges_skips_non_string_ids(
    field: str, caplog: pytest.LogCaptureFixture,
) -> None:
    valid = {
        "from_entity_id": CUSTOMERS_ENTITY.id,
        "to_entity_id": ORDERS_ENTITY.id,
        "type": "derived_from",
        "description": "CLV from orders.",
    }
    backend = _make_backend({"edges": [{**valid, field: [ORDERS_ENTITY.id]}, valid]})

    edges, truncated = await SemanticScanner(backend=backend).propose_semantic_edges(
        [ORDERS_ENTITY, CUSTOMERS_ENTITY],
        {ORDERS_ENTITY.id: "Orders.", CUSTOMERS_ENTITY.id: "Customers."},
    )

    assert [(edge.from_entity_id, edge.to_entity_id) for edge in edges] == [
        (CUSTOMERS_ENTITY.id, ORDERS_ENTITY.id),
    ]
    assert truncated == 0
    assert "Skipping semantic edge proposal with non-string entity IDs" in caplog.text


async def test_propose_semantic_edges_strips_description() -> None:
    backend = _make_backend({"edges": [{
        "from_entity_id": CUSTOMERS_ENTITY.id,
        "to_entity_id": ORDERS_ENTITY.id,
        "type": "derived_from",
        "description": "  CLV from orders.\n\n",
    }]})

    edges, _ = await SemanticScanner(backend=backend).propose_semantic_edges(
        [ORDERS_ENTITY, CUSTOMERS_ENTITY],
        {ORDERS_ENTITY.id: "Orders.", CUSTOMERS_ENTITY.id: "Customers."},
    )

    assert edges[0].description == "CLV from orders."


async def test_propose_semantic_edges_defaults_missing_type() -> None:
    backend = _make_backend({"edges": [{
        "from_entity_id": CUSTOMERS_ENTITY.id,
        "to_entity_id": ORDERS_ENTITY.id,
        "description": "CLV from orders.",
    }]})

    edges, truncated = await SemanticScanner(backend=backend).propose_semantic_edges(
        [ORDERS_ENTITY, CUSTOMERS_ENTITY],
        {ORDERS_ENTITY.id: "Orders.", CUSTOMERS_ENTITY.id: "Customers."},
    )

    assert len(edges) == 1
    assert edges[0].type == EdgeType.DERIVED_FROM
    assert truncated == 0


async def test_propose_semantic_edges_drops_hallucinated_ids():
    payload = {"edges": [{
        "from_entity_id": "model.demo_shop.nonexistent",
        "to_entity_id": ORDERS_ENTITY.id,
        "type": "derived_from",
        "description": "Hallucinated.",
    }]}
    backend = _make_backend(payload)
    scanner = SemanticScanner(backend=backend)

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
    backend = MagicMock()
    backend.complete = AsyncMock(return_value=LLMResponse(
        text=json.dumps(payload), truncated=False,
    ))

    scanner = SemanticScanner(backend=backend)
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
    backend = MagicMock()
    backend.complete = AsyncMock(return_value=LLMResponse(
        text='{"edges": [{"from_entity_id": "model.demo_shop.customers", "to',  # truncated
        truncated=True,
    ))

    scanner = SemanticScanner(backend=backend)
    edges, truncated = await scanner.propose_semantic_edges(
        [ORDERS_ENTITY, CUSTOMERS_ENTITY],
        {ORDERS_ENTITY.id: "def", CUSTOMERS_ENTITY.id: "def"},
    )

    assert edges == []
    assert truncated == 1


# ── scan — auto mode ──────────────────────────────────────────────────────────

async def test_scan_auto_upserts_entities_and_lineage():
    backend = _make_backend({})
    backend.complete = AsyncMock(side_effect=[
        _make_llm_response({"definition": "Orders model."}),
        _make_llm_response({"definition": "Customers model."}),
        _make_llm_response({"edges": []}),
    ])
    scanner = SemanticScanner(backend=backend)
    graph = _make_graph()
    connector = _make_connector()
    console = _make_console(inputs=["y", "n"])  # confirm defs, skip edges

    result = await scanner.scan(connector, graph, interactive=False, console=console)

    assert graph.upsert_entity.await_count == 2
    assert isinstance(result, ScanResult)
    assert result.entities_upserted == 2


async def test_scan_auto_writes_confirmed_definitions():
    backend = _make_backend({})
    backend.complete = AsyncMock(side_effect=[
        _make_llm_response({"definition": "Orders model."}),
        _make_llm_response({"definition": "Customers model."}),
        _make_llm_response({"edges": []}),
    ])
    scanner = SemanticScanner(backend=backend)
    graph = _make_graph()
    connector = _make_connector()
    console = _make_console(inputs=["y", "n"])

    result = await scanner.scan(connector, graph, interactive=False, console=console)

    assert graph.upsert_definition.await_count == 2
    assert result.definitions_written == 2


async def test_scan_auto_writes_no_definitions_when_cancelled():
    backend = _make_backend({})
    backend.complete = AsyncMock(side_effect=[
        _make_llm_response({"definition": "Orders model."}),
        _make_llm_response({"definition": "Customers model."}),
    ])
    scanner = SemanticScanner(backend=backend)
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
    backend = _make_backend({})
    backend.complete = AsyncMock(side_effect=[
        _make_llm_response({"definition": "Updated orders model."}),
        _make_llm_response({"definition": "Customers model."}),
        _make_llm_response({"edges": []}),
    ])
    scanner = SemanticScanner(backend=backend)
    graph = _make_graph()
    graph.get_latest_definition = AsyncMock(
        side_effect=lambda eid: existing if eid == ORDERS_ENTITY.id else None
    )
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
    backend = _make_backend({})
    backend.complete = AsyncMock(side_effect=[
        _make_llm_response({"definition": "Orders."}),
        _make_llm_response({"definition": "Customers."}),
        _make_llm_response(edge_payload),
    ])
    scanner = SemanticScanner(backend=backend)
    graph = _make_graph()
    connector = _make_connector()
    console = _make_console(inputs=["y", "y"])  # confirm defs, confirm edges

    result = await scanner.scan(connector, graph, interactive=False, console=console)

    assert result.semantic_edges_written == 1


@pytest.mark.parametrize("confirmation", ["y", "n"])
async def test_scan_invalid_edge_type_preserves_confirmation(
    tmp_path: Path, confirmation: str,
) -> None:
    valid_edge = {
        "from_entity_id": CUSTOMERS_ENTITY.id,
        "to_entity_id": ORDERS_ENTITY.id,
        "type": "derived_from",
        "description": "CLV from orders.",
    }
    backend = _make_backend({})
    backend.complete = AsyncMock(side_effect=[
        _make_llm_response({"definition": "Orders."}),
        _make_llm_response({"definition": "Customers."}),
        _make_llm_response({"edges": [{**valid_edge, "type": "invented"}, valid_edge]}),
    ])
    graph = SqliteKnowledgeGraph(tmp_path / "graph.db")
    await graph.connect()
    try:
        result = await SemanticScanner(backend=backend).scan(
            _make_connector(), graph, interactive=False,
            console=_make_console(inputs=["y", confirmation]),
        )
        stored_edges = await graph.get_all_semantic_edges()
        assert result.definitions_written == 2
        assert result.semantic_edges_written == (1 if confirmation == "y" else 0)
        assert [(edge.from_entity_id, edge.to_entity_id, edge.type) for edge in stored_edges] == (
            [(CUSTOMERS_ENTITY.id, ORDERS_ENTITY.id, EdgeType.DERIVED_FROM)]
            if confirmation == "y" else []
        )
    finally:
        await graph.close()


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

    backend = _make_backend({})
    backend.complete = AsyncMock(side_effect=_create)
    scanner = SemanticScanner(backend=backend)

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

    backend = _make_backend({})
    backend.complete = AsyncMock(side_effect=_create)
    scanner = SemanticScanner(backend=backend)

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
        content = kwargs["user_message"]
        # The FROM line names the source entities of this batch
        if f"FROM these source entities only: {entities[0].id}" in content:
            await asyncio.sleep(0.03)  # batch 1 finishes LAST
            return _make_llm_response({"edges": [batch1_edge]})
        return _make_llm_response({"edges": [batch2_edge]})

    backend = _make_backend({})
    backend.complete = AsyncMock(side_effect=_create)
    scanner = SemanticScanner(backend=backend)

    edges, truncated = await scanner.propose_semantic_edges(entities, definitions)

    assert truncated == 0
    assert [e.description for e in edges] == ["from batch 1", "from batch 2"]
    assert backend.complete.await_count == 2


# ── explicit confirmation — blank input never accepts ─────────────────────────

async def test_auto_definitions_blank_input_cancels():
    payload = {"definition": "Orders model."}
    backend = _make_backend(payload)
    scanner = SemanticScanner(backend=backend)
    console = _make_console(inputs=[""])  # blank at "Confirm all definitions?"

    confirmed = await scanner._auto_definitions(
        [ORDERS_ENTITY], {ORDERS_ENTITY.id: ORDERS_SQL}, {}, console
    )

    assert confirmed == {}


async def test_interactive_definitions_blank_input_skips():
    payload = {"definition": "Orders model."}
    backend = _make_backend(payload)
    scanner = SemanticScanner(backend=backend)
    console = _make_console(inputs=[""])  # blank at per-model "Confirm?"

    confirmed = await scanner._interactive_definitions(
        [ORDERS_ENTITY], {ORDERS_ENTITY.id: ORDERS_SQL}, {}, console
    )

    assert confirmed == {}


async def test_confirm_edges_blank_input_cancels():
    scanner = SemanticScanner(backend=_make_backend({}))
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


async def test_estimate_scan_cost_omits_cost_when_pricing_unknown():
    backend = _make_backend({})
    backend.input_cost_per_mtok = None
    backend.output_cost_per_mtok = None
    backend.count_input_tokens = AsyncMock(return_value=TokenCount(tokens=200, exact=False))

    scanner = SemanticScanner(backend=backend)
    estimate = await scanner.estimate_scan_cost(_make_connector())

    assert estimate["total_input_tokens"] > 0
    assert estimate["tokens_exact"] is False
    assert estimate["estimated_cost_usd"] is None


# ── scan — cross-connector reference resolution ───────────────────────────────

VIEW_ENTITY = Entity(
    id="view.public.user_stats",
    name="user_stats",
    type=EntityType.VIEW,
    source_connector="sql_views",
)

def _sqlref_edge() -> Edge:
    # resolve_edges rewrites edges in place — each test needs a fresh one.
    return Edge(
        from_entity_id="view.public.user_stats",
        to_entity_id="sqlref.users",
        type=EdgeType.DEPENDS_ON,
        connector="sql_views",
        metadata={"raw_reference": "users"},
    )


async def test_scan_resolves_sqlref_edges_with_reference_index():
    scanner = SemanticScanner(backend=_make_backend({}))
    graph = _make_graph()
    connector = _make_connector(entities=[VIEW_ENTITY], lineage=[_sqlref_edge()], sql_map={})
    console = _make_console()

    await scanner.scan(
        connector, graph, interactive=False, console=console,
        reference_index={"users": "table.prisma.User"},
    )

    upserted = graph.upsert_edge.await_args_list[0].args[0]
    assert upserted.to_entity_id == "table.prisma.User"
    assert upserted.metadata["resolved_from"] == "users"


async def test_scan_keeps_and_reports_unresolved_sqlref_edges():
    scanner = SemanticScanner(backend=_make_backend({}))
    graph = _make_graph()
    connector = _make_connector(entities=[VIEW_ENTITY], lineage=[_sqlref_edge()], sql_map={})
    console = _make_console()

    await scanner.scan(
        connector, graph, interactive=False, console=console,
        reference_index={},
    )

    upserted = graph.upsert_edge.await_args_list[0].args[0]
    assert upserted.to_entity_id == "sqlref.users"
    printed = " ".join(str(c.args[0]) for c in console.print.call_args_list)
    assert "users" in printed and "unresolved" in printed


async def test_scan_without_reference_index_leaves_edges_untouched():
    scanner = SemanticScanner(backend=_make_backend({}))
    graph = _make_graph()
    connector = _make_connector(entities=[VIEW_ENTITY], lineage=[_sqlref_edge()], sql_map={})
    console = _make_console()

    await scanner.scan(connector, graph, interactive=False, console=console)

    upserted = graph.upsert_edge.await_args_list[0].args[0]
    assert upserted.to_entity_id == "sqlref.users"


# ── scan — connector-stated (seed) definitions ────────────────────────────────

AOV_METRIC = Entity(
    id="metric.demo_shop.average_order_value",
    name="average_order_value",
    type=EntityType.METRIC,
    source_connector="dbt",
    project="demo_shop",
)
AOV_TEXT = "Revenue per order.\n\nDerived metric: revenue / orders"


async def _real_graph():
    from pathlib import Path

    from puxti.core.graph import KnowledgeGraph

    graph = KnowledgeGraph(db_path=Path(":memory:"))
    await graph.connect()
    await graph.upsert_entity(AOV_METRIC)
    return graph


async def test_scan_writes_seed_definitions_after_confirmation():
    backend = _make_backend({})
    backend.complete = AsyncMock(side_effect=[
        _make_llm_response({"definition": "Orders model."}),
        _make_llm_response({"definition": "Customers model."}),
        _make_llm_response({"edges": []}),
    ])
    scanner = SemanticScanner(backend=backend)
    graph = _make_graph()
    connector = _make_connector(
        entities=[ORDERS_ENTITY, CUSTOMERS_ENTITY, AOV_METRIC],
        seeds={AOV_METRIC.id: AOV_TEXT},
    )
    console = _make_console(inputs=["y", "y", "n"])  # model defs, metric defs, edges

    result = await scanner.scan(connector, graph, interactive=False, console=console)

    written = {c.args[0].entity_id: c.args[0] for c in graph.upsert_definition.call_args_list}
    assert written[AOV_METRIC.id].description == AOV_TEXT
    assert written[AOV_METRIC.id].created_by == "scan"
    assert written[AOV_METRIC.id].version == 1
    assert result.definitions_written == 3


async def test_seed_definitions_skipped_when_not_confirmed():
    graph = await _real_graph()
    console = _make_console(inputs=[""])  # blank input never confirms
    written = await SemanticScanner(backend=_make_backend({}))._write_seed_definitions(
        {AOV_METRIC.id: AOV_TEXT}, [AOV_METRIC], graph, False, console
    )
    assert written == 0
    assert await graph.get_latest_definition(AOV_METRIC.id) is None
    await graph.close()


async def test_seed_definition_unchanged_is_not_rewritten():
    graph = await _real_graph()
    await graph.upsert_definition(Definition(
        entity_id=AOV_METRIC.id, description=AOV_TEXT, version=1, created_by="scan",
    ))
    console = _make_console()
    written = await SemanticScanner(backend=_make_backend({}))._write_seed_definitions(
        {AOV_METRIC.id: AOV_TEXT}, [AOV_METRIC], graph, False, console
    )
    assert written == 0
    console.input.assert_not_called()
    assert len(await graph.get_definition_history(AOV_METRIC.id)) == 1
    await graph.close()


async def test_seed_definition_changed_yaml_writes_new_version():
    graph = await _real_graph()
    await graph.upsert_definition(Definition(
        entity_id=AOV_METRIC.id, description="Old YAML text.", version=1, created_by="scan",
    ))
    written = await SemanticScanner(backend=_make_backend({}))._write_seed_definitions(
        {AOV_METRIC.id: AOV_TEXT}, [AOV_METRIC], graph, False, _make_console(inputs=["y"])
    )
    latest = await graph.get_latest_definition(AOV_METRIC.id)
    assert written == 1
    assert latest.version == 2
    assert latest.description == AOV_TEXT
    await graph.close()


async def test_seed_definition_never_overwrites_user_definition():
    graph = await _real_graph()
    await graph.upsert_definition(Definition(
        entity_id=AOV_METRIC.id, description="Net revenue per order, as finance defines it.",
        version=1, created_by="user",
    ))
    console = _make_console()
    written = await SemanticScanner(backend=_make_backend({}))._write_seed_definitions(
        {AOV_METRIC.id: AOV_TEXT}, [AOV_METRIC], graph, False, console
    )
    latest = await graph.get_latest_definition(AOV_METRIC.id)
    assert written == 0
    assert latest.created_by == "user"
    assert latest.version == 1
    console.input.assert_not_called()
    printed = " ".join(str(c.args[0]) for c in console.print.call_args_list)
    assert "Kept 1 metric definition(s) written outside scan" in printed
    assert "average_order_value (by user)" in printed
    await graph.close()


async def test_seed_definitions_interactive_confirms_each_and_keeps_edits():
    other = Entity(id="metric.demo_shop.orders", name="orders", type=EntityType.METRIC,
                   source_connector="dbt", project="demo_shop")
    third = Entity(id="metric.demo_shop.revenue", name="revenue", type=EntityType.METRIC,
                   source_connector="dbt", project="demo_shop")
    graph = await _real_graph()
    for e in (other, third):
        await graph.upsert_entity(e)
    seeds = {AOV_METRIC.id: AOV_TEXT, other.id: "Count of orders.", third.id: "Revenue."}
    # y = accept AOV, e = edit orders, blank = skip revenue
    console = _make_console(inputs=["y", "e", "Orders net of cancellations.", ""])

    written = await SemanticScanner(backend=_make_backend({}))._write_seed_definitions(
        seeds, [AOV_METRIC, other, third], graph, True, console
    )

    assert written == 2
    aov = await graph.get_latest_definition(AOV_METRIC.id)
    assert (aov.description, aov.created_by) == (AOV_TEXT, "scan")
    orders = await graph.get_latest_definition(other.id)
    assert (orders.description, orders.created_by) == ("Orders net of cancellations.", "user")
    assert await graph.get_latest_definition(third.id) is None
    await graph.close()
