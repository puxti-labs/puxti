"""The agent skill puxti emits via `puxti mcp init`.

This is puxti's answer to the "skills" layer from Anthropic's self-service analytics
post: the four MCP tools give an agent *access* to the knowledge graph, but nothing
tells it *when* to consult them or *how* to cite provenance. Without that, an agent
grabs the first plausible model and reports a silently-wrong number. This file is the
convention that closes the gap — written once here, emitted as a Claude Code SKILL.md
(or, with --print, as markdown to paste into any other agent).

Kept as a Python string constant (not a package data file) so it ships automatically
in the wheel and sdist with no packaging changes.
"""

from __future__ import annotations

SKILL_MD = """---
name: puxti-analytics
description: >-
  Answer metric, KPI, and "what does this data mean" questions truthfully by
  checking puxti's knowledge graph for the current definition of each entity
  before trusting any model. Use whenever a question involves a business metric,
  or which model or table to trust for a number.
---

# Puxti analytics — trustworthy answers over the knowledge graph

Puxti tracks what each entity in this project *means* and how that meaning has changed
over time. A model's SQL can silently lag its definition: the column name never changes,
so nothing errors, but the number is wrong. Your job is to catch that before you answer.

You have four read-only puxti MCP tools:
`describe_entity`, `definition_history`, `impact_of_change`, `consumers`.

**These tools return meaning, not SQL.** They give you each entity's definition, how that
definition has changed, its semantic edges, and what depends on it — never the model's
query. The model's SQL lives in the dbt project files in this repo; read it there. Trust
is a comparison *you* make: the definition puxti holds versus what the SQL actually
computes. Puxti never flags staleness for you — it hands you the current meaning to check
the SQL against.

## When to use this skill

- Any question about a metric or KPI ("what was revenue / active users / win rate…").
- Any "which model or table should I trust for X" or "what does X mean" question.
- Before proposing a change to a model, to understand its blast radius.

## Workflow — do this before answering a metric question

1. **Identify the entities** the question depends on — the metric's model, and the sources
   and staging models feeding it.
2. **Call `describe_entity`** on each. Read the current definition and its semantic edges.
   An edge like `derived_from` or `feeds` pointing at a canonical source the model bypasses
   is a red flag: the model may be computing the number from the wrong place.
3. **Call `definition_history`** on the key entity to see whether its meaning changed
   recently. A newer version (say v2) that post-dates the model's SQL is the signal a model
   may be stale. **Honor the latest version**, not the one the SQL was written against.
4. **Decide current vs stale.** Open the model's `.sql` in the dbt project and check what it
   computes against the latest definition. If the SQL still reflects an older definition —
   reads a legacy column, sums the wrong grain — treat the number as unreliable and say so;
   do not report it as fact.
5. For "what breaks if I change X" use `impact_of_change`; for "who reads X" use `consumers`.

If a tool returns `{"error": "… not found"}`, the graph is not populated for that entity —
say so and ask the user to run `puxti scan`, rather than guessing an answer.

## Answer honestly

- If the definitions do not support an answer, say so. Do not invent a number.
- Separate a **retrieved value** (a number that exists in a model) from a **modeled
  estimate** (a projection or a "what if"). Puxti tells you what things mean and what
  depends on them; it does not compute forecasts. If the question needs modeling or
  assumptions, name that and say what you would need.
- If two models define the same concept differently, surface the conflict — never pick
  one silently.

## Provenance footer — end every metric answer with this

Append a footer citing where the answer's trust comes from:

    — via puxti · `<entity_id>` def v<version> (by <created_by>, <created_at>) · <trust>

where `<trust>` is one of:

- `current` — the model's SQL matches the latest definition; trust the number.
- `stale: <reason>` — a newer definition exists that the model's SQL does not reflect;
  treat the number as unreliable and explain why.

Example:

    — via puxti · `model.clariva.stg_opportunities` def v2 (by user, 2026-07-17) ·
      stale: fct_revenue still reads legacy total_value
"""


def render_skill() -> str:
    """Return the skill markdown to write or print. A seam for future interpolation."""
    return SKILL_MD
