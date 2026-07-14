"""`puxti telemetry` — manage anonymous usage telemetry."""

from puxti.cli._app import telemetry_app
from puxti.cli._shared import console


@telemetry_app.command("on")
def telemetry_on() -> None:
    """Enable anonymous usage telemetry."""
    from puxti.telemetry import get_install_id, set_enabled
    set_enabled(True)
    install_id = get_install_id()
    console.print("[green]✓[/green] Telemetry enabled.")
    console.print(f"  Install ID:  [dim]{install_id}[/dim]")
    console.print("  Events sent: command name, version, duration, exit status — nothing else.")
    console.print("  See [bold]TELEMETRY.md[/bold] or run [bold]puxti telemetry show[/bold] for details.")


@telemetry_app.command("off")
def telemetry_off() -> None:
    """Disable anonymous usage telemetry."""
    from puxti.telemetry import set_enabled
    set_enabled(False)
    console.print("[green]✓[/green] Telemetry disabled. No events will be sent.")


@telemetry_app.command("show")
def telemetry_show() -> None:
    """Show current telemetry state and install ID."""
    from puxti.telemetry import get_install_id, is_enabled
    enabled = is_enabled()
    status = "[green]enabled[/green]" if enabled else "[dim]disabled (default)[/dim]"
    console.print(f"[bold]Telemetry:[/bold]  {status}")
    if enabled:
        install_id = get_install_id()
        console.print(f"[bold]Install ID:[/bold] [dim]{install_id}[/dim]")
        console.print("\nWhat is sent per command: name, version, duration_ms, exit_status, python_version, platform.")
        console.print("Nothing from your dbt project, graph, or environment is ever sent.")
        console.print("See [bold]TELEMETRY.md[/bold] for the full event schema.")
    else:
        console.print("\nRun [bold]puxti telemetry on[/bold] to opt in.")
