import textwrap
from pathlib import Path

import pytest

from puxti.connectors.airflow import (
    AirflowConnector,
    _annotate_task_docstring,
    _extract_dag_id,
    _extract_task_ids,
    _find_dag_file,
)
from puxti.models import EdgeType, EntityType, SemanticChangeEvent


# ── Fixtures ──────────────────────────────────────────────────────────────────

SIMPLE_DAG = textwrap.dedent('''\
    from airflow.decorators import dag, task
    from datetime import datetime

    @dag(dag_id="my_dag", schedule="@daily", start_date=datetime(2024, 1, 1), catchup=False)
    def my_dag():

        @task(task_id="extract_orders")
        def extract_orders():
            """Extract orders from the source system.

            Loads raw order data into the warehouse.
            """
            pass

        @task(task_id="load_orders")
        def load_orders():
            """Load orders into the data warehouse."""
            pass

        extract_orders() >> load_orders()

    my_dag()
''')

TASK_BY_FUNCTION_NAME_DAG = textwrap.dedent('''\
    from airflow.decorators import dag, task
    from datetime import datetime

    @dag(dag_id="simple_dag", schedule="@daily", start_date=datetime(2024, 1, 1), catchup=False)
    def simple_dag():

        @task
        def process_data():
            """Process the raw data.

            Transforms and loads into target tables.
            """
            pass

    simple_dag()
''')

NO_DOCSTRING_DAG = textwrap.dedent('''\
    from airflow.decorators import dag, task
    from datetime import datetime

    @dag(dag_id="nodoc_dag", schedule="@daily", start_date=datetime(2024, 1, 1), catchup=False)
    def nodoc_dag():

        @task(task_id="no_doc_task")
        def no_doc_task():
            pass

    nodoc_dag()
''')


@pytest.fixture()
def dags_dir(tmp_path: Path) -> Path:
    (tmp_path / "my_dag.py").write_text(SIMPLE_DAG)
    (tmp_path / "simple_dag.py").write_text(TASK_BY_FUNCTION_NAME_DAG)
    (tmp_path / "nodoc_dag.py").write_text(NO_DOCSTRING_DAG)
    return tmp_path


# ── health_check ──────────────────────────────────────────────────────────────

def test_health_check_existing_dir(dags_dir: Path) -> None:
    connector = AirflowConnector(config={"dags_dir": str(dags_dir)})
    import asyncio
    assert asyncio.run(connector.health_check()) is True


def test_health_check_missing_dir(tmp_path: Path) -> None:
    connector = AirflowConnector(config={"dags_dir": str(tmp_path / "nonexistent")})
    import asyncio
    assert asyncio.run(connector.health_check()) is False


# ── extract_entities ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_extract_entities_returns_task_entities(dags_dir: Path) -> None:
    connector = AirflowConnector(config={"dags_dir": str(dags_dir)})
    entities = await connector.extract_entities()
    ids = {e.id for e in entities}

    assert "task.airflow.my_dag.extract_orders" in ids
    assert "task.airflow.my_dag.load_orders" in ids
    assert "task.airflow.simple_dag.process_data" in ids


@pytest.mark.asyncio
async def test_extract_entities_type_and_connector(dags_dir: Path) -> None:
    connector = AirflowConnector(config={"dags_dir": str(dags_dir)})
    entities = await connector.extract_entities()
    by_id = {e.id: e for e in entities}

    e = by_id["task.airflow.my_dag.extract_orders"]
    assert e.type == EntityType.TASK
    assert e.source_connector == "airflow"
    assert e.project == "my_dag"


@pytest.mark.asyncio
async def test_extract_entities_skips_no_decorator(tmp_path: Path) -> None:
    (tmp_path / "plain.py").write_text(textwrap.dedent('''\
        from airflow.decorators import dag
        from datetime import datetime

        @dag(dag_id="plain_dag", schedule="@daily", start_date=datetime(2024, 1, 1), catchup=False)
        def plain_dag():
            def not_a_task():
                pass
        plain_dag()
    '''))
    connector = AirflowConnector(config={"dags_dir": str(tmp_path)})
    entities = await connector.extract_entities()
    assert entities == []


# ── extract_lineage ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_extract_lineage_returns_edges(dags_dir: Path) -> None:
    connector = AirflowConnector(config={"dags_dir": str(dags_dir)})
    edges = await connector.extract_lineage()
    pairs = {(e.from_entity_id, e.to_entity_id) for e in edges}

    assert ("task.airflow.my_dag.extract_orders", "task.airflow.my_dag.load_orders") in pairs


@pytest.mark.asyncio
async def test_extract_lineage_edge_type(dags_dir: Path) -> None:
    connector = AirflowConnector(config={"dags_dir": str(dags_dir)})
    edges = await connector.extract_lineage()
    for e in edges:
        assert e.type == EdgeType.DEPENDS_ON
        assert e.connector == "airflow"


# ── generate_changes ──────────────────────────────────────────────────────────

def _make_event(affected: list[str]) -> SemanticChangeEvent:
    return SemanticChangeEvent(
        change_event_id="evt-1",
        entity_id="source.clariva.raw_opportunities",
        change_type="semantic",
        semantic_context="amount is now a Salesforce roll-up of order line totals.",
        affected_entity_ids=affected,
        reasoning="The task feeds this source.",
        change={"before": {"name": "amount"}, "after": {"name": "amount"}},
    )


@pytest.mark.asyncio
async def test_generate_changes_annotates_docstring(dags_dir: Path) -> None:
    connector = AirflowConnector(config={"dags_dir": str(dags_dir)})
    event = _make_event(["task.airflow.my_dag.extract_orders"])
    diffs, unverified = await connector.generate_changes(event)

    assert len(diffs) == 1
    assert unverified == []
    diff = diffs[0]
    assert diff.connector == "airflow"
    assert diff.file_path == "my_dag.py"
    assert "[PUXTI" in diff.after
    assert "amount is now a Salesforce roll-up" in diff.after


@pytest.mark.asyncio
async def test_generate_changes_no_airflow_entities_returns_empty(dags_dir: Path) -> None:
    connector = AirflowConnector(config={"dags_dir": str(dags_dir)})
    event = _make_event(["model.clariva.stg_opportunities"])
    diffs, unverified = await connector.generate_changes(event)
    assert diffs == []
    assert unverified == []


@pytest.mark.asyncio
async def test_generate_changes_no_docstring_goes_to_unverified(dags_dir: Path) -> None:
    connector = AirflowConnector(config={"dags_dir": str(dags_dir)})
    event = _make_event(["task.airflow.nodoc_dag.no_doc_task"])
    diffs, unverified = await connector.generate_changes(event)
    assert diffs == []
    assert "task.airflow.nodoc_dag.no_doc_task" in unverified


@pytest.mark.asyncio
async def test_generate_changes_missing_dag_goes_to_unverified(dags_dir: Path) -> None:
    connector = AirflowConnector(config={"dags_dir": str(dags_dir)})
    event = _make_event(["task.airflow.ghost_dag.some_task"])
    diffs, unverified = await connector.generate_changes(event)
    assert diffs == []
    assert "task.airflow.ghost_dag.some_task" in unverified


# ── _annotate_task_docstring ──────────────────────────────────────────────────

def test_annotate_by_task_id_kwarg() -> None:
    result = _annotate_task_docstring(
        source=SIMPLE_DAG,
        task_id="extract_orders",
        before="manual value",
        after="computed roll-up",
        context="Q1 2024 migration",
        capture_date="2026-04-21",
    )
    assert result is not None
    assert "[PUXTI 2026-04-21]" in result
    assert "Before:  manual value" in result
    assert "After:   computed roll-up" in result
    assert "Context: Q1 2024 migration" in result
    assert "Loads raw order data into the warehouse." in result


def test_annotate_by_function_name() -> None:
    result = _annotate_task_docstring(
        source=TASK_BY_FUNCTION_NAME_DAG,
        task_id="process_data",
        before="old",
        after="new",
        context="some context",
        capture_date="2026-04-21",
    )
    assert result is not None
    assert "[PUXTI 2026-04-21]" in result


def test_annotate_no_docstring_returns_none() -> None:
    result = _annotate_task_docstring(
        source=NO_DOCSTRING_DAG,
        task_id="no_doc_task",
        before="old",
        after="new",
        context="ctx",
        capture_date="2026-04-21",
    )
    assert result is None


def test_annotate_unknown_task_returns_none() -> None:
    result = _annotate_task_docstring(
        source=SIMPLE_DAG,
        task_id="nonexistent_task",
        before="old",
        after="new",
        context="ctx",
        capture_date="2026-04-21",
    )
    assert result is None


def test_annotate_preserves_original_docstring() -> None:
    result = _annotate_task_docstring(
        source=SIMPLE_DAG,
        task_id="extract_orders",
        before="old",
        after="new",
        context="ctx",
        capture_date="2026-04-21",
    )
    assert result is not None
    assert "Loads raw order data into the warehouse." in result


# ── helpers ───────────────────────────────────────────────────────────────────

def test_extract_dag_id_from_decorator() -> None:
    import ast
    tree = ast.parse(SIMPLE_DAG)
    assert _extract_dag_id(tree, "fallback") == "my_dag"


def test_extract_dag_id_fallback() -> None:
    import ast
    tree = ast.parse("x = 1")
    assert _extract_dag_id(tree, "my_fallback") == "my_fallback"


def test_extract_task_ids() -> None:
    import ast
    tree = ast.parse(SIMPLE_DAG)
    ids = _extract_task_ids(tree)
    assert "extract_orders" in ids
    assert "load_orders" in ids


def test_find_dag_file(dags_dir: Path) -> None:
    result = _find_dag_file(dags_dir, "my_dag")
    assert result is not None
    assert result.name == "my_dag.py"


def test_find_dag_file_missing(dags_dir: Path) -> None:
    assert _find_dag_file(dags_dir, "nonexistent") is None


# ── BaseConnector optional capabilities — defaults for non-producer methods ───

def test_optional_capabilities_have_safe_defaults(tmp_path: Path) -> None:
    """Connectors that don't own patchable SQL sources inherit no-op defaults
    from BaseConnector — engines treat them as 'nothing to patch', not errors."""
    connector = AirflowConnector(config={"dags_dir": str(tmp_path)})
    assert connector.get_model_sql_map() == {}
    assert connector.find_model_path("task.airflow.some_dag.some_task") is None
    assert connector.get_project_name() == ""
