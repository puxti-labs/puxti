"""Graph domain models."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class RelType(StrEnum):
    """Allowed relationship types — validated allowlist, never interpolated raw."""
    DEPENDS_ON = "DEPENDS_ON"       # structural lineage: column → column
    PART_OF = "PART_OF"             # entity → schema / schema → project
    DERIVED_FROM = "DERIVED_FROM"   # semantic lineage across a change
    REPLACES = "REPLACES"           # column rename: new → old


@dataclass
class Entity:
    name: str
    type: str           # "table" | "column" | "dataset" | "schema"
    description: str = ""
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass
class Relationship:
    from_name: str
    to_name: str
    rel_type: RelType
