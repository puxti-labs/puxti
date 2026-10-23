# Changelog

## [Unreleased]

### Added

- **`search_entities` MCP tool.** The other MCP tools take an exact `entity_id`, and nothing let an agent find one, so it guessed the format and got "not found". `search_entities(query, type=None, limit=20)` matches every word of the query case-insensitively against entity names and IDs ("average order" finds `average_order_value`), optionally filtered by entity type, and returns each hit's ID, name, type, project, status and the first line of its current definition, plus a `truncated` flag when more entities matched than the limit. Exact name and ID matches rank first. The `puxti-analytics` skill written by `puxti mcp init` now tells the agent to search before it calls the other tools. ([#71](https://github.com/puxti-labs/puxti/issues/71))

### Fixed

- **Latest definitions keep their stored timestamp.** `get_latest_definition` and `get_all_entities_with_definitions` did not read `created_at` on either backend, so the value fell back to the current time. The MCP `describe_entity` tool, and the provenance footer the `puxti-analytics` skill builds from it, reported every definition as written at the moment of the call. `definition_history` was not affected.

## [0.16.0] — 2026-10-19

### Changed

- **Default model is now `claude-sonnet-5-5`.** It replaces `claude-sonnet-4-6` when `LLM_MODEL` is not set. Cost estimates use its pricing ($2 input, $10 output per million tokens). Responses that lead with a thinking block are read correctly, and the scanner's definition call allows 2048 output tokens so thinking cannot exhaust the limit. Set `LLM_MODEL=claude-sonnet-4-6` to keep the previous model. (#64)

### Fixed

- **Column rename skips string literals and SQL comments, handles `[bracket]` quoting and case.** The SQL text rename no longer rewrites matches inside `'...'` literals or `--` and `/* */` comments. `[old_name]` is renamed like backtick and double-quoted identifiers, all of which match exactly (case-sensitive). Bare references still match case-insensitively but keep their case style (`ORDER_DATE` becomes `RECORDED_DATE`). A new name that is not a plain SQL identifier raises `ValueError` instead of being written into the file. (#46)
- **`puxti capture` no longer opens an empty PR when every affected model is unverified.** A connector that returned no diffs but a non-empty unverified list still reached the PR step, which created a branch with no commits. Such results are now treated as a manual-review outcome: the unverified models are listed, no branch or PR is created, and the command exits 0. When only some connectors have diffs, PRs are opened for those connectors alone. (#59)
- **Column rename no longer writes half-renamed models.** A model or view that mixes bare and qualified (`alias.col`) references to the renamed column used to get only the bare ones rewritten, leaving broken SQL with no warning. Downstream dbt models with only qualified references were skipped silently. Both cases are now reported as unverified and no diff is written for them. Any dot before the name counts as a qualifier, including `{{ ref(...) }}.col` and `(o).col`; relation names such as `from raw.col` and dots inside quoted names do not. (#47)

## [0.15.0] — 2026-10-11

### Added

- **Claude on Bedrock via Mantle's Anthropic endpoint.** `LLM_PROVIDER=bedrock-anthropic` points the native Anthropic backend at `https://bedrock-mantle.us-east-1.api.aws/anthropic` (other regions via `LLM_BASE_URL`) and authenticates with `LLM_API_KEY` as a bearer token. Claude users on Bedrock get the Anthropic Messages API instead of the OpenAI-compatible adapter. The endpoint's `count_tokens` support is not confirmed: when it answers 404/405/501, `--dry-run` falls back to an approximate count and `puxti health` checks the key with a one-token completion. `ANTHROPIC_API_KEY` is never sent to this endpoint. The existing `bedrock` provider is unchanged.

### Changed

- Minimum `anthropic` SDK is now 1.0.0. Earlier versions send `ANTHROPIC_API_KEY` alongside a bearer token and lack `messages.count_tokens`.

## [0.14.0] — 2026-10-03

### Added

- **Postgres storage backend.** The Knowledge Graph can now be stored in Postgres instead of the default local SQLite file. Install the extra (`pip install 'puxti[postgres]'`) and set `DATABASE_URL` to a Postgres DSN; puxti creates its tables on first connect. Both backends sit behind one `GraphStore` interface and store identical data, so every command and the MCP server work the same against either. SQLite remains the default — nothing changes for existing users, and `asyncpg` is only needed when the Postgres backend is selected. `puxti health` verifies the Postgres connection (not just the config) and reports which backend is active.

## [0.13.0] — 2026-09-19

### Added

- **`puxti define` and `puxti bind`** — author a metric's meaning before a model implements it. `puxti define` records a *proposed* metric in the Knowledge Graph with no LLM call, no dbt manifest, and no PR; the definition is intent, not code. `puxti scan` offers to bind a proposed metric to a scanned model with a matching name (you confirm; it never binds silently), and `puxti bind` binds one explicitly. A proposed metric is never reported as a fact by the MCP tools or the agent skill until it is bound: `describe_entity` returns `"bound": false`, and `puxti describe` lists proposed metrics in their own "Proposed (unbound)" section.
- **`puxti graph`** — render the Knowledge Graph as a self-contained, interactive HTML page (`puxti graph -o kg.html`, add `--open` to launch it). A dependency-free, force-directed view of entities and their lineage and semantic relationships; click a node for its definition, relationships, and definition history. No server and no external dependencies. Proposed metrics are shown with a distinct style.
- **Continuous integration.** A GitHub Actions workflow runs the test suite on Python 3.12 and 3.13 and lints `src/` with ruff on every pull request and push to `main`. The pre-existing `src/` lint findings were cleaned up and the lint step is now a required gate.

### Changed

- `puxti describe` shows a proposed metric's status on `--entity`, and the overview groups proposed metrics into their own flagged "Proposed (unbound)" section.

### Fixed

- `get_definition_history` now returns each definition's stored `created_at` instead of the current time, so the MCP `definition_history` tool and the graph's history timeline report real timestamps.

## [0.12.0] — 2026-09-01

### Added

- **`puxti mcp init`** — writes an agent skill (`.claude/skills/puxti-analytics/SKILL.md`) that teaches an agent *when* to use the MCP tools: check each entity's current definition and `definition_history` before trusting a model, honor the latest definition (flag a model whose SQL lags its definition as unreliable rather than reporting its number as fact), and end every metric answer with a provenance footer (definition version, author, date). `--print` emits the same markdown to stdout to paste into Cursor rules, `CLAUDE.md`, or any other agent; `--force` overwrites an existing file. This is puxti's small-scale version of the "skills" layer from Anthropic's self-service analytics work — the tools give an agent access to the graph, this tells it how to answer truthfully.

### Changed

- The MCP `describe_entity` tool now returns `created_at` in the `definition` block, so an agent can cite a definition's freshness for the provenance footer from a single call.

### Fixed

- **`puxti mcp serve` on a fresh install.** The server was written against the mcp v1 `FastMCP` API while the lockfile had advanced to mcp 2.x, so a clean `pip install puxti` broke `mcp serve` with `ModuleNotFoundError: No module named 'mcp.server.fastmcp'`. Migrated the server to the mcp 2.x `MCPServer` API and pinned `mcp>=2.1.1`. Tool behavior is unchanged.

## [0.11.0] — 2026-07-17

### Added

- **Prisma connector** (`connectors.prisma` in `.puxti.yml`). Parses `schema.prisma` directly — no Node toolchain — turning models into table entities (`table.prisma.<Model>`), scalar/enum fields into columns, `@relation` foreign keys into lineage, and `///` doc comments into starter descriptions. `puxti capture` patches `schema.prisma` for column renames: `@map`-ed fields get the map string updated, unmapped fields get the identifier renamed including `references: [...]` lists in related models. Every Prisma diff description states that a `prisma migrate dev` + `prisma generate` is still required — the file edit alone does not change the database.
- **SQL views connector** (`connectors.sql_views` in `.puxti.yml`). Reads a directory of `CREATE VIEW` .sql files via sqlglot (configurable `dialect`), turning views into entities (`view.<schema>.<name>`), projections into columns, and FROM/JOIN references into lineage. `puxti capture` renames columns in affected views under the same single-hop safety rule the dbt connector uses; views it cannot verify are flagged for manual review instead of blind-patched.
- **Cross-connector reference resolution** (`core/resolution.py`). Producer connectors stay isolated; a view referencing `public.users` emits a `sqlref.` placeholder edge, and `puxti scan` resolves it against every configured producer's entities by database-level name (Prisma `@@map` names, dbt model/source names, view names). Ambiguous names are never resolved; unmatched references are kept dangling and reported.
- **Connector registry** (`connectors/registry.py`). The CLI builds producer connectors from `.puxti.yml` through one seam; `scan` now scans every configured producer, `health` checks Prisma/SQL-views configs, `capture` propagates through all producers and opens one PR per connector repo (`repo`/`repo_subdir`/`base_branch` per connector). New dependency: `sqlglot`.
- `CONTRIBUTING.md` — dev setup, test/lint commands, and PR expectations.
- `puxti correct` now offers to run the printed `puxti redefine` handoff when a correction is classified as a real change, while keeping the existing copy-paste behavior unless the user explicitly confirms.

### Changed

- Engines (scanner, redefine) now depend on the `BaseConnector` interface instead of `DbtConnector` concretely; file-location logic moved into the connector (`find_model_path`), removing the engines' access to private manifest internals. No behavior change — groundwork for non-dbt producer connectors. The entity ID grammar is now documented as puxti's own, connector-neutral scheme.
- The scanner decides which entities get LLM definitions by "connector exposes source text for it" (`get_model_sql_map`) instead of the hardcoded dbt `model` type — behavior-identical for dbt, and what lets Prisma models and SQL views get definitions.

---

## [0.10.0] — 2026-07-14

### Added

- **Provider-agnostic BYOK.** `puxti` now works with any LLM provider that speaks the OpenAI-compatible wire format, selected via `LLM_PROVIDER` + `LLM_MODEL` + `LLM_API_KEY`: OpenAI, Mistral, GLM/Zhipu, DeepSeek, Groq, OpenRouter, Gemini, AWS Bedrock (Mantle), local Ollama, or any custom endpoint (`LLM_BASE_URL`, e.g. vLLM). Anthropic remains the default and needs no new configuration.
- `--dry-run` cost estimates are provider-aware: exact token counts on Anthropic, clearly-labeled approximate counts elsewhere; dollar costs appear only when pricing is known (built-in for the default model, or via `LLM_INPUT_COST_PER_MTOK` / `LLM_OUTPUT_COST_PER_MTOK`) — never a fabricated number.
- `puxti health` validates the configured provider (free auth check per provider) and reports incomplete provider config with an actionable message; `puxti config` shows the provider, model, and masked key.

### Changed

- All LLM access now goes through an internal provider backend (`puxti.llm.LLMBackend`); engines no longer touch the Anthropic SDK directly. No behavior change — groundwork for provider-agnostic BYOK. Auth and billing errors are normalized (`LLMAuthError` / `LLMBillingError`), so scan and capture now get the same actionable credit-balance message redefine already had.

---

## [0.9.0] — 2026-07-13

### Changed

- **Confirmation prompts now require an explicit answer — blank input never accepts.** Previously scan's "Confirm?" / "Confirm all definitions?" / "Confirm all edges?" treated Enter as yes, and correct's edge re-assessment applied the LLM suggestion on blank or unrecognized input. Blank now skips/cancels (scan) or keeps the edge unchanged (correct); only `y` confirms. Prompts that already required an explicit `y` or a typed `yes` are unchanged.

- **`cli.py` split into a `puxti/cli/` package** — one module per command, `_app.py` for the Typer apps, `_shared.py` for the console/runner helpers. No behavior change: the `puxti.cli:app` entry point and all `--help` output are byte-identical. `tests/test_cli.py` split into `tests/cli/` to mirror the layout.

- **`puxti scan` runs LLM calls in parallel.** Definition generation (auto mode), semantic-edge batches, and `--dry-run` token counting now run with bounded concurrency (default 4, tunable via the `LLM_CONCURRENCY` env var). Interactive mode stays sequential — each call is gated on user confirmation. The first API error cancels in-flight calls and propagates; no silent partial results.
- Scan progress is now a live counter ("Generating definitions... 12/48") instead of a static spinner.

### Fixed

- `.env.example` no longer opens with a Neo4j block referencing `docker-compose.yml` — both were removed in 0.7.0.

---

## [0.8.1] — 2026-07-13

### Fixed

- **`puxti link` edges are now discoverable by `puxti capture`.** Link previously stored entities under random UUIDs, so the FEEDS edge could never be found by `get_feeds_producers()` and the documented scan → link → capture cross-system flow silently did nothing. Entities are now keyed by their canonical string ID, and link reuses entities that scan already registered instead of duplicating them.
- **Airflow PR file paths.** Capture no longer applies the dbt `repo_subdir` prefix to Airflow diffs, and Airflow diff paths are now rebased onto the airflow repo root (`<repo_subdir>/<dags_dir>/<file>`), so the PR updates `dags/my_dag.py` instead of creating a new file at the repo root.
- **`puxti redefine` no longer swallows API errors.** An invalid API key, exhausted credits, or a rate limit used to surface as "No diffs generated"; API errors now propagate with an actionable message, and only unparseable LLM responses are skipped (with a logged warning).
- The "Update available" notice is printed to stderr so it can no longer corrupt `puxti impact --json` output consumed by pipes.
- Concurrent writes to `~/.puxti/config.toml` (update check vs telemetry threads) are now serialized and atomic, preventing a clobbered install ID or opt-in setting.
- `capture --dry-run` now builds the same prompt as the real run (including the known-entity-ID list), and `scan --dry-run` counts partial edge batches, so both cost estimates match what is actually billed.
- `DBT_PROJECT_DIR` no longer defaults to `./dbt`, so a missing configuration produces the intended "not configured" error instead of "manifest not found at dbt/target/manifest.json".
- GitHub branch lookup uses the exact-match `git/ref/heads/{branch}` endpoint; the previous prefix-matching endpoint could crash when another branch shared the base branch's name as a prefix.
- `puxti purge --project` now also removes change events and correction events referencing the purged entities, matching `--all`.

### Changed

- LLM model ID and pricing are centralized in `puxti.llm` (previously duplicated across five modules).

---

## [0.8.0] — 2026-05-24

_Backfilled: this release shipped without a changelog entry._

### Added

- **`puxti impact`** — query what depends on an entity and what breaks, straight from the Knowledge Graph, with `--json` output for pipelines.
- **MCP server** (`puxti mcp serve`, stdio transport) exposing four read-only tools to MCP-compatible agents: `impact_of_change`, `consumers`, `definition_history`, `describe_entity`.
- **Opt-in anonymous telemetry** (`puxti telemetry on|off|show`) — command name, version, duration, and exit status only; disabled by default. See `TELEMETRY.md`.

### Changed

- Internal docs excluded from the sdist.

---

## [0.7.0] — 2026-05-10

### Changed

- **Knowledge Graph backend replaced: Neo4j → SQLite.** The graph now lives in a local file at `~/.puxti/graph.db`. No Docker, no Neo4j, no external services required.
- `neo4j` dependency removed; `aiosqlite` added.
- `NEO4J_URI`, `NEO4J_USERNAME`, `NEO4J_PASSWORD` environment variables removed — no longer needed.
- `puxti health` now shows the graph file path and whether it has been initialised.
- `puxti config` now shows the graph file path instead of Neo4j connection details.
- `docker-compose.yml` removed from the repository.
- README updated: removed Docker requirement, Neo4j setup section, and Neo4j env vars.

### Removed

- `puxti.graph` package (`WorkspaceGraph`, `GraphDriver`) — internal multi-tenant API-era code, not part of the public interface.

---

## [0.6.0] — 2026-05-09

First open-source release under Apache 2.0.

### Removed

- Closed-beta backend (FastAPI service, license key validation, trial tracking, usage recording). Puxti is now fully self-contained.
- `puxti auth` command and all subcommands. No authentication required.
- Trial limits (30 days / 10 propagations / 25 captures). No limits.

### Changed

- License changed to Apache 2.0.
- README updated to reflect open-source positioning.
- `puxti config` and `puxti health` no longer reference the backend.
- Dependencies trimmed: removed fastapi, uvicorn, slowapi, sqlalchemy, asyncpg, resend, email-validator, httpx.

### Notes

- Existing design partners on closed-beta versions: upgrade to 0.6.0 to drop the license key requirement. Your workflow is otherwise unchanged.
- Telemetry: none in this release. Anonymous opt-in analytics will arrive in a future version with clear documentation.
