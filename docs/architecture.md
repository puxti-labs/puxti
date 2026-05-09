# Puxti — Architecture

This document describes how Puxti works internally. It's aimed at contributors,
integrators, and curious users who want to understand the design before reading
the code.

For installation and usage, see the [README](../README.md). For codebase
conventions, see [AGENTS.md](../AGENTS.md).

---

## Table of contents

1. [Guiding principles](#1-guiding-principles)
2. [System overview](#2-system-overview)
3. [The knowledge graph](#3-the-knowledge-graph)
4. [Data model](#4-data-model)
5. [Command flows](#5-command-flows)
6. [The connector layer](#6-the-connector-layer)
7. [LLM usage](#7-llm-usage)
8. [Trust and safety](#8-trust-and-safety)
9. [Tech stack](#9-tech-stack)
10. [Design choices and trade-offs](#10-design-choices-and-trade-offs)

---

## 1. Guiding principles

These constrain every design decision. If a proposal violates one, it gets
revisited.

**Read-first, write-on-confirm.** Puxti never modifies a user's data stack
without explicit human confirmation. All generated changes are delivered as
reviewable PRs. The system observes and proposes; the human decides.

**Semantic capture is in the critical path.** Every propagation flow passes
through a step where the user describes what a change means. This is not
optional. It's the mechanism by which the knowledge layer is built up over
time.

**Connectors are modular and isolated.** No connector knows about another
connector. The orchestration layer is the only thing that understands the full
graph. This keeps integrations testable and replaceable.

**The knowledge layer compounds.** Every change enriches Puxti's understanding
of the org's data semantics. Design decisions are evaluated against whether
they support or undermine this compounding property.

**Trust is earned incrementally.** Read access first, write access only when a
feature requires it. Never ask for more permissions than the current operation
needs.

---

## 2. System overview

Puxti is a Python CLI. It reads from a dbt project (and optionally an Airflow
project), populates a knowledge graph, and generates GitHub PRs in response to
declared changes. There is no backend, no hosted service, no account.

```
┌────────────────────────────────────────────────────────────┐
│                          CLI                               │
│   scan · capture · redefine · link · correct · describe   │
└──────────────────────────┬─────────────────────────────────┘
                           │
                           ▼
┌────────────────────────────────────────────────────────────┐
│                       Puxti core                           │
│                                                            │
│   ┌──────────┐   ┌──────────────┐   ┌────────────────┐    │
│   │  Scanner │   │   Capture &  │   │  Propagation   │    │
│   │          │──▶│    Redefine  │──▶│    Engine      │    │
│   └──────────┘   └──────────────┘   └────────┬───────┘    │
│                                              │             │
│   ┌──────────────────────────────────────────▼─────────┐  │
│   │             Knowledge Graph                        │  │
│   │  (entities · definitions · lineage · semantics)    │  │
│   └────────────────────────────────────────────────────┘  │
└──────────────────────────┬─────────────────────────────────┘
                           │
                           ▼
┌────────────────────────────────────────────────────────────┐
│                   Connector layer                          │
│                                                            │
│           dbt          Airflow         GitHub              │
│       (manifest)      (DAG parse)    (PR creation)         │
└────────────────────────────────────────────────────────────┘
```

The core modules are stateless processors. All persistent state lives in the
knowledge graph.

---

## 3. The knowledge graph

The knowledge graph is the persistent state of Puxti. It stores:

- **Entities** — dbt models, columns, sources, Airflow tasks
- **Definitions** — semantic descriptions of each entity, versioned over time
- **Structural lineage** — directed graph of code-level dependencies
- **Semantic edges** — conceptual relationships between entities (derived_from,
  filtered_from, etc.) that may not be visible from code alone
- **Change history** — every captured change, with timestamps and authorship

The graph has two distinct layers that are not conflated:

**Structural lineage** is what entity depends on which. It's populated by
connectors reading the actual stack — the dbt manifest, Airflow DAGs.
Deterministic, mechanical, connector-specific.

**The semantic graph** is what each entity *means* and how concepts relate
across the full stack regardless of which connector owns them. It's populated
by semantic capture — built from user descriptions accumulated over every
change.

The semantic graph is first-class, not an annotation on top of structural
lineage. This is what allows Puxti to handle chained semantic dependencies.
A worked example:

> `sales → revenue → marketing cost ratio → board dashboard`

A structural lineage tool sees that `revenue` references `sales`. The semantic
graph knows that *revenue is calculated from sales excluding refunds*, so
redefining *sales* to include trial conversions makes *revenue* wrong, the
*ratio* misleading, and the board dashboard untrustworthy — even when nothing
in the dbt graph technically broke.

The semantic graph is queryable independently of connectors. The propagation
engine queries it to understand meaning before dispatching to connectors for
the structural changes.

In v0.6.0 the graph is stored in Neo4j. A SQLite port is in progress to remove
the Docker requirement for OSS users. The graph schema is portable across both
backends; the abstraction lives in `graph/repository.py`.

---

## 4. Data model

High-level entity model. Implementation lives in `src/puxti/graph/models.py`.

```
Entity
  id, name, type (model | column | source | task | dashboard | metric)
  source_connector
  project              ← dbt project name or Airflow instance name
  created_at, updated_at

Definition
  id, entity_id
  description (semantic text)
  version, created_at
  created_by (user | llm | scan | correct)
  change_event_id (the change that produced this definition)

Edge (lineage)
  from_entity_id, to_entity_id
  type (depends_on | derived_from | feeds | references)
  connector

ChangeEvent
  id, type (structural | semantic)
  source_entity_id
  change (before/after JSON)
  semantic_context (text)
  status (detected | captured | propagated | closed)
  created_at

CorrectionEvent
  id, entity_id
  old_definition_id, new_definition_id
  edges_kept, edges_updated, edges_removed
  classified_as (correction | real_change)
  created_at, created_by
```

---

## 5. Command flows

### 5.1 `scan` — populate the knowledge graph

```
puxti scan --dbt-project-dir <path> [--interactive | --auto]
      │
      ▼
Read dbt manifest
      │
      ▼
Extract entities (models, columns, sources)
Extract structural lineage edges
      │
      ▼
LLM infers starter definitions from
SQL + upstream context + any dbt yml descriptions
      │
      ▼
User confirms (interactive: one at a time)
        or reviews and confirms in batch (auto)
      │
      ▼
Write entities + definitions to knowledge graph
      │
      ▼
LLM proposes semantic edges (derived_from, etc.)
      │
      ▼
User confirms semantic edges as a group
      │
      ▼
Write semantic edges to knowledge graph
```

`scan` is safe to re-run. It upserts entities and definitions, so new models
and columns are added and existing ones updated.

### 5.2 `capture` — propagate a structural change

For column renames and other deterministic schema changes.

```
puxti capture --entity <id> --before <old> --after <new>
              --description <semantic context>
      │
      ▼
LLM enriches the description against existing
knowledge graph context for the entity
      │
      ▼
Scan dbt models for references to <old>
      │
      ▼
Generate diffs replacing <old> with <new>
      │
      ▼
If Airflow connector configured:
  Identify upstream tasks via FEEDS edges
  Generate annotation diffs for affected DAGs
      │
      ▼
Open PRs (one per affected repo, linked by change_id)
      │
      ▼
Write change event + propagation results to KG
```

### 5.3 `redefine` — propagate a semantic change

For meaning shifts that don't change the schema. The hard case.

```
puxti redefine --entity <id> --description <new meaning>
      │
      ▼
LLM enriches description against existing context
      │
      ▼
Traverse the semantic graph for affected downstream entities
(falls back to structural lineage if semantic graph is sparse)
      │
      ▼
Walk structural ancestors upstream to identify
passthrough chain — every model between the
ingestion boundary and the redefined entity
needs to surface the new attribute
      │
      ▼
Generate diffs with depth-aware confidence:
  Hop 1: PUXTI [high confidence] — direct dependent
  Hop 2: PUXTI [verify carefully]  — 2-hop inference
  Hop 3+: PUXTI [manual review]    — annotation only
      │
      ▼
Open a single PR with both upstream passthrough
and downstream semantic diffs, ordered for safe deployment
```

The PR is **deployable in order**: upstream passthrough first so new attributes
exist before downstream models reference them. Each diff that depends on
upstream changes carries an `⚠️ UPSTREAM DEPENDENCY` note.

### 5.4 `correct` — fix an inaccurate definition

This handles the case where Puxti's representation of an entity is wrong —
either because the LLM inferred it incorrectly during scan, or because the
user wants to refine it.

This is **distinct from `redefine`**. Conflating them would either generate
unnecessary PRs (if a real correction were treated as a change) or silently
miss propagation work (if a real change were treated as a correction).

```
puxti correct --entity <id>
      │
      ▼
Show current definition + all edges involving the entity
      │
      ▼
User provides corrected definition
      │
      ▼
LLM re-evaluates each edge against the new definition
      │
      ▼
Per edge, user decides: keep / update description / remove
      │
      ▼
Classification prompt:
  "Is this a correction (KG was wrong)
   or a real change (business meaning shifted)?"
      │
      ├── Correction → write new definition version + edges,
      │                no PRs generated
      │
      └── Real change → hand off to redefine flow with the
                        new definition pre-filled
```

Atomic writes: a new definition version and all edge changes write together
or not at all. Versioned, not overwritten — the previous definition is
preserved in history.

Corrections on scan-inferred definitions are logged separately. Patterns in
those logs are how we identify where auto-scan is weakest.

### 5.5 `link` — declare a cross-system edge

When a data producer outside dbt feeds a dbt source, declaring the link tells
Puxti to trace semantic changes across the system boundary.

```
puxti link --from task.airflow.<dag>.<task>
           --to source.<project>.<table>
           --description <what flows and what it means>
      │
      ▼
Write FEEDS edge to knowledge graph
```

Subsequent `capture` and `redefine` calls then surface the upstream task as
context in generated PRs.

---

## 6. The connector layer

Each connector is an isolated module implementing a standard interface. The
core never calls connector internals directly — only through the interface.

### 6.1 Base interface

```python
from abc import ABC, abstractmethod

class BaseConnector(ABC):

    @abstractmethod
    def health_check(self) -> bool:
        """Verify connection and required permissions."""

    @abstractmethod
    def extract_schema(self) -> list[Entity]:
        """Read current state of all entities in this connector."""

    @abstractmethod
    def extract_lineage(self) -> list[Edge]:
        """Read dependency graph for entities in this connector."""

    @abstractmethod
    def generate_changes(self, event: SemanticChangeEvent) -> list[FileDiff]:
        """Given a semantic change event, generate the required file changes.
        Must NOT write anything — returns diffs only."""
```

### 6.2 Connectors in v0.6.0

| Connector | Read (schema + lineage) | Write (generate diff) |
|---|---|---|
| `dbt` | yes — parses manifest.json | yes — produces SQL diffs |
| `airflow` | yes — parses DAG files | yes — produces docstring annotations |
| `github` | yes — repo metadata | yes — opens PRs |

Additional connectors (Dagster, Looker, Superset, Mode, BI tools generally)
are not part of the OSS package today. Contributions welcome — see the
contributor guide in the README.

### 6.3 Configuration

Each connector is configured via a `.puxti.yml` at the workspace root:

```yaml
version: 1

connectors:
  dbt:
    project_dir: ./dbt
    repo: your-org/your-dbt-repo
    base_branch: main

  airflow:
    project_dir: ./airflow
    repo: your-org/your-airflow-repo
    dags_dir: dags/
    base_branch: main
```

Workspace discovery walks up from the current directory, git-style, until
either a `.puxti.yml` is found or filesystem root is reached. CLI flags
override `.puxti.yml` values, which override environment variables.

---

## 7. LLM usage

Puxti uses Anthropic's Claude API for semantic reasoning. LLM calls are never
in the read path — only in semantic capture, propagation reasoning, and
scan-time inference.

### 7.1 Where LLMs are used

| Task | Notes |
|---|---|
| Semantic capture enrichment | Structures user input into a clean definition |
| Scan — definition inference | Infers starter definitions from SQL + context |
| Scan — semantic edge proposal | Proposes derived_from edges between models |
| Propagation reasoning | Reasons about non-deterministic semantic changes |
| `correct` — edge re-assessment | Reviews affected edges when a definition changes |
| Release note generation | Drafts human-readable change summaries |

### 7.2 Where LLMs are not used

- Schema diffing (deterministic)
- dbt model parsing (uses dbt's own manifest)
- Lineage extraction (deterministic graph traversal)
- PR generation for purely structural changes (templated)

### 7.3 Prompt design principles

- Always provide the affected entity's knowledge graph context before asking
  for reasoning
- Keep user-facing prompts under three sentences — Puxti is a productivity
  tool, not a chat interface
- LLM outputs that become code changes are validated against connector
  schemas before being included in a PR
- Never include credentials or raw data — only metadata and definitions

### 7.4 Confidence model for SQL generation

For semantic changes, the LLM attempts to generate SQL diffs for affected
downstream models. Confidence is determined by traversal depth — deeper chains
compound uncertainty at each hop.

| Traversal depth | LLM action | PR annotation |
|-----------------|-----------|---------------|
| Hop 1 | Generate SQL diff | `PUXTI [high confidence] — verify reasoning` |
| Hop 2 | Generate SQL diff | `PUXTI [verify carefully] — 2-hop inference` |
| Hop 3+ | Annotation only | `PUXTI [manual review required]` |

Why depth drives confidence: the human expert's mental map of metric
dependencies is itself a graph — the same structure Puxti models explicitly.
Shallow graphs (1–2 hops, e.g. a marketing data mart) have simple additive or
filtered logic the LLM can reason about reliably. Deep graphs (3+ hops, e.g.
insurance P&L, balance sheets, regulatory capital ratios) compound uncertainty
at each step. A wrong inference at hop 2 silently corrupts everything
downstream.

The graph depth makes the risk visible. Engineers see *why* something needs
manual review, not just that it does.

There's a more honest framing of the same point: Puxti's effective propagation
depth depends on the quality of the SQL it's reading. Well-structured dbt
projects with clear lineage support reliable propagation deeper into the
graph; tangled projects get conservative behavior. The system is honest about
which side it's on for a given query.

### 7.5 Handling large SQL files

Real-world data stacks contain SQL files that can run to hundreds or
thousands of lines — deeply chained CTEs, complex window functions,
multi-stage aggregations. Sending a full large SQL file to an LLM risks
hitting token limits and producing a truncated, invalid response.

Three strategies, in order of preference:

**Selective extraction via SQL AST (preferred).** Since Puxti always knows
*what changed* (a specific column, table, or concept), a SQL parser
(`sqlglot`) extracts only clauses referencing the changed entity — JOIN
conditions, SELECT expressions, WHERE predicates, CTE references. The LLM
receives the relevant 10–20 lines, not the full file. Changes are applied
back to the original via a targeted patch. Surgical, dialect-aware, keeps
context small.

**CTE-aware chunking (fallback).** When selective extraction isn't precise
enough, the SQL is split on CTE boundaries — natural, well-defined seams.
Each CTE is processed independently with summaries of preceding CTEs as
context. Results are reassembled in order. Works well for pipelines
structured as linear CTE chains.

**Two-pass: locate then rewrite.** First pass: send the full SQL and ask
the LLM to identify which lines or expressions reference the changed entity
(response is line numbers and short excerpts). Second pass: send only those
excerpts for the actual rewrite. Avoids needing a SQL parser, at the cost of
one additional LLM call.

`sqlglot` is the preferred parsing library — it handles BigQuery, Snowflake,
and Databricks dialects natively.

---

## 8. Trust and safety

Trust is the hardest thing to earn in a tool that touches production data
infrastructure. Every design decision is evaluated against this model.

### 8.1 Permission levels

| Level | What Puxti can do | When it applies |
|---|---|---|
| Read-only | Extract schema, lineage, definitions. No writes anywhere. | Onboarding, initial scan |
| Propose | Read + open draft PRs in VCS. Cannot merge. | Default operating mode |
| Auto-apply | Propose + merge PRs that pass all checks. | Not available in v0.6.0 |

Auto-apply is intentionally not in v0.6.0 and may never be a default. It might
be introduced later for specific low-risk change types (e.g. documentation-only
updates).

### 8.2 What Puxti never does

- Modifies warehouse schemas directly (no `ALTER TABLE`, no `DROP COLUMN`)
- Pushes to main or production branches
- Merges its own PRs
- Stores raw warehouse data — only metadata, schemas, and definitions
- Sends raw data to LLM providers — only schemas, model SQL, and definitions
  go to the user's own Anthropic API endpoint

### 8.3 What leaves your environment

In OSS mode (v0.6.0), the only outbound traffic from Puxti is to:

- Your own Anthropic API endpoint, with your key. Sends dbt model SQL and
  metadata (model names, column names, descriptions). No raw warehouse data.
- Your own GitHub API endpoint, with your token. Reads repo state and opens
  PRs.
- Your local Neo4j instance.

Nothing goes to a Puxti-controlled backend, because there isn't one.

### 8.4 Audit log

Every action Puxti takes writes to an immutable log:

```
timestamp | actor (user|system) | action | entity | before | after | pr_url
```

Users can inspect everything Puxti has ever done in their stack.

---

## 9. Tech stack

| Layer | Choice |
|---|---|
| Language | Python 3.12+ |
| Package manager | `uv` |
| CLI framework | Typer |
| Settings/validation | Pydantic |
| Graph store | Neo4j (SQLite port in progress) |
| LLM provider | Anthropic Claude |
| SQL parsing | `sqlglot` (when needed) |
| dbt parsing | dbt's own manifest |
| Testing | pytest |
| Lint/format | Ruff |

Decisions about the stack are made in service of OSS adoption. Anything that
adds install friction (Docker, server processes, account creation) is treated
as a tax to be reduced.

---

## 10. Design choices and trade-offs

A few decisions worth flagging because the alternatives are reasonable and
might come up.

**Why a graph store rather than a relational database?** Lineage is
fundamentally a graph problem. Multi-hop traversals over chained
dependencies, cross-tool propagation paths, and impact analysis all map
naturally onto graph queries. Relational schemas can model this but with
more complexity and worse performance at the depths we care about. The
trade-off is operational complexity for OSS users — Neo4j requires Docker.
A SQLite port is in progress to address this for the OSS mode.

**Why CLI-first rather than a web UI?** The CLI lives in the workflow
engineers already use. A web UI requires a separate interaction model and
typically a hosted backend, both of which are additional adoption taxes.
Eventually both surfaces will exist; CLI first because it's where contributors
and design partners actually work.

**Why generate proposals for non-deterministic changes rather than just
flagging them?** A proposal with transparent reasoning is useful — the human
can see where the reasoning broke down and correct it, and that correction
feeds back into the semantic graph. A flag without a proposal is a data
catalog: it observes but doesn't act. That's explicitly not the product.

The PR is the safety net. Wrong proposals in reviewable PRs are starting
points, not liabilities. Users don't expect a perfect automated answer; they
expect something that moves the work forward and that they can validate.

**Why one PR per affected repo, linked by change ID, rather than atomic
cross-repo transactions?** Cross-repo atomic transactions are fragile and
hard to roll back. Independent PRs sharing a `change_id` give reviewers in
each repo full context — "this PR is part of change X which also touches
dbt and Airflow" — without coupling the PRs to each other's success. The
change ID is the traceability primitive. Audit logs use it to track the
full propagation lifecycle.

**Why force agreement on definition conflicts rather than picking one?**
When two teams define the same entity differently, silently picking one
definition would undermine the entire semantic layer. Surfacing the conflict
explicitly — both definitions side by side, like a merge conflict in
GitHub — and blocking propagation until both owners agree is intentional.
Forcing alignment is the product.

**Why not auto-apply?** Auto-applying changes to production data
infrastructure breaks the trust model. The first auto-applied wrong change
ends the relationship. Puxti is designed around human review, and PRs are
the safety net that makes the rest of the architecture viable.