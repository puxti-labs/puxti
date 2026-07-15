"""Connector for Prisma schema definitions (schema.prisma).

Parses the Prisma DSL directly — no Node toolchain required — the same way
the Airflow connector parses DAG files without running Airflow. Models become
TABLE entities, scalar fields become COLUMN entities, and relations become
REFERENCES edges.

Rename diffs patch schema.prisma only. Applying them still requires a Prisma
migration (`prisma migrate dev`) and client regeneration (`prisma generate`)
— every diff description says so, because the file edit alone does not change
the database.
"""

import re
from dataclasses import dataclass, field
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

_BLOCK_RE = re.compile(
    r"((?:^[ \t]*///[^\n]*\n)*)^(model|enum)\s+(\w+)\s*\{(.*?)^\}",
    re.DOTALL | re.MULTILINE,
)
_FIELD_RE = re.compile(r"^(\w+)\s+(\w+)(\[\])?(\?)?\s*(.*)$")
_MAP_RE = re.compile(r'@map\(\s*"([^"]+)"\s*\)')
_BLOCK_MAP_RE = re.compile(r'@@map\(\s*"([^"]+)"\s*\)')
_RELATION_REFS_RE = re.compile(r"references:\s*\[([^\]]*)\]")


@dataclass
class _PrismaField:
    name: str
    type: str
    is_list: bool
    is_optional: bool
    attrs: str
    db_column: str          # @map(...) or the field name itself
    is_relation: bool       # type is another model (enum-typed fields are columns)
    description: str = ""


@dataclass
class _PrismaModel:
    name: str
    body: str               # text between the braces
    span: tuple[int, int]   # (start, end) offsets of the whole block in the schema
    db_table: str           # @@map(...) or the model name itself
    description: str = ""
    fields: list[_PrismaField] = field(default_factory=list)


class PrismaConnector(BaseConnector):
    """Connector for Prisma ORM schema files.

    Config keys:
        project_dir  - repo root the schema lives in (diff paths are relative to it)
        schema_path  - path to schema.prisma, absolute or relative to project_dir
                       defaults to {project_dir}/prisma/schema.prisma
    """

    name = "prisma"

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.project_dir = Path(config["project_dir"])
        schema = Path(config.get("schema_path", "prisma/schema.prisma"))
        self.schema_path = schema if schema.is_absolute() else self.project_dir / schema
        self._models: dict[str, _PrismaModel] | None = None

    # ── Internal ──────────────────────────────────────────────────────────────

    def _load_schema_text(self) -> str:
        if not self.schema_path.exists():
            raise FileNotFoundError(
                f"Prisma schema not found at {self.schema_path}. "
                "Set connectors.prisma.schema_path in .puxti.yml if it lives elsewhere."
            )
        return self.schema_path.read_text()

    def _load_models(self) -> dict[str, _PrismaModel]:
        if self._models is None:
            self._models = _parse_schema(self._load_schema_text())
        return self._models

    def _entity_id(self, model: str, field_name: str | None = None) -> str:
        base = f"table.prisma.{model}"
        return f"{base}.{field_name}" if field_name else base

    def _schema_rel_path(self) -> str:
        try:
            return str(self.schema_path.relative_to(self.project_dir))
        except ValueError:
            return str(self.schema_path)

    # ── BaseConnector ─────────────────────────────────────────────────────────

    async def health_check(self) -> bool:
        return self.schema_path.exists()

    async def extract_entities(self) -> list[Entity]:
        """Prisma models as TABLE entities, scalar fields as COLUMN entities."""
        entities: list[Entity] = []
        for model in self._load_models().values():
            model_id = self._entity_id(model.name)
            entities.append(Entity(
                id=model_id,
                name=model.name,
                type=EntityType.TABLE,
                source_connector=self.name,
                project="prisma",
                metadata={
                    "path": self._schema_rel_path(),
                    "db_table": model.db_table,
                    "description": model.description,
                },
            ))
            for f in model.fields:
                if f.is_relation:
                    continue  # relation fields are virtual — no DB column
                entities.append(Entity(
                    id=self._entity_id(model.name, f.name),
                    name=f.name,
                    type=EntityType.COLUMN,
                    source_connector=self.name,
                    project="prisma",
                    metadata={
                        "model_id": model_id,
                        "model_name": model.name,
                        "data_type": f.type,
                        "db_column": f.db_column,
                        "description": f.description,
                    },
                ))
        return entities

    async def extract_lineage(self) -> list[Edge]:
        """Foreign-key relations as REFERENCES edges (FK side → target model).

        Implicit many-to-many relations (list fields on both sides) produce no
        edges — there is no FK-holding side to anchor the direction on.
        """
        models = self._load_models()
        edges: dict[tuple[str, str], Edge] = {}
        for model in models.values():
            for f in model.fields:
                if not f.is_relation or f.type not in models or f.is_list:
                    continue
                key = (model.name, f.type)
                edges[key] = Edge(
                    from_entity_id=self._entity_id(model.name),
                    to_entity_id=self._entity_id(f.type),
                    type=EdgeType.REFERENCES,
                    connector=self.name,
                    metadata={"relation_field": f.name},
                )
        return list(edges.values())

    def supports_change_type(self, change_type: str) -> bool:
        return change_type == ChangeType.STRUCTURAL.value

    def get_model_sql_map(self) -> dict[str, str]:
        """Model-block source text keyed by entity ID.

        Not SQL, but the interface contract is "raw source text for entities
        this connector can patch" — the LLM reads Prisma DSL fine.
        """
        schema_text = self._load_schema_text()
        return {
            self._entity_id(m.name): schema_text[m.span[0]:m.span[1]]
            for m in self._load_models().values()
        }

    def find_model_path(self, entity_id: str) -> str | None:
        parts = entity_id.split(".")
        if len(parts) < 3 or parts[0] != "table" or parts[1] != "prisma":
            return None
        try:
            models = self._load_models()
        except FileNotFoundError:
            return None
        return self._schema_rel_path() if parts[2] in models else None

    async def generate_changes(
        self, event: SemanticChangeEvent
    ) -> tuple[list[FileDiff], list[str]]:
        """Patch schema.prisma for a column rename originating in Prisma.

        Only acts when the changed entity is a Prisma field. Two cases:
        - the field has @map("old"): only the @map string changes — application
          code keeps the field name, the migration renames the column
        - otherwise: the field identifier is renamed in its model block
          (definition, @@index/@@unique/@@id lists, own relation fields lists)
          and in `references: [...]` of relations pointing at this model
        """
        if event.change_type != ChangeType.STRUCTURAL:
            return [], []
        parts = event.entity_id.split(".")
        if len(parts) < 3 or parts[0] != "table" or parts[1] != "prisma":
            return [], []

        model_name = parts[2]
        old_name: str = event.change.get("before", {}).get("name", "")
        new_name: str = event.change.get("after", {}).get("name", "")
        if not old_name or not new_name:
            return [], []

        models = self._load_models()
        model = models.get(model_name)
        if model is None:
            return [], [event.entity_id]

        target = next(
            (f for f in model.fields
             if not f.is_relation and old_name in (f.name, f.db_column)),
            None,
        )
        if target is None:
            return [], [event.entity_id]

        schema_text = self._load_schema_text()
        migration_note = (
            "Requires a Prisma migration (`prisma migrate dev`) and client "
            "regeneration (`prisma generate`) — the schema edit alone does not "
            "rename the database column."
        )

        if _MAP_RE.search(target.attrs) and target.db_column == old_name:
            # Mapped field: the DB column lives in the @map string only.
            block = schema_text[model.span[0]:model.span[1]]
            new_block = block.replace(f'@map("{old_name}")', f'@map("{new_name}")')
            updated = (
                schema_text[:model.span[0]] + new_block + schema_text[model.span[1]:]
            )
            description = (
                f"Renamed mapped column `{old_name}` → `{new_name}` on field "
                f"`{model.name}.{target.name}` (@map updated). {migration_note}"
            )
        else:
            updated = _rename_field_in_schema(schema_text, models, model_name, old_name, new_name)
            description = (
                f"Renamed field `{model.name}.{old_name}` → `{new_name}`. "
                f"{migration_note}"
            )

        if updated == schema_text:
            return [], [event.entity_id]

        return [FileDiff(
            file_path=self._schema_rel_path(),
            before=schema_text,
            after=updated,
            connector=self.name,
            description=description,
        )], []


# ── Schema parsing ────────────────────────────────────────────────────────────

def _parse_schema(text: str) -> dict[str, _PrismaModel]:
    """Parse model blocks (and enum names, to classify field types) from a schema."""
    enums: set[str] = set()
    models: dict[str, _PrismaModel] = {}

    for match in _BLOCK_RE.finditer(text):
        doc_block, kind, name, body = match.groups()
        if kind == "enum":
            enums.add(name)
            continue
        block_map = _BLOCK_MAP_RE.search(body)
        models[name] = _PrismaModel(
            name=name,
            body=body,
            span=match.span(),
            db_table=block_map.group(1) if block_map else name,
            description=_strip_doc(doc_block),
        )

    model_names = set(models)
    for model in models.values():
        model.fields = _parse_fields(model.body, model_names)

    return models


def _parse_fields(body: str, model_names: set[str]) -> list[_PrismaField]:
    fields: list[_PrismaField] = []
    pending_doc: list[str] = []
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if line.startswith("///"):
            pending_doc.append(line.lstrip("/").strip())
            continue
        if not line or line.startswith("//") or line.startswith("@@"):
            pending_doc = []
            continue
        m = _FIELD_RE.match(line)
        if not m:
            pending_doc = []
            continue
        fname, ftype, list_marker, optional_marker, attrs = m.groups()
        field_map = _MAP_RE.search(attrs)
        fields.append(_PrismaField(
            name=fname,
            type=ftype,
            is_list=list_marker is not None,
            is_optional=optional_marker is not None,
            attrs=attrs,
            db_column=field_map.group(1) if field_map else fname,
            # Enum-typed fields are real DB columns; only model-typed
            # fields are virtual relation fields.
            is_relation=ftype in model_names,
            description=" ".join(pending_doc),
        ))
        pending_doc = []
    return fields


def _strip_doc(doc_block: str) -> str:
    lines = [ln.strip().lstrip("/").strip() for ln in doc_block.splitlines()]
    return " ".join(ln for ln in lines if ln)


def _ident_re(name: str) -> re.Pattern:
    # Word-boundary identifier match that skips quoted occurrences, so
    # @map("email") survives a rename of the `email` field.
    return re.compile(rf'(?<!["\w]){re.escape(name)}(?!["\w])')


def _rename_field_in_schema(
    schema_text: str,
    models: dict[str, _PrismaModel],
    model_name: str,
    old_name: str,
    new_name: str,
) -> str:
    """Rename a field identifier in its own model block and in `references:`
    lists of relation fields (in other models) that point at this model."""
    ident = _ident_re(old_name)
    model = models[model_name]

    # Replace bottom-up so earlier spans stay valid.
    replacements: list[tuple[int, int, str]] = []

    block_text = schema_text[model.span[0]:model.span[1]]
    replacements.append((model.span[0], model.span[1], ident.sub(new_name, block_text)))

    for other in models.values():
        if other.name == model_name:
            continue
        relation_types = {f.type for f in other.fields if f.is_relation}
        if model_name not in relation_types:
            continue
        block = schema_text[other.span[0]:other.span[1]]
        new_block = _RELATION_REFS_RE.sub(
            lambda m: m.group(0).replace(m.group(1), ident.sub(new_name, m.group(1))),
            block,
        )
        if new_block != block:
            replacements.append((other.span[0], other.span[1], new_block))

    result = schema_text
    for start, end, new_text in sorted(replacements, reverse=True):
        result = result[:start] + new_text + result[end:]
    return result
