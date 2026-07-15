"""Shared CLI helpers — entity ID parsing."""


def test_parse_entity_id_task():
    from puxti.cli._shared import _parse_entity_id
    from puxti.models import EntityType
    entity_type, connector, project = _parse_entity_id("task.airflow.salesforce_sync.extract_opportunities")
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


def test_parse_entity_id_bare_table_prefix_raises():
    """table.* without the prisma namespace is not claimed by any connector."""
    from puxti.cli._shared import _parse_entity_id
    import pytest
    with pytest.raises(ValueError, match="Unrecognized entity ID"):
        _parse_entity_id("table.warehouse.users")


def test_parse_entity_id_unknown_raises():
    from puxti.cli._shared import _parse_entity_id
    import pytest
    with pytest.raises(ValueError, match="Unrecognized entity ID"):
        _parse_entity_id("unknown.prefix.thing")
