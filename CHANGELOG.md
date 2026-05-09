# Changelog

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
