"""SqlViewsConnector — parsing, entities, lineage, and rename diffs."""

import pytest

from puxti.connectors.sql_views import SqlViewsConnector
from puxti.models import ChangeType, EdgeType, EntityType, SemanticChangeEvent

USER_STATS = """\
create view user_stats as
select
    u.id as user_id,
    email,
    count(*) as post_count
from users u
join posts p on p.author_id = u.id
group by u.id, email
"""

DAILY_SIGNUPS = """\
create or replace view analytics.daily_signups as
with recent as (
    select created_at from users
)
select
    date(created_at) as day,
    count(*) as signups
from recent
group by 1
"""

TOP_USERS = """\
create view top_users as
select * from user_stats where post_count > 10
"""


@pytest.fixture
def connector(tmp_path):
    views = tmp_path / "db" / "views"
    views.mkdir(parents=True)
    (views / "user_stats.sql").write_text(USER_STATS)
    (views / "daily_signups.sql").write_text(DAILY_SIGNUPS)
    (views / "top_users.sql").write_text(TOP_USERS)
    return SqlViewsConnector(
        config={"project_dir": str(tmp_path), "views_dir": "db/views"}
    )


def _rename_event(
    entity_id: str, before: str, after: str, affected: list[str] | None = None
) -> SemanticChangeEvent:
    return SemanticChangeEvent(
        change_event_id="evt-1",
        entity_id=entity_id,
        change_type=ChangeType.STRUCTURAL,
        semantic_context="rename",
        affected_entity_ids=affected or [],
        reasoning="",
        change={"before": {"name": before}, "after": {"name": after}},
    )


# ── health ─────────────────────────────────────────────────────────────────────

async def test_health_check_true_when_dir_exists(connector):
    assert await connector.health_check() is True


async def test_health_check_false_when_dir_missing(tmp_path):
    connector = SqlViewsConnector(
        config={"project_dir": str(tmp_path), "views_dir": "nope"}
    )
    assert await connector.health_check() is False


# ── entities ───────────────────────────────────────────────────────────────────

async def test_extract_entities_views_with_default_schema(connector):
    entities = await connector.extract_entities()
    views = {e.id: e for e in entities if e.type == EntityType.VIEW}
    assert set(views) == {
        "view.public.user_stats",
        "view.analytics.daily_signups",   # explicit schema honoured
        "view.public.top_users",
    }
    assert views["view.public.user_stats"].source_connector == "sql_views"
    assert views["view.public.user_stats"].metadata["path"] == "db/views/user_stats.sql"


async def test_extract_entities_columns_from_projections(connector):
    entities = await connector.extract_entities()
    columns = {e.id for e in entities if e.type == EntityType.COLUMN}
    assert "view.public.user_stats.user_id" in columns
    assert "view.public.user_stats.email" in columns
    assert "view.public.user_stats.post_count" in columns
    assert "view.analytics.daily_signups.day" in columns
    assert "view.analytics.daily_signups.signups" in columns


async def test_extract_entities_star_projection_yields_no_columns(connector):
    entities = await connector.extract_entities()
    top_users_cols = [
        e for e in entities
        if e.type == EntityType.COLUMN and e.metadata.get("view_id") == "view.public.top_users"
    ]
    assert top_users_cols == []


async def test_unparseable_file_is_skipped(tmp_path):
    views = tmp_path / "views"
    views.mkdir()
    (views / "broken.sql").write_text("this is (not sql")
    (views / "ok.sql").write_text("create view ok as select 1 as one from t")
    connector = SqlViewsConnector(config={"project_dir": str(tmp_path), "views_dir": "views"})
    entities = await connector.extract_entities()
    assert {e.id for e in entities if e.type == EntityType.VIEW} == {"view.public.ok"}


# ── lineage ────────────────────────────────────────────────────────────────────

async def test_extract_lineage_external_tables_get_sqlref_placeholders(connector):
    edges = await connector.extract_lineage()
    user_stats_targets = {
        e.to_entity_id for e in edges if e.from_entity_id == "view.public.user_stats"
    }
    assert user_stats_targets == {"sqlref.users", "sqlref.posts"}
    raw_refs = {
        e.metadata.get("raw_reference")
        for e in edges if e.from_entity_id == "view.public.user_stats"
    }
    assert raw_refs == {"users", "posts"}
    assert all(e.type == EdgeType.DEPENDS_ON for e in edges)


async def test_extract_lineage_ctes_are_not_references(connector):
    edges = await connector.extract_lineage()
    signup_targets = {
        e.to_entity_id for e in edges if e.from_entity_id == "view.analytics.daily_signups"
    }
    assert signup_targets == {"sqlref.users"}


async def test_extract_lineage_own_views_resolve_directly(connector):
    edges = await connector.extract_lineage()
    top_users_targets = {
        e.to_entity_id for e in edges if e.from_entity_id == "view.public.top_users"
    }
    assert top_users_targets == {"view.public.user_stats"}


# ── capabilities ───────────────────────────────────────────────────────────────

def test_get_model_sql_map(connector):
    sql_map = connector.get_model_sql_map()
    assert sql_map["view.public.user_stats"] == USER_STATS
    assert sql_map["view.analytics.daily_signups"] == DAILY_SIGNUPS


def test_find_model_path(connector):
    assert connector.find_model_path("view.public.user_stats") == "db/views/user_stats.sql"
    assert connector.find_model_path("view.public.nope") is None


def test_supports_only_structural_changes(connector):
    assert connector.supports_change_type("structural") is True
    assert connector.supports_change_type("semantic") is False


# ── generate_changes ───────────────────────────────────────────────────────────

async def test_rename_patches_views_that_directly_reference_source(connector):
    # users.email renamed; user_stats selects `email` directly from users.
    diffs, unverified = await connector.generate_changes(
        _rename_event(
            "model.shop.users.email", "email", "contact_email",
            affected=["view.public.user_stats"],
        )
    )
    assert len(diffs) == 1
    assert diffs[0].file_path == "db/views/user_stats.sql"
    assert "contact_email" in diffs[0].after
    assert diffs[0].connector == "sql_views"
    assert unverified == []


async def test_rename_flags_affected_view_without_direct_reference(connector):
    # Prisma model User @@map("users"): the view references "users" but the
    # entity parent is named "User" — the connector cannot verify the link.
    diffs, unverified = await connector.generate_changes(
        _rename_event(
            "table.prisma.User.email", "email", "contact_email",
            affected=["view.public.user_stats"],
        )
    )
    assert diffs == []
    assert unverified == ["view.public.user_stats"]


async def test_rename_own_view_column_patches_the_view(connector):
    diffs, unverified = await connector.generate_changes(
        _rename_event(
            "view.public.user_stats.post_count", "post_count", "n_posts",
            affected=["view.public.user_stats", "view.public.top_users"],
        )
    )
    paths = {d.file_path for d in diffs}
    # The view itself is patched; top_users directly references user_stats
    # and its bare post_count reference follows the rename.
    assert paths == {"db/views/user_stats.sql", "db/views/top_users.sql"}
    assert unverified == []


async def test_rename_skips_views_outside_affected_set(connector):
    diffs, unverified = await connector.generate_changes(
        _rename_event(
            "model.shop.users.email", "email", "contact_email",
            affected=["view.analytics.daily_signups"],
        )
    )
    # daily_signups references users but has no bare `email` — nothing changes
    # and nothing else is touched.
    assert all(d.file_path != "db/views/user_stats.sql" for d in diffs)


async def test_rename_with_only_qualified_references_flags_unverified(tmp_path):
    views = tmp_path / "views"
    views.mkdir()
    (views / "q.sql").write_text(
        "create view q as select u.email from users u"
    )
    connector = SqlViewsConnector(config={"project_dir": str(tmp_path), "views_dir": "views"})
    diffs, unverified = await connector.generate_changes(
        _rename_event("model.shop.users.email", "email", "contact_email",
                      affected=["view.public.q"])
    )
    assert diffs == []
    assert unverified == ["view.public.q"]


async def test_non_structural_event_produces_no_changes(connector):
    event = SemanticChangeEvent(
        change_event_id="evt-2",
        entity_id="view.public.user_stats.post_count",
        change_type=ChangeType.SEMANTIC,
        semantic_context="meaning changed",
        affected_entity_ids=[],
        reasoning="",
        change={"description": "new meaning"},
    )
    diffs, unverified = await connector.generate_changes(event)
    assert diffs == []
    assert unverified == []
