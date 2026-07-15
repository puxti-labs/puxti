# Changelog

## [Unreleased]

### Added

- `CONTRIBUTING.md` — dev setup, test/lint commands, and PR expectations.

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
