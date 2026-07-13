"""Anonymous usage telemetry for puxti.

Telemetry is opt-in and disabled by default. Enable it with:
    puxti telemetry on

See TELEMETRY.md for the full list of events and fields collected.
"""
from __future__ import annotations

import sys
import threading
import tomllib
import uuid
from pathlib import Path

from puxti import __version__

POSTHOG_API_KEY = "phc_zv5zvpc5x6ncnKYB98HHCeWj9hmUVKtEVBKfvPGKXbyP"
POSTHOG_HOST = "https://eu.i.posthog.com"

_CONFIG_PATH = Path.home() / ".puxti" / "config.toml"

# Serialises read-modify-write cycles on the config file. The update-check
# thread (cli._check_for_update) and the telemetry thread can both mutate
# ~/.puxti/config.toml concurrently; without this an install ID or opt-in
# choice can be silently clobbered by a lost update.
_config_lock = threading.Lock()


def _load_config() -> dict:
    if not _CONFIG_PATH.exists():
        return {}
    try:
        with open(_CONFIG_PATH, "rb") as f:
            return tomllib.load(f)
    except Exception:
        return {}


def _save_config(data: dict) -> None:
    """Write the config atomically so a concurrent reader never sees a partial file."""
    import tomli_w

    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _CONFIG_PATH.with_suffix(".toml.tmp")
    with open(tmp, "wb") as f:
        tomli_w.dump(data, f)
    tmp.replace(_CONFIG_PATH)


def get_install_id() -> str:
    """Return the anonymous install ID, creating and persisting it if absent."""
    with _config_lock:
        data = _load_config()
        existing = data.get("telemetry", {}).get("install_id")
        if existing:
            return existing
        new_id = str(uuid.uuid4())
        data.setdefault("telemetry", {})["install_id"] = new_id
        _save_config(data)
        return new_id


def is_enabled() -> bool:
    """Return True only if the user has explicitly opted in."""
    return bool(_load_config().get("telemetry", {}).get("enabled", False))


def set_enabled(value: bool) -> None:
    """Persist the opt-in / opt-out choice. Creates an install ID on first opt-in."""
    with _config_lock:
        data = _load_config()
        data.setdefault("telemetry", {})["enabled"] = value
        if value:
            data["telemetry"].setdefault("install_id", str(uuid.uuid4()))
        _save_config(data)


def record_event(command: str, duration_ms: int, exit_status: int) -> threading.Thread | None:
    """Fire a command_run event to PostHog in a background thread.

    Returns the thread so the caller can join with a timeout. Silently drops the
    event on any error — telemetry must never break the CLI.
    """
    if not is_enabled():
        return None

    install_id = get_install_id()

    def _send() -> None:
        try:
            import posthog

            posthog.api_key = POSTHOG_API_KEY
            posthog.host = POSTHOG_HOST
            posthog.capture(
                distinct_id=install_id,
                event="command_run",
                properties={
                    "command": command,
                    "version": __version__,
                    "duration_ms": duration_ms,
                    "exit_status": exit_status,
                    "python_version": sys.version.split()[0],
                    "platform": sys.platform,
                },
            )
            posthog.flush()
        except Exception:
            pass

    thread = threading.Thread(target=_send, daemon=True)
    thread.start()
    return thread
