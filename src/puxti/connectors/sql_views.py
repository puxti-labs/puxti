"""Connector for plain SQL view definitions.

Reads a directory of .sql files containing CREATE VIEW statements. Views
become VIEW entities, their output columns become COLUMN entities, and
FROM/JOIN references become DEPENDS_ON edges.

References to tables this connector does not own (application tables, dbt
models) are emitted with a `sqlref.<name>` placeholder target carrying the
raw referenced name in edge metadata. The scan flow resolves those against
entities extracted by the other configured connectors — see
puxti.core.resolution. Connectors stay isolated: this module never inspects
another connector's entities.

One view per file is the expected layout. Files with several views are
parsed fine, but a column rename patches the file as a whole, so sibling
views in the same file may be touched by the same rename.
"""

from dataclasses import dataclass, field
from pathlib import Path

import sqlglot
from sqlglot import expressions as exp

from puxti.connectors.base import BaseConnector
from puxti.connectors.sql_utils import rename_column_in_sql
from puxti.models import (
    ChangeType,
    Edge,
    EdgeType,
    Entity,
    EntityType,
    FileDiff,
    SemanticChangeEvent,
)

SQLREF_PREFIX = "sqlref."


@dataclass
class _ParsedView:
    schema: str                 # lowercased; default_schema when unqualified
    name: str                   # lowercased view name
    display_name: str           # as written in the file
    file: Path
    columns: list[str] = field(default_factory=list)
    table_refs: list[tuple[str | None, str]] = field(default_factory=list)
    # (schema | None, table) — lowercased raw references, CTEs excluded

    @property
    def entity_id(self) -> str:
        return f"view.{self.schema}.{self.name}"


class SqlViewsConnector(BaseConnector):
    """Connector for directories of CREATE VIEW .sql files.

    Config keys:
        project_dir    - repo root; diff paths are relative to it
        views_dir      - directory with .sql files, relative to project_dir
                         (default: "." — searched recursively)
        dialect        - sqlglot dialect name, e.g. "postgres", "bigquery"
                         (default: sqlglot's generic dialect)
        default_schema - schema assumed for unqualified view names
                         (default: "public")
    """

    name = "sql_views"

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.project_dir = Path(config["project_dir"])
        self.views_dir = self.project_dir / config.get("views_dir", ".")
        self.dialect = config.get("dialect") or None
        self.default_schema = (config.get("default_schema") or "public").lower()
        self._views: dict[str, _ParsedView] | None = None

    # ── Internal ──────────────────────────────────────────────────────────────

    def _load_views(self) -> dict[str, _ParsedView]:
        if self._views is None:
            views: dict[str, _ParsedView] = {}
            for sql_file in sorted(self.views_dir.rglob("*.sql")):
                for view in self._parse_file(sql_file):
                    views[view.entity_id] = view
            self._views = views
        return self._views

    def _parse_file(self, sql_file: Path) -> list[_ParsedView]:
        try:
            statements = sqlglot.parse(sql_file.read_text(), read=self.dialect)
        except (sqlglot.errors.ParseError, OSError):
            return []

        views: list[_ParsedView] = []
        for stmt in statements:
            if not isinstance(stmt, exp.Create) or (stmt.kind or "").upper() != "VIEW":
                continue

            target = stmt.this
            explicit_columns: list[str] = []
            if isinstance(target, exp.Schema):
                explicit_columns = [c.name for c in target.expressions]
                target = target.this
            if not isinstance(target, exp.Table):
                continue

            query = stmt.expression
            view = _ParsedView(
                schema=(target.db or self.default_schema).lower(),
                name=target.name.lower(),
                display_name=target.name,
                file=sql_file,
                columns=explicit_columns or _output_columns(query),
                table_refs=_table_references(query),
            )
            views.append(view)
        return views

    def _rel_path(self, file: Path) -> str:
        try:
            return str(file.relative_to(self.project_dir))
        except ValueError:
            return str(file)

    # ── BaseConnector ─────────────────────────────────────────────────────────

    async def health_check(self) -> bool:
        return self.views_dir.exists() and self.views_dir.is_dir()

    async def extract_entities(self) -> list[Entity]:
        entities: list[Entity] = []
        for view in self._load_views().values():
            entities.append(Entity(
                id=view.entity_id,
                name=view.name,
                type=EntityType.VIEW,
                source_connector=self.name,
                project=view.schema,
                metadata={
                    "path": self._rel_path(view.file),
                    "schema": view.schema,
                },
            ))
            for col in view.columns:
                entities.append(Entity(
                    id=f"{view.entity_id}.{col}",
                    name=col,
                    type=EntityType.COLUMN,
                    source_connector=self.name,
                    project=view.schema,
                    metadata={"view_id": view.entity_id, "view_name": view.name},
                ))
        return entities

    async def extract_lineage(self) -> list[Edge]:
        """DEPENDS_ON edges for every table/view referenced in a view's query.

        References to this connector's own views resolve directly. Everything
        else gets a `sqlref.` placeholder target for cross-connector
        resolution at scan time.
        """
        views = self._load_views()
        own_by_key = {(v.schema, v.name): v.entity_id for v in views.values()}
        edges: list[Edge] = []
        for view in views.values():
            seen: set[str] = set()
            for ref_schema, ref_name in view.table_refs:
                lookup_schema = ref_schema or self.default_schema
                target = own_by_key.get((lookup_schema, ref_name))
                if target is None:
                    raw = f"{ref_schema}.{ref_name}" if ref_schema else ref_name
                    target = f"{SQLREF_PREFIX}{raw}"
                if target in seen or target == view.entity_id:
                    continue
                seen.add(target)
                edge = Edge(
                    from_entity_id=view.entity_id,
                    to_entity_id=target,
                    type=EdgeType.DEPENDS_ON,
                    connector=self.name,
                )
                if target.startswith(SQLREF_PREFIX):
                    edge.metadata["raw_reference"] = target[len(SQLREF_PREFIX):]
                edges.append(edge)
        return edges

    def supports_change_type(self, change_type: str) -> bool:
        return change_type == ChangeType.STRUCTURAL.value

    def get_model_sql_map(self) -> dict[str, str]:
        return {
            view.entity_id: view.file.read_text()
            for view in self._load_views().values()
            if view.file.exists()
        }

    def find_model_path(self, entity_id: str) -> str | None:
        view = self._load_views().get(entity_id)
        return self._rel_path(view.file) if view else None

    async def generate_changes(
        self, event: SemanticChangeEvent
    ) -> tuple[list[FileDiff], list[str]]:
        """Rename a column in the view files affected by a structural change.

        Mirrors the dbt connector's single-hop safety rule: a view is patched
        when it is the source of the change itself, or when it directly
        references the changed entity's parent by name. Views flagged as
        affected that reference the parent only through an alias the connector
        cannot verify (e.g. a Prisma model whose @@map name differs from the
        model name) are returned as unverified for manual review.
        """
        if event.change_type != ChangeType.STRUCTURAL:
            return [], []

        old_name: str = event.change.get("before", {}).get("name", "")
        new_name: str = event.change.get("after", {}).get("name", "")
        if not old_name or not new_name:
            return [], []

        # Parent of the changed column: "view.public.user_stats.email" →
        # "view.public.user_stats"; "table.prisma.User.email" → parent name "User".
        source_parent_id = event.entity_id.rsplit(".", 1)[0]
        source_parent_name = source_parent_id.rsplit(".", 1)[-1].lower()

        affected_ids = set(event.affected_entity_ids or [])
        views = self._load_views()

        diffs: list[FileDiff] = []
        unverified: list[str] = []
        patched_files: set[Path] = set()

        for view in views.values():
            if affected_ids and view.entity_id not in affected_ids \
                    and view.entity_id != source_parent_id:
                continue

            is_source = view.entity_id == source_parent_id
            is_direct = any(
                ref_name == source_parent_name for _, ref_name in view.table_refs
            )
            if not (is_source or is_direct):
                unverified.append(view.entity_id)
                continue

            if view.file in patched_files:
                continue

            original_sql = view.file.read_text()
            updated_sql = rename_column_in_sql(original_sql, old_name, new_name)
            if updated_sql == original_sql:
                # Only qualified (alias.col) references — cannot rename safely
                # at the text level; flag for manual review.
                unverified.append(view.entity_id)
                continue

            patched_files.add(view.file)
            diffs.append(FileDiff(
                file_path=self._rel_path(view.file),
                before=original_sql,
                after=updated_sql,
                connector=self.name,
                description=(
                    f"Renamed column `{old_name}` → `{new_name}` "
                    f"in view `{view.display_name}`"
                ),
            ))

        return diffs, unverified


# ── sqlglot helpers ───────────────────────────────────────────────────────────

def _output_columns(query: exp.Expression | None) -> list[str]:
    """Output column names of a view query, skipping stars and unnamed items."""
    if query is None:
        return []
    try:
        named = query.named_selects
    except AttributeError:
        return []
    return [n for n in named if n and n != "*"]


def _table_references(query: exp.Expression | None) -> list[tuple[str | None, str]]:
    """(schema, table) pairs referenced by a query, lowercased, CTEs excluded."""
    if query is None:
        return []
    cte_names = {cte.alias_or_name.lower() for cte in query.find_all(exp.CTE)}
    refs: list[tuple[str | None, str]] = []
    for table in query.find_all(exp.Table):
        name = table.name.lower()
        schema = table.db.lower() if table.db else None
        if not name:
            continue
        if schema is None and name in cte_names:
            continue
        if (schema, name) not in refs:
            refs.append((schema, name))
    return refs
