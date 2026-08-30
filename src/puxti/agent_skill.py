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

## When to use this skill

- Any question about a metric or KPI ("what was revenue / active users / win rate…").
- Any "which model or table should I trust for X" or "what does X mean" question.
- Before proposing a change to a model, to understand its blast radius.

## Workflow — do this before answering a metric question

1. Identify the entities the question depends on — the metric's model, and the sources
   and staging models feeding it.
2. Call `describe_entity` on each. Read the definition and its semantic edges. An edge
   like `derived_from` pointing at a canonical source the model bypasses is a red flag.
3. Call `definition_history` on the key entity. **Honor the latest version.** If the
   model's SQL reflects an older definition than the newest one, treat the model as
   unreliable and say so — do not report its number as trustworthy.
4. For "what breaks if I change X" use `impact_of_change`; for "who reads X" use `consumers`.

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

- `current` — the model's SQL reflects the latest definition; trust the number.
- `stale: <reason>` — a newer definition exists that the model does not reflect; treat
  the number as unreliable and explain why.

Example:

    — via puxti · `model.clariva.stg_opportunities` def v2 (by user, 2026-07-17) ·
      stale: fct_revenue still reads legacy total_value

Run `puxti scan` in the project first if a tool reports that an entity is not found.
"""


def render_skill() -> str:
    """Return the skill markdown to write or print. A seam for future interpolation."""
    return SKILL_MD
