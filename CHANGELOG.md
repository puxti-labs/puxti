# Changelog

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
