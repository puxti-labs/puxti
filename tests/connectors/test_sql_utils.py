"""Column renames must preserve CREATE VIEW target names."""

import pytest

from puxti.connectors.sql_utils import rename_column_fully


@pytest.mark.parametrize(
    "target",
    [
        "order_date",
        "ORDER_DATE",
        '"order_date"',
        "`order_date`",
        "[order_date]",
        "order_date.summary",
        '"order_date"."summary"',
        "raw.order_date",
        "raw /* schema */ . order_date",
        "{{ var('schema') }}.order_date",
    ],
)
def test_rename_preserves_create_view_target(target: str) -> None:
    sql = f"create view {target} as select order_date from orders"
    assert rename_column_fully(sql, "order_date", "recorded_date") == (
        f"create view {target} as select recorded_date from orders"
    )


@pytest.mark.parametrize(
    "prefix",
    [
        "CREATE OR REPLACE VIEW",
        "create temporary view",
        "create temp view if not exists",
        "create /* view */ view -- name\n",
    ],
)
def test_rename_preserves_create_view_variants(prefix: str) -> None:
    sql = f"{prefix} order_date as select order_date from orders"
    assert rename_column_fully(sql, "order_date", "recorded_date") == (
        f"{prefix} order_date as select recorded_date from orders"
    )


@pytest.mark.parametrize(
    "sql, expected",
    [
        (
            "select -- create view\norder_date from orders",
            "select -- create view\nrecorded_date from orders",
        ),
        (
            "select 'create view order_date', order_date from orders",
            "select 'create view order_date', recorded_date from orders",
        ),
        (
            "select /* create view */ order_date from orders",
            "select /* create view */ recorded_date from orders",
        ),
        (
            "select extract(day from order_date) from orders",
            "select extract(day from recorded_date) from orders",
        ),
        (
            "create view summary (order_date) as select order_date from orders",
            "create view summary (recorded_date) as select recorded_date from orders",
        ),
    ],
)
def test_rename_preserves_column_handling(sql: str, expected: str) -> None:
    assert rename_column_fully(sql, "order_date", "recorded_date") == expected


def test_view_name_does_not_hide_a_qualified_column() -> None:
    assert (
        rename_column_fully(
            "create view order_date as select order_date, t.order_date from orders t",
            "order_date",
            "recorded_date",
        )
        is None
    )


def test_view_column_is_not_a_create_view_statement() -> None:
    assert rename_column_fully("select view from orders", "view", "view_count") == (
        "select view_count from orders"
    )
