import json
import re
from pathlib import Path

from puxti.connectors.base import BaseConnector
from puxti.models import (
    ChangeType,
    Edge,
    EdgeType,
    Entity,
    EntityType,
    FileDiff,
    SemanticChangeEvent,
)


class DbtConnector(BaseConnector):
    """Connector for dbt Core and dbt Cloud projects.

    Reads the dbt manifest.json to extract models, columns, and lineage.
    Generates file diffs for structural changes (column renames, model updates).

    Config keys:
        project_dir   - path to the dbt project root (contains dbt_project.yml)
        manifest_path - optional override for manifest.json location
                        defaults to {project_dir}/target/manifest.json
    """

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.project_dir = Path(config["project_dir"])
        self.manifest_path = Path(
            config.get("manifest_path", self.project_dir / "target" / "manifest.json")
        )
        self._manifest: dict | None = None

    # ── Internal ──────────────────────────────────────────────────────────────

    def _load_manifest(self) -> dict:
        if self._manifest is None:
            if not self.manifest_path.exists():
                raise FileNotFoundError(
                    f"dbt manifest not found at {self.manifest_path}. "
                    "Run `dbt compile` or `dbt run` to generate it."
                )
            with open(self.manifest_path) as f:
                self._manifest = json.load(f)
        return self._manifest

    def _model_file_path(self, node: dict) -> Path:
        """Absolute path to a model's SQL file."""
        return self.project_dir / node["original_file_path"]

    # ── BaseConnector ─────────────────────────────────────────────────────────

    async def health_check(self) -> bool:
        try:
            self._load_manifest()
            return True
        except FileNotFoundError:
            return False

    async def extract_entities(self) -> list[Entity]:
        """Extract dbt models and their columns as entities."""
        manifest = self._load_manifest()
        project_name = manifest.get("metadata", {}).get("project_name", "")
        entities: list[Entity] = []

        for node_id, node in manifest.get("nodes", {}).items():
            if node.get("resource_type") != "model":
                continue

            # Model entity
            model_entity = Entity(
                id=node_id,
                name=node["name"],
                type=EntityType.MODEL,
                source_connector="dbt",
                project=project_name,
                metadata={
                    "path": node.get("original_file_path", ""),
                    "schema": node.get("schema", ""),
                    "database": node.get("database", ""),
                    "description": node.get("description", ""),
                    "tags": node.get("tags", []),
                },
            )
            entities.append(model_entity)

            # Column entities for this model
            for col_name, col_meta in node.get("columns", {}).items():
                col_entity = Entity(
                    id=f"{node_id}.{col_name}",
                    name=col_name,
                    type=EntityType.COLUMN,
                    source_connector="dbt",
                    project=project_name,
                    metadata={
                        "model_id": node_id,
                        "model_name": node["name"],
                        "description": col_meta.get("description", ""),
                        "data_type": col_meta.get("data_type", ""),
                    },
                )
                entities.append(col_entity)

        # Sources as table entities
        for source_id, source in manifest.get("sources", {}).items():
            source_entity = Entity(
                id=source_id,
                name=source["name"],
                type=EntityType.TABLE,
                source_connector="dbt",
                project=project_name,
                metadata={
                    "source_name": source.get("source_name", ""),
                    "schema": source.get("schema", ""),
                    "database": source.get("database", ""),
                    "description": source.get("description", ""),
                },
            )
            entities.append(source_entity)

        return entities

    async def extract_lineage(self) -> list[Edge]:
        """Extract model-to-model and model-to-source dependencies."""
        manifest = self._load_manifest()
        edges: list[Edge] = []

        for node_id, node in manifest.get("nodes", {}).items():
            if node.get("resource_type") != "model":
                continue

            for dep_id in node.get("depends_on", {}).get("nodes", []):
                edges.append(
                    Edge(
                        from_entity_id=node_id,
                        to_entity_id=dep_id,
                        type=EdgeType.DEPENDS_ON,
                        connector="dbt",
                    )
                )

        return edges

    def get_project_name(self) -> str:
        """Return the dbt project name from the manifest metadata."""
        return self._load_manifest().get("metadata", {}).get("project_name", "")

    def get_model_sql_map(self) -> dict[str, str]:
        """Return {node_id: sql_content} for all model nodes.

        Used by the scanner and redefiner to give the LLM full SQL context
        when generating definitions and reasoning about semantic changes.
        """
        manifest = self._load_manifest()
        sql_map: dict[str, str] = {}
        for node_id, node in manifest.get("nodes", {}).items():
            if node.get("resource_type") != "model":
                continue
            model_path = self._model_file_path(node)
            if model_path.exists():
                sql_map[node_id] = model_path.read_text()
        return sql_map

    async def generate_changes(self, event: SemanticChangeEvent) -> tuple[list[FileDiff], list[str]]:
        """Generate file diffs for a semantic change event.

        Returns (diffs, unverified_entity_ids). unverified_entity_ids contains
        entities flagged as potentially affected but skipped because they could
        not be safely propagated (e.g. no direct ref() to the source model).

        Supported change types:
        - STRUCTURAL (column rename): finds all model SQL files referencing
          the old column name and replaces with the new name.
        """
        if event.change_type == ChangeType.STRUCTURAL:
            return await self._generate_column_rename_diffs(event)

        # SEMANTIC and SURFACE changes: no unverified entities.
        diffs = await self._generate_semantic_diffs(event)
        return diffs, []

    # ── Change generators ─────────────────────────────────────────────────────

    async def _generate_column_rename_diffs(
        self, event: SemanticChangeEvent
    ) -> tuple[list[FileDiff], list[str]]:
        """Case 1 — column rename.

        Only touches models that DIRECTLY reference the source model via
        ref(). This is the safe single-hop approach: without column-level
        lineage we cannot know whether a transitive dependent's bare
        reference to old_name traces back to the renamed column or to an
        unrelated column with the same name in a different upstream model.

        Returns (diffs, unverified_entity_ids). unverified_entity_ids lists
        entities that were in affected_entity_ids but skipped — these may
        still need manual review, especially in non-standard layered models.

        Deeper hops are handled by re-running capture after the first PR
        is merged, at which point the intermediate model already outputs
        the new column name.
        """
        old_name: str = event.change.get("before", {}).get("name", "")
        new_name: str = event.change.get("after", {}).get("name", "")

        if not old_name or not new_name:
            return [], []

        # Derive source model name from entity_id.
        # e.g. "model.jaffle_shop.orders.order_date" → "orders"
        # e.g. "sports_sims.prep.nba_results_log.type" → "nba_results_log"
        entity_parts = event.entity_id.rsplit(".", 1)
        model_part = entity_parts[0] if len(entity_parts) == 2 else event.entity_id
        source_model_name = model_part.rsplit(".", 1)[-1]

        manifest = self._load_manifest()
        affected_ids = set(event.affected_entity_ids or [])
        diffs: list[FileDiff] = []
        unverified: list[str] = []

        for node_id, node in manifest.get("nodes", {}).items():
            if node.get("resource_type") != "model":
                continue

            # Skip models not in the affected set (when capture identified them).
            if affected_ids and node_id not in affected_ids:
                continue

            model_path = self._model_file_path(node)
            if not model_path.exists():
                continue

            original_sql = model_path.read_text()

            # Only process the source model itself or models that directly
            # reference it via ref(). Track everything else as unverified —
            # the column may come from a different upstream source (e.g. mixed
            # raw/prep layering, shared column names across unrelated models).
            is_source = node["name"] == source_model_name
            is_direct_dependent = _references_model(original_sql, source_model_name)
            if source_model_name and not (is_source or is_direct_dependent):
                unverified.append(node_id)
                continue

            try:
                updated_sql = _rename_column_in_sql(original_sql, old_name, new_name)
            except Exception as exc:
                raise RuntimeError(
                    f"Failed to apply rename `{old_name}` → `{new_name}` "
                    f"in model `{node['name']}` ({model_path})"
                ) from exc

            if updated_sql == original_sql:
                # Source model has no bare references — column is likely only
                # referenced via qualified refs (e.g. alias.col). The rename
                # cannot be applied automatically without a SQL parser.
                # Surface this as unverified so the PR flags it for manual review.
                if is_source:
                    unverified.append(node_id)
                continue

            diffs.append(
                FileDiff(
                    file_path=str(model_path.relative_to(self.project_dir)),
                    before=original_sql,
                    after=updated_sql,
                    connector="dbt",
                    description=(
                        f"Renamed column `{old_name}` → `{new_name}` "
                        f"in model `{node['name']}`"
                    ),
                )
            )

        return diffs, unverified

    async def _generate_semantic_diffs(
        self, event: SemanticChangeEvent
    ) -> list[FileDiff]:
        """Case 3 — definition redefinition and Case 2 — attribute surfacing.

        For non-deterministic changes, we generate a proposed diff with the
        full reasoning chain embedded as a comment in the PR description.
        The human reviews the reasoning and corrects where the logic broke down.
        """
        # Semantic diffs require LLM reasoning — delegated to the propagation
        # engine which has access to the Anthropic client and the full
        # Knowledge Graph context. Return empty here; the propagation engine
        # calls the LLM and then calls generate_changes again with enriched
        # change data if it needs connector-specific file generation.
        return []


# ── SQL utilities ─────────────────────────────────────────────────────────────

def _rename_column_in_sql(sql: str, old_name: str, new_name: str) -> str:
    """Replace references to old_name with new_name in a SQL string.

    Handles common patterns:
    - bare column references:       old_name → new_name
    - aliased selections:           old_name as alias → new_name as alias
    - quoted identifiers:           `old_name`, "old_name" → `new_name`, "new_name"
    - qualified refs (alias.old_name): left completely unchanged

    Qualified references are deliberately not touched: at the SQL text level
    we cannot know which table alias maps to the source model, so renaming
    them risks both wrong renames (s.type where s is a different table) and
    broken SQL. A source model whose only references are qualified is instead
    reported as unverified so the PR flags it for manual review.

    Uses word-boundary matching to avoid partial replacements
    (e.g. 'recorded_date' should not match 'date').
    """
    # Backtick-quoted (BigQuery style) — direct rename
    sql = re.sub(rf"`{re.escape(old_name)}`", f"`{new_name}`", sql)

    # Double-quoted (standard SQL) — direct rename
    sql = re.sub(rf'"{re.escape(old_name)}"', f'"{new_name}"', sql)

    # Qualified references (alias.col, table.col) — leave completely unchanged.
    # We cannot determine from SQL text alone which table alias maps to the
    # source model being renamed, so touching qualified refs risks both wrong
    # renames (s.type where s is a different table) and broken SQL (AS alias
    # in WHERE clauses is invalid). Bare references are unambiguous within a
    # model that is already scoped to the affected entity set.

    # Bare identifier: not preceded by a qualifier (word + dot)
    # word-boundary on both sides, case-insensitive
    sql = re.sub(
        rf"(?<!\w\.)\b{re.escape(old_name)}\b",
        new_name,
        sql,
        flags=re.IGNORECASE,
    )

    return sql


def _references_model(sql: str, model_name: str) -> bool:
    """Return True if sql contains a dbt ref() call for model_name."""
    return bool(re.search(
        rf"ref\s*\(\s*['\"]" + re.escape(model_name) + r"['\"]",
        sql,
        re.IGNORECASE,
    ))
