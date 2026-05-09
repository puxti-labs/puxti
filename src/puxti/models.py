from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class EntityType(str, Enum):
    TABLE = "table"
    COLUMN = "column"
    MODEL = "model"          # dbt model
    DAG = "dag"              # Airflow DAG
    TASK = "task"            # Airflow task
    DASHBOARD = "dashboard"
    METRIC = "metric"


class ChangeType(str, Enum):
    STRUCTURAL = "structural"    # Case 1 — column rename, table restructure
    SEMANTIC = "semantic"        # Case 3 — definition redefinition
    SURFACE = "surface"          # Case 2 — attribute surfacing to reporting layer
    UNKNOWN = "unknown"


class ChangeStatus(str, Enum):
    DETECTED = "detected"
    CAPTURED = "captured"
    PROPAGATED = "propagated"
    CLOSED = "closed"


class EdgeType(str, Enum):
    DEPENDS_ON = "depends_on"
    TRANSFORMS = "transforms"
    REFERENCES = "references"
    DERIVED_FROM = "derived_from"   # semantic graph — concept derived from another
    FEEDS = "feeds"                 # cross-system: upstream producer → downstream entity


# ── Structural lineage entities ──────────────────────────────────────────────

class Entity(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    type: EntityType
    source_connector: str
    project: str = ""                        # project name — e.g. dbt project, Airflow instance
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Edge(BaseModel):
    from_entity_id: str
    to_entity_id: str
    type: EdgeType
    connector: str
    metadata: dict[str, Any] = Field(default_factory=dict)


# ── Semantic graph ────────────────────────────────────────────────────────────
# Sits above structural lineage. Captures what entities mean and how concepts
# relate across the full stack, regardless of which connector owns them.

class Definition(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    entity_id: str
    description: str                        # semantic text — what this entity means
    version: int = 1
    created_by: str                         # "user" | "llm" | "import"
    change_event_id: str | None = None      # the change that triggered this definition
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class SemanticEdge(BaseModel):
    """Concept-level relationship in the semantic graph.
    Example: revenue DERIVED_FROM sales — so redefining sales affects revenue."""
    from_entity_id: str
    to_entity_id: str
    type: EdgeType
    description: str                        # why this semantic relationship exists
    created_by: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# ── Change events ─────────────────────────────────────────────────────────────

class ChangeEvent(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    type: ChangeType
    source_entity_id: str
    change: dict[str, Any]                  # before/after representation
    semantic_context: str | None = None     # filled by semantic capture
    declared_by: str | None = None          # user who declared it (proactive mode)
    status: ChangeStatus = ChangeStatus.DETECTED
    detected_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class SemanticChangeEvent(BaseModel):
    """A ChangeEvent enriched with semantic context — ready for propagation."""
    change_event_id: str
    entity_id: str
    change_type: ChangeType
    semantic_context: str
    affected_entity_ids: list[str]          # entities the semantic graph says are affected
    reasoning: str                          # LLM chain of reasoning for the propagation
    change: dict[str, Any]


# ── Propagation ───────────────────────────────────────────────────────────────

class FileDiff(BaseModel):
    file_path: str
    before: str
    after: str
    connector: str
    description: str                        # human-readable explanation of this change


class PropagationResult(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    change_event_id: str
    connector: str
    target_entity_id: str
    diffs: list[FileDiff]
    unverified_entity_ids: list[str] = Field(default_factory=list)
    """Entity IDs that were flagged as potentially affected but could not be
    safely propagated (e.g. transitive dependents where the column may come
    from a different upstream source). These should be reviewed manually."""
    pr_url: str | None = None
    status: str = "pending"                 # pending | opened | merged | rejected
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# ── Correction ────────────────────────────────────────────────────────────────

class EdgeAssessment(BaseModel):
    """LLM-proposed disposition for a single semantic edge during correction."""
    from_entity_id: str
    to_entity_id: str
    action: str                             # "keep" | "update" | "remove"
    updated_description: str | None = None  # populated when action == "update"
    reasoning: str


class CorrectionEvent(BaseModel):
    """Records a user correction to an entity definition and its edge impacts."""
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    entity_id: str
    old_definition_id: str
    new_definition_id: str
    edges_kept: list[tuple[str, str]] = Field(default_factory=list)     # (from_id, to_id)
    edges_updated: list[tuple[str, str]] = Field(default_factory=list)  # (from_id, to_id)
    edges_removed: list[tuple[str, str]] = Field(default_factory=list)  # (from_id, to_id)
    classified_as: str                      # "correction" | "real_change"
    change_event_id: str | None = None      # set when classified_as == "real_change"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
