import json
import logging
import re
from pathlib import Path

from puxti.connectors.base import BaseConnector
from puxti.connectors.sql_utils import (
    mentions_in_jinja,
    rename_column_fully,
    rename_column_in_sql,
)
from puxti.models import (
    ChangeType,
    Edge,
    EdgeType,
    Entity,
    EntityType,
    FileDiff,
    SemanticChangeEvent,
    UnverifiedEntity,
    UnverifiedReason,
)

_logger = logging.getLogger(__name__)


class DbtConnector(BaseConnector):
    """Connector for dbt Core and dbt Cloud projects.

    Reads the dbt manifest.json to extract models, columns, sources, MetricFlow
    metrics, and lineage.
    Generates file diffs for structural changes (column renames, model updates).

    Config keys:
        project_dir   - path to the dbt project root (contains dbt_project.yml)
        manifest_path - optional override for manifest.json location
                        defaults to {project_dir}/target/manifest.json
    """

    name = "dbt"

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
        """Extract dbt models, their columns, sources, and metrics as entities."""
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

        # MetricFlow metrics. dbt writes them to manifest.json next to
        # semantic_manifest.json, keyed metric.<project>.<name> — the same ID
        # `puxti define` gives a proposed metric, so the two reconcile on upsert.
        for metric_id, metric in manifest.get("metrics", {}).items():
            entities.append(
                Entity(
                    id=metric_id,
                    name=metric["name"],
                    type=EntityType.METRIC,
                    source_connector="dbt",
                    project=project_name,
                    metadata={
                        "description": metric.get("description", ""),
                        "label": metric.get("label") or "",
                        "metric_type": metric.get("type", ""),
                    },
                )
            )

        return entities

    async def extract_lineage(self) -> list[Edge]:
        """Extract model-to-model, model-to-source, and metric dependencies.

        A simple or cumulative metric depends on a semantic model, which is not
        an entity; its edge goes to the model behind that semantic model. Derived
        and ratio metrics depend on their input metrics.
        """
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

        semantic_models = manifest.get("semantic_models", {})
        for metric_id, metric in manifest.get("metrics", {}).items():
            targets: list[str] = []
            for dep_id in metric.get("depends_on", {}).get("nodes", []):
                if not dep_id.startswith("semantic_model."):
                    targets.append(dep_id)
                    continue
                models = semantic_models.get(dep_id, {}).get("depends_on", {}).get("nodes", [])
                if not models:
                    _logger.warning(
                        "Metric %s depends on %s, which resolves to no model; "
                        "no lineage edge written for it.", metric_id, dep_id,
                    )
                targets.extend(models)
            for target_id in dict.fromkeys(targets):
                edges.append(
                    Edge(
                        from_entity_id=metric_id,
                        to_entity_id=target_id,
                        type=EdgeType.DEPENDS_ON,
                        connector="dbt",
                    )
                )

        return edges

    def get_seed_definitions(self) -> dict[str, str]:
        """Return {metric_id: definition} for every MetricFlow metric, built from
        its YAML description and type params — no LLM, since the metric spec
        already states what it computes."""
        manifest = self._load_manifest()
        measures: dict[str, tuple[str, dict]] = {}
        for semantic_model in manifest.get("semantic_models", {}).values():
            for measure in semantic_model.get("measures", []):
                measures[measure["name"]] = (semantic_model.get("name", ""), measure)
        return {
            metric_id: _metric_definition(metric, measures)
            for metric_id, metric in manifest.get("metrics", {}).items()
        }

    def get_project_name(self) -> str:
        """Return the dbt project name from the manifest metadata."""
        return self._load_manifest().get("metadata", {}).get("project_name", "")

    def find_model_path(self, entity_id: str) -> str | None:
        """Repo-relative path of a model's SQL file, or None if unknown."""
        try:
            manifest = self._load_manifest()
        except FileNotFoundError:
            return None
        node = manifest.get("nodes", {}).get(entity_id)
        if not node:
            return None
        model_path = self._model_file_path(node)
        if not model_path.exists():
            return None
        return str(model_path.relative_to(self.project_dir))

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

    async def generate_changes(
        self, event: SemanticChangeEvent
    ) -> tuple[list[FileDiff], list[UnverifiedEntity]]:
        """Generate file diffs for a semantic change event.

        Returns (diffs, unverified_entities). unverified_entities contains
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
    ) -> tuple[list[FileDiff], list[UnverifiedEntity]]:
        """Case 1 — column rename.

        Only touches models that DIRECTLY reference the source model via
        ref(). This is the safe single-hop approach: without column-level
        lineage we cannot know whether a transitive dependent's bare
        reference to old_name traces back to the renamed column or to an
        unrelated column with the same name in a different upstream model.

        Returns (diffs, unverified_entities). unverified_entities lists
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
        unverified: list[UnverifiedEntity] = []

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
                unverified.append(
                    UnverifiedEntity(
                        entity_id=node_id,
                        reason=UnverifiedReason.NOT_DIRECT_DEPENDENT,
                    )
                )
                continue

            # A reference inside a Jinja span (macro argument, {% set %}, comment)
            # cannot be renamed at the text level. Renaming only the plain SQL
            # would leave the model half-renamed, so skip it for manual review.
            if mentions_in_jinja(original_sql, old_name):
                _logger.warning(
                    "Skipping rename of `%s` in model `%s`: column is referenced "
                    "inside a Jinja expression; flagged as unverified",
                    old_name,
                    node["name"],
                )
                unverified.append(
                    UnverifiedEntity(
                        entity_id=node_id,
                        reason=UnverifiedReason.JINJA_REFERENCE,
                    )
                )
                continue

            try:
                updated_sql = rename_column_fully(original_sql, old_name, new_name)
            except Exception as exc:
                raise RuntimeError(
                    f"Failed to apply rename `{old_name}` → `{new_name}` "
                    f"in model `{node['name']}` ({model_path})"
                ) from exc

            # Qualified references (alias.col) are never rewritten, so renaming
            # the bare ones alone would leave broken SQL. Never write a partial
            # rename: flag the whole model for manual review.
            if updated_sql is None:
                _logger.warning(
                    "Skipping rename of `%s` in model `%s`: column has qualified "
                    "references that cannot be renamed safely; flagged as unverified",
                    old_name,
                    node["name"],
                )
                unverified.append(
                    UnverifiedEntity(
                        entity_id=node_id,
                        reason=UnverifiedReason.QUALIFIED_REFERENCE,
                    )
                )
                continue

            if updated_sql == original_sql:
                # No replacement was made. Flag the source model anyway:
                # its rename did not land anywhere.
                if is_source:
                    unverified.append(
                        UnverifiedEntity(
                            entity_id=node_id,
                            reason=UnverifiedReason.NO_CHANGE,
                        )
                    )
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

# Shared with other SQL-patching connectors; kept under the old private name
# because existing callers and tests import it from here.
_rename_column_in_sql = rename_column_in_sql


def _window_text(window: object) -> str:
    """'7 days' from either a {count, granularity} dict or a plain string."""
    if isinstance(window, dict):
        return f"{window.get('count', '')} {window.get('granularity', '')}".strip()
    return str(window)


def _where_text(filter_: dict | None) -> str:
    """The where clauses of a MetricFlow filter, joined with AND, or ''."""
    clauses = [
        f["where_sql_template"].strip()
        for f in (filter_ or {}).get("where_filters", [])
        if f.get("where_sql_template")
    ]
    return " AND ".join(clauses)


def _measure_text(params: dict, measures: dict[str, tuple[str, dict]]) -> str | None:
    """What a simple or cumulative metric aggregates, or None when the manifest
    does not say. Newer specs put agg/expr on the metric instead of a measure."""
    ref = params.get("measure") or {}
    name = ref.get("name", "")
    if name:
        if name in measures:
            semantic_model, measure = measures[name]
            text = (
                f"{measure.get('agg') or 'aggregate'} of {measure.get('expr') or name} "
                f"(measure {name} in semantic model {semantic_model})"
            )
        else:
            text = f"measure {name}"
        where = _where_text(ref.get("filter"))
        return f"{text}, where {where}" if where else text
    if params.get("agg"):
        expr = params.get("expr")
        return f"{params['agg']} of {expr}" if expr else str(params["agg"])
    return None


def _input_text(ref: dict | None) -> str:
    ref = ref or {}
    text = ref.get("name", "")
    where = _where_text(ref.get("filter"))
    if where:
        text += f" (where {where})"
    if ref.get("offset_window"):
        text += f" offset by {_window_text(ref['offset_window'])}"
    if ref.get("offset_to_grain"):
        text += f" offset to start of {ref['offset_to_grain']}"
    if ref.get("alias"):
        text += f" (as {ref['alias']})"
    return text


def _metric_definition(metric: dict, measures: dict[str, tuple[str, dict]]) -> str:
    """Plain-text definition of a MetricFlow metric: its description, then what
    it computes. The description comes first so it is the definition's first
    line wherever only one line is shown."""
    params = metric.get("type_params") or {}
    metric_type = metric.get("type", "")

    aggregated = _measure_text(params, measures)
    if metric_type == "simple":
        formula = f"Simple metric: {aggregated}" if aggregated else "Simple metric."
    elif metric_type == "cumulative":
        cumulative = params.get("cumulative_type_params") or {}
        window = cumulative.get("window") or params.get("window")
        grain = cumulative.get("grain_to_date") or params.get("grain_to_date")
        if window:
            span = f"over a {_window_text(window)} window"
        elif grain:
            span = f"{grain}-to-date"
        else:
            span = "over all time"
        formula = (
            f"Cumulative metric: {aggregated}, {span}" if aggregated
            else f"Cumulative metric, {span}."
        )
    elif metric_type == "ratio":
        formula = (
            f"Ratio metric: {_input_text(params.get('numerator'))} / "
            f"{_input_text(params.get('denominator'))}"
        )
    elif metric_type == "derived":
        formula = f"Derived metric: {params.get('expr') or ''}".rstrip()
        inputs = ", ".join(_input_text(i) for i in params.get("metrics") or [])
        if inputs:
            formula += f"\nInputs: {inputs}"
    else:
        formula = f"{metric_type.capitalize() or 'Unknown'} metric."

    where = _where_text(metric.get("filter"))
    if where:
        formula += f"\nFilter: {where}"

    description = (metric.get("description") or "").strip()
    return f"{description}\n\n{formula}" if description else formula


def _references_model(sql: str, model_name: str) -> bool:
    """Return True if sql contains a dbt ref() call for model_name."""
    return bool(re.search(
        r"ref\s*\(\s*['\"]" + re.escape(model_name) + r"['\"]",
        sql,
        re.IGNORECASE,
    ))
