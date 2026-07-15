import ast
import re
from datetime import date
from pathlib import Path

from puxti.connectors.base import BaseConnector
from puxti.models import Edge, EdgeType, Entity, EntityType, FileDiff, SemanticChangeEvent


class AirflowConnector(BaseConnector):
    """Connector for Apache Airflow DAG projects.

    Reads Python DAG files to extract task entities and dependencies.
    Generates docstring annotation diffs for semantically affected tasks.

    Config keys:
        dags_dir  - path to the directory containing DAG Python files
    """

    name = "airflow"

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.dags_dir = Path(config["dags_dir"])

    async def health_check(self) -> bool:
        return self.dags_dir.exists() and self.dags_dir.is_dir()

    async def extract_entities(self) -> list[Entity]:
        """Parse DAG files and return Task entities."""
        entities: list[Entity] = []
        for dag_file in sorted(self.dags_dir.glob("*.py")):
            try:
                source = dag_file.read_text()
                tree = ast.parse(source)
            except (SyntaxError, OSError):
                continue

            dag_id = _extract_dag_id(tree, dag_file.stem)
            if not dag_id:
                continue

            for task_id in _extract_task_ids(tree):
                entities.append(Entity(
                    id=f"task.airflow.{dag_id}.{task_id}",
                    name=task_id,
                    type=EntityType.TASK,
                    source_connector="airflow",
                    project=dag_id,
                ))
        return entities

    async def extract_lineage(self) -> list[Edge]:
        """Extract task dependencies from >> operator patterns in DAG files."""
        edges: list[Edge] = []
        for dag_file in sorted(self.dags_dir.glob("*.py")):
            try:
                source = dag_file.read_text()
                tree = ast.parse(source)
            except (SyntaxError, OSError):
                continue

            dag_id = _extract_dag_id(tree, dag_file.stem)
            if not dag_id:
                continue

            for from_task, to_task in _extract_task_dependencies(source):
                edges.append(Edge(
                    from_entity_id=f"task.airflow.{dag_id}.{from_task}",
                    to_entity_id=f"task.airflow.{dag_id}.{to_task}",
                    type=EdgeType.DEPENDS_ON,
                    connector="airflow",
                ))
        return edges

    async def generate_changes(self, event: SemanticChangeEvent) -> tuple[list[FileDiff], list[str]]:
        """Generate docstring annotation diffs for Airflow tasks in the affected set."""
        affected_task_ids = [
            eid for eid in (event.affected_entity_ids or [])
            if eid.startswith("task.airflow.")
        ]
        if not affected_task_ids:
            return [], []

        before_val = event.change.get("before", {}).get("name", "")
        after_val = event.change.get("after", {}).get("name", "")
        context = event.semantic_context or ""
        capture_date = date.today().isoformat()

        diffs: list[FileDiff] = []
        unverified: list[str] = []

        for entity_id in affected_task_ids:
            parts = entity_id.split(".")
            if len(parts) < 4:
                unverified.append(entity_id)
                continue
            dag_id, task_id = parts[2], parts[3]

            dag_file = _find_dag_file(self.dags_dir, dag_id)
            if not dag_file:
                unverified.append(entity_id)
                continue

            original = dag_file.read_text()
            updated = _annotate_task_docstring(
                source=original,
                task_id=task_id,
                before=before_val,
                after=after_val,
                context=context,
                capture_date=capture_date,
            )
            if updated is None or updated == original:
                unverified.append(entity_id)
                continue

            diffs.append(FileDiff(
                file_path=str(dag_file.relative_to(self.dags_dir)),
                before=original,
                after=updated,
                connector="airflow",
                description=(
                    f"Annotated task `{task_id}` in DAG `{dag_id}` with semantic change context"
                ),
            ))

        return diffs, unverified


# ── DAG file parsing ──────────────────────────────────────────────────────────

def _extract_dag_id(tree: ast.AST, fallback: str) -> str:
    """Extract dag_id from @dag(dag_id="...") or dag(...) call. Falls back to filename stem."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == "dag_id" and isinstance(kw.value, ast.Constant):
                    return str(kw.value.value)
    return fallback


def _extract_task_ids(tree: ast.AST) -> list[str]:
    """Extract task_id values from @task-decorated functions."""
    task_ids: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for decorator in node.decorator_list:
            if _is_task_decorator(decorator):
                task_ids.append(_get_task_id_from_decorator(decorator) or node.name)
                break
    return task_ids


def _is_task_decorator(decorator: ast.expr) -> bool:
    if isinstance(decorator, ast.Name) and decorator.id == "task":
        return True
    if isinstance(decorator, ast.Attribute) and decorator.attr == "task":
        return True
    if isinstance(decorator, ast.Call):
        func = decorator.func
        if isinstance(func, ast.Name) and func.id == "task":
            return True
        if isinstance(func, ast.Attribute) and func.attr == "task":
            return True
    return False


def _get_task_id_from_decorator(decorator: ast.expr) -> str | None:
    if not isinstance(decorator, ast.Call):
        return None
    for kw in decorator.keywords:
        if kw.arg == "task_id" and isinstance(kw.value, ast.Constant):
            return str(kw.value.value)
    return None


def _extract_task_dependencies(source: str) -> list[tuple[str, str]]:
    """Extract task dependencies from >> operator patterns (best-effort).

    Handles both variable syntax (t1 >> t2) and call syntax (task1() >> task2()).
    """
    return [
        (m.group(1), m.group(2))
        for m in re.finditer(r'(\w+)(?:\([^)]*\))?\s*>>\s*(\w+)(?:\([^)]*\))?', source)
    ]


def _find_dag_file(dags_dir: Path, dag_id: str) -> Path | None:
    """Find the DAG file that defines dag_id."""
    pattern = re.compile(r'dag_id\s*=\s*["\']' + re.escape(dag_id) + r'["\']')
    for py_file in sorted(dags_dir.glob("*.py")):
        try:
            if pattern.search(py_file.read_text()):
                return py_file
        except OSError:
            continue
    return None


# ── Docstring annotation ──────────────────────────────────────────────────────

def _annotate_task_docstring(
    source: str,
    task_id: str,
    before: str,
    after: str,
    context: str,
    capture_date: str,
) -> str | None:
    """Return source with a PUXTI annotation appended to the task's docstring.

    Uses ast to locate the closing triple-quote, then injects the annotation
    block before it so the existing docstring text is preserved.
    Returns None if the task or its docstring cannot be located.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None

    lines = source.splitlines()

    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue

        matched = node.name == task_id
        if not matched:
            for dec in node.decorator_list:
                if _get_task_id_from_decorator(dec) == task_id:
                    matched = True
                    break
        if not matched:
            continue

        # Must have a string literal as the first statement (docstring)
        if not (
            node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        ):
            continue

        doc_node = node.body[0].value
        end_lineno = doc_node.end_lineno      # 1-based
        end_col = doc_node.end_col_offset     # 0-based, points past the last quote char

        line = lines[end_lineno - 1]
        closing_start = end_col - 3
        if closing_start < 0 or line[closing_start:closing_start + 3] not in ('"""', "'''"):
            continue

        quote = line[closing_start:closing_start + 3]
        indent = " " * closing_start

        annotation = (
            f"\n{indent}[PUXTI {capture_date}] Semantic change captured\n"
            f"{indent}Before:  {before}\n"
            f"{indent}After:   {after}\n"
            f"{indent}Context: {context}\n"
        )

        new_line = line[:closing_start] + annotation + quote + line[closing_start + 3:]
        lines[end_lineno - 1] = new_line
        return "\n".join(lines)

    return None
