"""SQL text utilities shared by producer connectors that patch SQL files."""

import re


def rename_column_in_sql(sql: str, old_name: str, new_name: str) -> str:
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
