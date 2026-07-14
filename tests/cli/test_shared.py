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


def test_parse_entity_id_unknown_raises():
    from puxti.cli._shared import _parse_entity_id
    import pytest
    with pytest.raises(ValueError, match="Unrecognized entity ID"):
        _parse_entity_id("unknown.prefix.thing")
