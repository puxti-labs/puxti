"""Shared CLI runtime: consoles, async runner, update check, workspace loading."""

import asyncio
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone

import typer
from rich.console import Console

from puxti import __version__
from puxti.llm import LLMConfigError
from puxti.models import EntityType
from puxti.workspace import WorkspaceConfig, load_workspace

console = Console()
err_console = Console(stderr=True)

_UPDATE_CHECK_INTERVAL = timedelta(hours=24)
_PYPI_URL = "https://pypi.org/pypi/puxti/json"
_update_notice: list[str] = []  # populated by background thread, read after command


def _check_for_update() -> None:
    """Fetch latest version from PyPI and queue a notice if newer than installed."""
    try:
        import json

        from puxti.telemetry import _config_lock, _load_config, _save_config

        last_checked_str = _load_config().get("update", {}).get("last_checked")
        if last_checked_str:
            last_checked = datetime.fromisoformat(last_checked_str)
            if datetime.now(timezone.utc) - last_checked < _UPDATE_CHECK_INTERVAL:
                return

        with urllib.request.urlopen(_PYPI_URL, timeout=3) as resp:
            latest = json.loads(resp.read())["info"]["version"]

        # Record check time. Re-read under the config lock so a concurrent
        # telemetry write (e.g. install_id creation) is not clobbered.
        with _config_lock:
            data = _load_config()
            data.setdefault("update", {})["last_checked"] = datetime.now(timezone.utc).isoformat()
            _save_config(data)

        if __version__ == "dev" or latest == __version__:
            return

        def _parse(v: str) -> tuple:
            try:
                return tuple(int(x) for x in v.split("."))
            except ValueError:
                return (0,)

        if _parse(latest) > _parse(__version__):
            _update_notice.append(latest)

    except Exception:
        pass  # never break the CLI over an update check


def _run(coro, *, command: str = "") -> None:
    """Run an async command, catching unexpected errors with actionable guidance."""
    updater = threading.Thread(target=_check_for_update, daemon=True)
    updater.start()
    start = time.monotonic()
    exit_status = 0
    tel_thread: threading.Thread | None = None
    try:
        asyncio.run(coro)
    except typer.Exit as exc:
        exit_status = getattr(exc, "exit_code", None) or getattr(exc, "code", 0) or 0
        raise
    except KeyboardInterrupt:
        exit_status = 130
        raise
    except LLMConfigError as exc:
        # Incomplete provider config is a user-fixable error, not a bug —
        # print the actionable message without the bug-report boilerplate.
        exit_status = 1
        err_console.print(f"[red bold]Config error:[/red bold] {exc}")
        raise typer.Exit(1)
    except Exception as exc:
        exit_status = 1
        err_console.print(f"\n[red bold]Unexpected error:[/red bold] {exc}")
        err_console.print(
            f"\n[yellow]Something went wrong. To report this bug:[/yellow]\n"
            f"  1. Copy the full error above and the traceback below\n"
            f"  2. Note the exact command you ran\n"
            f"  3. Email [bold]puxti@okolico.com[/bold] with:\n"
            f"     - puxti version (shown below)\n"
            f"     - the exact command you ran\n"
            f"     - the error message and traceback\n"
            f"\n[dim]puxti {__version__}[/dim]\n"
        )
        err_console.print_exception(show_locals=False)
        raise typer.Exit(1)
    finally:
        duration_ms = int((time.monotonic() - start) * 1000)
        if command:
            from puxti.telemetry import record_event as _record_event
            tel_thread = _record_event(command=command, duration_ms=duration_ms, exit_status=exit_status)
        updater.join(timeout=4)
        if tel_thread is not None:
            tel_thread.join(timeout=3)
        if _update_notice:
            # stderr — stdout may carry machine-readable output (e.g. --json)
            err_console.print(
                f"\n[yellow]Update available:[/yellow] {__version__} → {_update_notice[0]}\n"
                f"  Run: [bold]pip install --upgrade puxti[/bold]"
            )


def _load_workspace() -> WorkspaceConfig:
    """Load .puxti.yml walking up from CWD. Exit with error on parse/version failure."""
    try:
        return load_workspace()
    except ValueError as exc:
        err_console.print(f"[red bold]Config error:[/red bold] {exc}")
        raise typer.Exit(1)


def _parse_entity_id(entity_id: str) -> tuple[EntityType, str, str]:
    """Return (EntityType, source_connector, project) from a puxti entity ID.

    Entity IDs follow puxti's own grammar — `<type>.<namespace>.<name>[.<attribute>]`.
    For dbt entities it deliberately coincides with dbt's node naming so manifest
    node IDs are valid puxti IDs as-is; the grammar itself is connector-neutral,
    and each producer connector claims its prefixes here:

      task.airflow.<dag_id>.<task_id>  → TASK,  airflow, dag_id
      source.<project>.<table>         → TABLE, dbt,     project
      model.<project>.<name>           → MODEL, dbt,     project
    """
    parts = entity_id.split(".")
    if parts[0] == "task" and len(parts) >= 4 and parts[1] == "airflow":
        return EntityType.TASK, "airflow", parts[2]
    if parts[0] == "source" and len(parts) >= 3:
        return EntityType.TABLE, "dbt", parts[1]
    if parts[0] == "model" and len(parts) >= 3:
        return EntityType.MODEL, "dbt", parts[1]
    raise ValueError(
        f"Unrecognized entity ID: {entity_id!r}\n"
        "  Expected: task.airflow.<dag>.<task>  or  source.<project>.<table>  or  model.<project>.<name>"
    )
