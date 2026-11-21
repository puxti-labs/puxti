"""Cross-connector reference resolution — index building and edge rewriting."""

from puxti.core.resolution import build_reference_index, resolve_edges
from puxti.models import Edge, EdgeType, Entity, EntityType


def _prisma_table(model: str, db_table: str) -> Entity:
    return Entity(
        id=f"table.prisma.{model}",
        name=model,
        type=EntityType.TABLE,
        source_connector="prisma",
        metadata={"db_table": db_table},
    )


def _view(schema: str, name: str) -> Entity:
    return Entity(
        id=f"view.{schema}.{name}",
        name=name,
        type=EntityType.VIEW,
        source_connector="sql_views",
        metadata={"schema": schema},
    )


def _dbt_model(project: str, name: str, schema: str = "") -> Entity:
    return Entity(
        id=f"model.{project}.{name}",
        name=name,
        type=EntityType.MODEL,
        source_connector="dbt",
        metadata={"schema": schema},
    )


def _sqlref_edge(from_id: str, raw: str) -> Edge:
    return Edge(
        from_entity_id=from_id,
        to_entity_id=f"sqlref.{raw}",
        type=EdgeType.DEPENDS_ON,
        connector="sql_views",
        metadata={"raw_reference": raw},
    )


# ── build_reference_index ──────────────────────────────────────────────────────

def test_index_prisma_tables_by_db_table_name():
    index = build_reference_index([_prisma_table("User", "users")])
    assert index["users"] == "table.prisma.User"
    assert "user" not in index


def test_index_dbt_models_by_bare_and_qualified_name():
    index = build_reference_index([_dbt_model("shop", "orders", schema="analytics")])
    assert index["orders"] == "model.shop.orders"
    assert index["analytics.orders"] == "model.shop.orders"


def test_index_views_by_bare_and_qualified_name():
    index = build_reference_index([_view("reporting", "daily_kpis")])
    assert index["daily_kpis"] == "view.reporting.daily_kpis"
    assert index["reporting.daily_kpis"] == "view.reporting.daily_kpis"


def test_index_drops_ambiguous_names():
    index = build_reference_index([
        _prisma_table("Order", "orders"),
        _dbt_model("shop", "orders", schema="analytics"),
    ])
    assert "orders" not in index                      # two claimants → dropped
    assert index["analytics.orders"] == "model.shop.orders"


def test_index_ignores_columns_and_other_connectors():
    column = Entity(
        id="table.prisma.User.email",
        name="email",
        type=EntityType.COLUMN,
        source_connector="prisma",
    )
    task = Entity(
        id="task.airflow.sync.load",
        name="load",
        type=EntityType.TASK,
        source_connector="airflow",
    )
    assert build_reference_index([column, task]) == {}


# ── resolve_edges ──────────────────────────────────────────────────────────────

def test_resolve_rewrites_matching_placeholder():
    index = build_reference_index([_prisma_table("User", "users")])
    edges = [_sqlref_edge("view.public.user_stats", "users")]
    resolved, unresolved = resolve_edges(edges, index)
    assert resolved[0].to_entity_id == "table.prisma.User"
    assert resolved[0].metadata["resolved_from"] == "users"
    assert unresolved == []


def test_resolve_qualified_reference_falls_back_to_bare_name():
    index = build_reference_index([_prisma_table("User", "users")])
    edges = [_sqlref_edge("view.public.user_stats", "public.users")]
    resolved, unresolved = resolve_edges(edges, index)
    assert resolved[0].to_entity_id == "table.prisma.User"
    assert unresolved == []


def test_resolve_keeps_unmatched_placeholders():
    edges = [_sqlref_edge("view.public.user_stats", "stripe_charges")]
    resolved, unresolved = resolve_edges(edges, {})
    assert resolved[0].to_entity_id == "sqlref.stripe_charges"
    assert unresolved == ["stripe_charges"]


def test_resolve_leaves_regular_edges_alone():
    edge = Edge(
        from_entity_id="view.public.top_users",
        to_entity_id="view.public.user_stats",
        type=EdgeType.DEPENDS_ON,
        connector="sql_views",
    )
    resolved, unresolved = resolve_edges([edge], {"user_stats": "something.else"})
    assert resolved[0].to_entity_id == "view.public.user_stats"
    assert unresolved == []


def test_resolve_reports_unresolved_sorted_and_unique():
    edges = [
        _sqlref_edge("view.public.a", "zeta"),
        _sqlref_edge("view.public.b", "alpha"),
        _sqlref_edge("view.public.c", "zeta"),
    ]
    _, unresolved = resolve_edges(edges, {})
    assert unresolved == ["alpha", "zeta"]


def _dbt_column(model_id: str, name: str) -> Entity:
    return Entity(
        id=f"{model_id}.{name}",
        name=name,
        type=EntityType.COLUMN,
        source_connector="dbt",
        metadata={"model_id": model_id},
    )


def test_index_columns_under_each_key_of_their_table():
    model = _dbt_model("shop", "orders", schema="analytics")
    index = build_reference_index([model, _dbt_column(model.id, "Amount")])
    assert index["orders#amount"] == "model.shop.orders.Amount"
    assert index["analytics.orders#amount"] == "model.shop.orders.Amount"


def test_index_dbt_metrics_by_name():
    metric = Entity(
        id="metric.shop.arr", name="arr", type=EntityType.METRIC, source_connector="dbt"
    )
    assert build_reference_index([metric]) == {"metric:arr": "metric.shop.arr"}


def test_index_drops_columns_of_an_ambiguous_table():
    a, b = _dbt_model("a", "orders"), _dbt_model("b", "orders")
    index = build_reference_index([a, b, _dbt_column(a.id, "x"), _dbt_column(b.id, "x")])
    assert "orders" not in index and "orders#x" not in index


def test_resolve_column_and_metric_references():
    model = _dbt_model("shop", "orders", schema="analytics")
    index = build_reference_index([model, _dbt_column(model.id, "amount")])
    edges, unresolved = resolve_edges(
        [
            _sqlref_edge("chart.bi.c", "analytics.orders#amount"),
            _sqlref_edge("chart.bi.c", "orders#missing"),
        ],
        index,
    )
    assert edges[0].to_entity_id == "model.shop.orders.amount"
    assert unresolved == ["orders#missing"]


def test_columns_of_an_ambiguous_table_key_stay_unresolved():
    """A dbt model and a view are both `orders`; only the model declares `amount`.
    `orders#amount` must not resolve to the model, since `orders` itself does not."""
    model = _dbt_model("shop", "orders", schema="analytics")
    view = _view("public", "orders")
    index = build_reference_index([model, view, _dbt_column(model.id, "amount")])
    assert "orders" not in index and "orders#amount" not in index
    assert index["analytics.orders#amount"] == "model.shop.orders.amount"


def test_prisma_columns_go_by_their_database_name():
    table = _prisma_table("User", "users")
    field = Entity(
        id="table.prisma.User.createdAt",
        name="createdAt",
        type=EntityType.COLUMN,
        source_connector="prisma",
        metadata={"db_column": "created_at"},
    )
    index = build_reference_index([table, field])
    assert index["users#created_at"] == "table.prisma.User.createdAt"
    assert "users#createdat" not in index
