"""PrismaConnector — schema parsing, entities, lineage, and rename diffs."""

import pytest

from puxti.connectors.prisma import PrismaConnector
from puxti.models import ChangeType, EdgeType, EntityType, SemanticChangeEvent

SCHEMA = '''\
generator client {
  provider = "prisma-client-js"
}

datasource db {
  provider = "postgresql"
  url      = env("DATABASE_URL")
}

enum Role {
  USER
  ADMIN
}

/// A registered account holder.
model User {
  id        Int      @id @default(autoincrement())
  /// Primary contact address.
  email     String   @unique
  role      Role     @default(USER)
  createdAt DateTime @default(now()) @map("created_at")
  posts     Post[]

  @@map("users")
}

model Post {
  id       Int    @id @default(autoincrement())
  title    String
  author   User   @relation(fields: [authorId], references: [id])
  authorId Int    @map("author_id")

  @@index([authorId])
}
'''


@pytest.fixture
def connector(tmp_path):
    schema_file = tmp_path / "prisma" / "schema.prisma"
    schema_file.parent.mkdir()
    schema_file.write_text(SCHEMA)
    return PrismaConnector(config={"project_dir": str(tmp_path)})


def _rename_event(entity_id: str, before: str, after: str) -> SemanticChangeEvent:
    return SemanticChangeEvent(
        change_event_id="evt-1",
        entity_id=entity_id,
        change_type=ChangeType.STRUCTURAL,
        semantic_context="rename",
        affected_entity_ids=[],
        reasoning="",
        change={"before": {"name": before}, "after": {"name": after}},
    )


# ── health ─────────────────────────────────────────────────────────────────────

async def test_health_check_true_when_schema_exists(connector):
    assert await connector.health_check() is True


async def test_health_check_false_when_schema_missing(tmp_path):
    connector = PrismaConnector(config={"project_dir": str(tmp_path)})
    assert await connector.health_check() is False


async def test_schema_path_override(tmp_path):
    (tmp_path / "db.prisma").write_text(SCHEMA)
    connector = PrismaConnector(
        config={"project_dir": str(tmp_path), "schema_path": "db.prisma"}
    )
    assert await connector.health_check() is True
    entities = await connector.extract_entities()
    assert any(e.id == "table.prisma.User" for e in entities)


# ── entities ───────────────────────────────────────────────────────────────────

async def test_extract_entities_models_become_tables(connector):
    entities = await connector.extract_entities()
    tables = {e.id: e for e in entities if e.type == EntityType.TABLE}
    assert set(tables) == {"table.prisma.User", "table.prisma.Post"}
    assert tables["table.prisma.User"].source_connector == "prisma"
    assert tables["table.prisma.User"].metadata["db_table"] == "users"     # @@map
    assert tables["table.prisma.Post"].metadata["db_table"] == "Post"      # unmapped
    assert tables["table.prisma.User"].metadata["path"] == "prisma/schema.prisma"


async def test_extract_entities_doc_comment_becomes_description(connector):
    entities = await connector.extract_entities()
    user = next(e for e in entities if e.id == "table.prisma.User")
    assert user.metadata["description"] == "A registered account holder."
    email = next(e for e in entities if e.id == "table.prisma.User.email")
    assert email.metadata["description"] == "Primary contact address."


async def test_extract_entities_scalar_and_enum_fields_become_columns(connector):
    entities = await connector.extract_entities()
    columns = {e.id for e in entities if e.type == EntityType.COLUMN}
    assert "table.prisma.User.email" in columns
    assert "table.prisma.User.role" in columns          # enum-typed → column
    assert "table.prisma.User.createdAt" in columns
    assert "table.prisma.Post.authorId" in columns


async def test_extract_entities_relation_fields_are_not_columns(connector):
    entities = await connector.extract_entities()
    ids = {e.id for e in entities}
    assert "table.prisma.User.posts" not in ids
    assert "table.prisma.Post.author" not in ids


async def test_extract_entities_map_recorded_as_db_column(connector):
    entities = await connector.extract_entities()
    created = next(e for e in entities if e.id == "table.prisma.User.createdAt")
    assert created.metadata["db_column"] == "created_at"
    email = next(e for e in entities if e.id == "table.prisma.User.email")
    assert email.metadata["db_column"] == "email"


# ── lineage ────────────────────────────────────────────────────────────────────

async def test_extract_lineage_fk_side_references_target(connector):
    edges = await connector.extract_lineage()
    assert len(edges) == 1
    edge = edges[0]
    assert edge.from_entity_id == "table.prisma.Post"
    assert edge.to_entity_id == "table.prisma.User"
    assert edge.type == EdgeType.REFERENCES
    assert edge.connector == "prisma"


# ── capabilities ───────────────────────────────────────────────────────────────

def test_get_model_sql_map_returns_model_blocks(connector):
    sql_map = connector.get_model_sql_map()
    assert set(sql_map) == {"table.prisma.User", "table.prisma.Post"}
    assert "email     String   @unique" in sql_map["table.prisma.User"]
    assert "@@map" in sql_map["table.prisma.User"]


def test_find_model_path(connector):
    assert connector.find_model_path("table.prisma.User") == "prisma/schema.prisma"
    assert connector.find_model_path("table.prisma.User.email") == "prisma/schema.prisma"
    assert connector.find_model_path("table.prisma.Nope") is None
    assert connector.find_model_path("model.shop.orders") is None


def test_supports_only_structural_changes(connector):
    assert connector.supports_change_type("structural") is True
    assert connector.supports_change_type("semantic") is False


# ── generate_changes ───────────────────────────────────────────────────────────

async def test_rename_unmapped_field_patches_definition(connector):
    diffs, unverified = await connector.generate_changes(
        _rename_event("table.prisma.Post.title", "title", "heading")
    )
    assert unverified == []
    assert len(diffs) == 1
    assert diffs[0].connector == "prisma"
    assert diffs[0].file_path == "prisma/schema.prisma"
    assert "heading" in diffs[0].after
    assert "title" not in diffs[0].after
    assert "prisma migrate" in diffs[0].description


async def test_rename_field_updates_relation_references(connector):
    diffs, unverified = await connector.generate_changes(
        _rename_event("table.prisma.User.id", "id", "uid")
    )
    assert unverified == []
    assert len(diffs) == 1
    after = diffs[0].after
    # Post's relation points at User.id — its references list must follow.
    assert "references: [uid]" in after
    # Post's own `id` field is untouched.
    assert "id       Int    @id @default(autoincrement())" in after


async def test_rename_mapped_field_only_updates_map_string(connector):
    diffs, unverified = await connector.generate_changes(
        _rename_event("table.prisma.User.createdAt", "created_at", "created_ts")
    )
    assert unverified == []
    assert len(diffs) == 1
    after = diffs[0].after
    assert '@map("created_ts")' in after
    assert "createdAt" in after          # field identifier stays
    assert '@map("created_at")' not in after


async def test_rename_does_not_touch_other_map_strings(connector):
    diffs, _ = await connector.generate_changes(
        _rename_event("table.prisma.Post.authorId", "authorId", "writerId")
    )
    assert len(diffs) == 1
    after = diffs[0].after
    assert "writerId" in after
    # The @map string holds the DB name and was not keyed on the field name.
    assert '@map("author_id")' in after
    # The own-model relation fields list follows the rename.
    assert "fields: [writerId]" in after


async def test_rename_unknown_field_flags_unverified(connector):
    diffs, unverified = await connector.generate_changes(
        _rename_event("table.prisma.User.nope", "nope", "yep")
    )
    assert diffs == []
    assert unverified == ["table.prisma.User.nope"]


async def test_rename_unknown_model_flags_unverified(connector):
    diffs, unverified = await connector.generate_changes(
        _rename_event("table.prisma.Ghost.name", "name", "label")
    )
    assert diffs == []
    assert unverified == ["table.prisma.Ghost.name"]


async def test_non_prisma_entity_produces_no_changes(connector):
    diffs, unverified = await connector.generate_changes(
        _rename_event("model.shop.orders.order_date", "order_date", "recorded_date")
    )
    assert diffs == []
    assert unverified == []


async def test_non_structural_event_produces_no_changes(connector):
    event = SemanticChangeEvent(
        change_event_id="evt-2",
        entity_id="table.prisma.User.email",
        change_type=ChangeType.SEMANTIC,
        semantic_context="meaning changed",
        affected_entity_ids=[],
        reasoning="",
        change={"description": "new meaning"},
    )
    diffs, unverified = await connector.generate_changes(event)
    assert diffs == []
    assert unverified == []
