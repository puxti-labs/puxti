"""SQL text utilities shared by producer connectors that patch SQL files."""

import re

# A quoted Jinja string, so a closing delimiter inside it ("}}", "%}") does not
# end the span. Unquoted characters exclude quotes, which keeps the pattern
# unambiguous and free of catastrophic backtracking.
_JINJA_STRING = r"""'(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*\""""
_JINJA_BODY = rf"""(?:{_JINJA_STRING}|[^'"])*?"""

# Capture group makes re.split keep the spans, so callers can tell them apart.
# Comments end at the first "#}" regardless of quotes, as in Jinja itself.
_JINJA_SPAN = re.compile(
    rf"(\{{\{{{_JINJA_BODY}\}}\}}|\{{%{_JINJA_BODY}%\}}|\{{#.*?#\}})", re.DOTALL
)


_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")


def rename_column_in_sql(sql: str, old_name: str, new_name: str) -> str:
    """Replace references to old_name with new_name in a SQL string.

    Handles common patterns:
    - bare column references:       old_name → new_name
    - aliased selections:           old_name as alias → new_name as alias
    - quoted identifiers:           `old_name`, "old_name", [old_name] → same quoting, new_name
    - qualified refs (alias.old_name): left completely unchanged

    Qualified references are deliberately not touched: at the SQL text level
    we cannot know which table alias maps to the source model, so renaming
    them risks both wrong renames (s.type where s is a different table) and
    broken SQL. Any dot before the name makes it qualified, whatever precedes
    the dot (an alias, a quoted name, a Jinja ref(), a parenthesis). Callers
    that cannot accept a partial rename use rename_column_fully instead.

    Jinja spans ({{ }}, {% %}, {# #}) are never modified. Callers that cannot
    accept a partial rename should check mentions_in_jinja first.

    String literals and SQL comments (-- and /* */) are never modified, and
    quoted identifiers (`x`, "x", [x]) are renamed only on an exact match.
    Bare references match case-insensitively and keep their case style.

    Uses word-boundary matching to avoid partial replacements
    (e.g. 'recorded_date' should not match 'date').

    Raises ValueError if new_name is not a plain SQL identifier.
    """
    return _rename(sql, old_name, new_name)[0]


def rename_column_fully(sql: str, old_name: str, new_name: str) -> str | None:
    """Rename like rename_column_in_sql, or return None if that would be partial.

    The result is None when the SQL has a qualified column reference to
    old_name (alias.old_name), which the rename never rewrites. The caller
    should report the whole model as unverified instead of writing it.
    Relation names (from raw.old_name) and path segments (a.old_name.b) are
    not column references and do not count.
    """
    renamed, qualified = _rename(sql, old_name, new_name)
    return None if qualified else renamed


def _rename(sql: str, old_name: str, new_name: str) -> tuple[str, bool]:
    if not _IDENTIFIER.fullmatch(new_name):
        raise ValueError(f"new column name is not a valid SQL identifier: {new_name!r}")
    spans: list[str] = []

    def stash(m: re.Match) -> str:
        spans.append(m.group(0))
        return f"\x00{len(spans) - 1}\x00"

    # Jinja spans become quote-free placeholders so a literal or comment that
    # contains one ('{{ var("x") }}') stays a single token for the rename.
    masked = _JINJA_SPAN.sub(stash, sql)
    renamed, qualified = _rename_in_plain_sql(masked, old_name, new_name)
    restored = re.sub(r"\x00(\d+)\x00", lambda m: spans[int(m.group(1))], renamed)
    return restored, qualified


def mentions_in_jinja(sql: str, name: str) -> bool:
    """Return True if name appears as a whole word inside a Jinja span.

    Such a reference (a macro argument, a {% set %} list, a comment) cannot be
    renamed safely at the text level, so the model needs manual review.
    """
    pattern = re.compile(rf"\b{re.escape(name)}\b", re.IGNORECASE)
    return any(pattern.search(span) for span in _JINJA_SPAN.findall(sql))


# Characters of a qualifier chain such as proj.ds.tbl, "s"."t", [s].[t] or a
# Jinja placeholder, walked back over to find the keyword before a relation.
_QUALIFIER_CHARS = re.compile(r"""[\w$."`\[\]\x00]*$""")
_RELATION_KEYWORD = re.compile(
    r"\b(?:from|join|into|update|table|view)\s+$", re.IGNORECASE
)


def _rename_in_plain_sql(sql: str, old_name: str, new_name: str) -> tuple[str, bool]:
    """Rename bare references; also report whether a qualified one was seen.

    Qualified references (alias.col, table.col) are left completely unchanged.
    We cannot determine from SQL text alone which table alias maps to the
    source model being renamed, so touching qualified refs risks both wrong
    renames (s.type where s is a different table) and broken SQL (AS alias
    in WHERE clauses is invalid). Bare references are unambiguous within a
    model that is already scoped to the affected entity set.
    """
    old = re.escape(old_name)
    pattern = re.compile(
        rf"""(?P<skip>'(?:''|[^'])*'|--[^\n]*|/\*.*?\*/)"""
        rf"""|(?P<quoted>`[^`]*`|"(?:""|[^"])*"|(?<![\w\])])\[[^\]\[,()'"+\-*/<>=!|&%.:]*\])"""
        rf"""|\.\s*\b(?P<qualified>{old})\b"""
        rf"""|\b(?P<bare>{old})\b""",
        re.DOTALL | re.IGNORECASE,
    )
    qualified = False

    def replace(m: re.Match) -> str:
        nonlocal qualified
        if m.group("skip") is not None:
            return m.group(0)
        if m.group("qualified") is not None:
            if not _is_relation_segment(m.string, m.start(), m.end()):
                qualified = True
            return m.group(0)
        if m.group("quoted") is not None:
            # Quoted identifiers are case-sensitive: exact match only.
            text = m.group(0)
            open_, inner, close = text[0], text[1:-1], text[-1]
            if inner != old_name:
                return text
            before = m.string[: m.start()].rstrip()
            if before.endswith("."):
                if not _is_relation_segment(m.string, len(before) - 1, m.end()):
                    qualified = True
                return text
            return f"{open_}{new_name}{close}"
        return _match_case(m.group("bare"), old_name, new_name)

    return pattern.sub(replace, sql), qualified


def _is_relation_segment(sql: str, dot: int, end: int) -> bool:
    """Return True if the name after sql[dot] is part of a relation, not a column.

    It is when another segment follows (a.name.b) or when the dotted chain
    comes right after FROM, JOIN, INTO, UPDATE, TABLE or VIEW (from raw.name).
    """
    if sql[end:].lstrip().startswith("."):
        return True
    head = sql[:dot]
    chain = _QUALIFIER_CHARS.search(head)
    return bool(_RELATION_KEYWORD.search(head[: chain.start()]))


def _match_case(found: str, old_name: str, new_name: str) -> str:
    """Write new_name in the case style the bare reference used.

    Unquoted identifiers are case-insensitive, so ORDER_DATE and order_date
    both refer to the column. An exact match gets new_name as given; an
    all-upper or all-lower variant gets new_name in the same style.
    """
    if found == old_name:
        return new_name
    if found.isupper():
        return new_name.upper()
    if found.islower():
        return new_name.lower()
    return new_name
