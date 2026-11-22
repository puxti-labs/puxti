"""Shared CLI helpers — entity ID parsing."""


def test_parse_entity_id_task():
    from puxti.cli._shared import _parse_entity_id
    from puxti.models import EntityType
    entity_type, connector, project = _parse_entity_id(
        "task.airflow.salesforce_sync.extract_opportunities"
    )
    assert entity_type == EntityType.TASK
    assert connector == "airflow"
    assert project == "salesforce_sync"


def test_parse_entity_id_source():
    from puxti.cli._shared import _parse_entity_id
    from puxti.models import EntityType
    entity_type, connector, project = _parse_entity_id("source.clariva.raw_opportunities")
    assert entity_type == EntityType.TABLE
    assert connector == "dbt"
    assert project == "clariva"


def test_parse_entity_id_model():
    from puxti.cli._shared import _parse_entity_id
    from puxti.models import EntityType
    entity_type, connector, project = _parse_entity_id("model.clariva.stg_opportunities")
    assert entity_type == EntityType.MODEL
    assert connector == "dbt"
    assert project == "clariva"


def test_parse_entity_id_prisma_table():
    from puxti.cli._shared import _parse_entity_id
    from puxti.models import EntityType
    entity_type, connector, project = _parse_entity_id("table.prisma.User")
    assert entity_type == EntityType.TABLE
    assert connector == "prisma"
    assert project == "prisma"


def test_parse_entity_id_prisma_field():
    from puxti.cli._shared import _parse_entity_id
    from puxti.models import EntityType
    entity_type, connector, project = _parse_entity_id("table.prisma.User.email")
    assert entity_type == EntityType.TABLE
    assert connector == "prisma"


def test_parse_entity_id_view():
    from puxti.cli._shared import _parse_entity_id
    from puxti.models import EntityType
    entity_type, connector, project = _parse_entity_id("view.public.user_stats")
    assert entity_type == EntityType.VIEW
    assert connector == "sql_views"
    assert project == "public"


def test_parse_entity_id_view_column():
    from puxti.cli._shared import _parse_entity_id
    from puxti.models import EntityType
    entity_type, connector, project = _parse_entity_id("view.analytics.daily_signups.day")
    assert entity_type == EntityType.VIEW
    assert connector == "sql_views"
    assert project == "analytics"


def test_parse_entity_id_metric_named_project():
    from puxti.cli._shared import _parse_entity_id
    from puxti.models import EntityType
    entity_type, connector, project = _parse_entity_id("metric.jaffle_shop.nrr")
    assert entity_type == EntityType.METRIC
    assert connector == "proposed"
    assert project == "jaffle_shop"


def test_parse_entity_id_metric_proposed_sentinel_decodes_to_empty_project():
    """`define` stores project="" and encodes it as the `proposed` namespace in the
    ID; parsing must decode it back to "" so `link` gets the same project."""
    from puxti.cli._shared import _parse_entity_id
    from puxti.models import EntityType
    entity_type, connector, project = _parse_entity_id("metric.proposed.nrr")
    assert entity_type == EntityType.METRIC
    assert connector == "proposed"
    assert project == ""


def test_parse_entity_id_bare_table_prefix_raises():
    """table.* without the prisma namespace is not claimed by any connector."""
    import pytest

    from puxti.cli._shared import _parse_entity_id
    with pytest.raises(ValueError, match="Unrecognized entity ID"):
        _parse_entity_id("table.warehouse.users")


def test_parse_entity_id_unknown_raises():
    import pytest

    from puxti.cli._shared import _parse_entity_id
    with pytest.raises(ValueError, match="Unrecognized entity ID"):
        _parse_entity_id("unknown.prefix.thing")


# ── one-time star notice ──────────────────────────────────────────────────────

def _run_noop(tmp_path, *, terminal: bool):
    import io
    from unittest.mock import patch

    from rich.console import Console

    from puxti.cli import _shared

    async def _noop():
        return None

    buf = io.StringIO()
    console = Console(file=buf, force_terminal=terminal)
    with (
        patch("puxti.telemetry._CONFIG_PATH", tmp_path / "config.toml"),
        patch.object(_shared, "err_console", console),
        patch.object(_shared, "_check_for_update", lambda: None),
    ):
        _shared._run(_noop())
    return buf.getvalue()


def test_star_notice_shown_once_on_terminal(tmp_path):

    first = _run_noop(tmp_path, terminal=True)
    second = _run_noop(tmp_path, terminal=True)
    assert "github.com/puxti-labs/puxti" in first
    assert "github.com/puxti-labs/puxti" not in second
    assert "star_shown = true" in (tmp_path / "config.toml").read_text()


def test_star_notice_skipped_when_not_a_terminal(tmp_path):
    output = _run_noop(tmp_path, terminal=False)
    assert "github.com" not in output
    assert not (tmp_path / "config.toml").exists()


def test_star_notice_skipped_on_failure(tmp_path):
    import io
    from unittest.mock import patch

    import pytest
    import typer
    from rich.console import Console

    from puxti.cli import _shared

    async def _fail():
        raise typer.Exit(1)

    buf = io.StringIO()
    with (
        patch("puxti.telemetry._CONFIG_PATH", tmp_path / "config.toml"),
        patch.object(_shared, "err_console", Console(file=buf, force_terminal=True)),
        patch.object(_shared, "_check_for_update", lambda: None),
        pytest.raises(typer.Exit),
    ):
        _shared._run(_fail())
    assert "github.com" not in buf.getvalue()
    assert not (tmp_path / "config.toml").exists()


def test_parse_entity_id_bi_assets():
    import pytest

    from puxti.cli._shared import _parse_entity_id
    from puxti.models import EntityType

    assert _parse_entity_id("dataset.bi.orders") == (EntityType.DATASET, "bi", "bi")
    assert _parse_entity_id("chart.bi.revenue") == (EntityType.CHART, "bi", "bi")
    assert _parse_entity_id("dashboard.bi.sales") == (EntityType.DASHBOARD, "bi", "bi")
    with pytest.raises(ValueError):
        _parse_entity_id("chart.other.revenue")
