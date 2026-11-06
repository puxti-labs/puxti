"""`puxti scan` — bootstrap the Knowledge Graph from the configured producers."""

import hashlib
import json
import logging
from typing import Optional

import typer
from rich.panel import Panel

from puxti import __version__
from puxti.cli._app import app
from puxti.cli._shared import _load_workspace, _run, console, err_console
from puxti.connectors.base import BaseConnector
from puxti.connectors.registry import build_configured_connectors
from puxti.core.graph import GraphStore, KnowledgeGraph
from puxti.core.resolution import build_reference_index
from puxti.core.scanner import SemanticScanner
from puxti.llm import COST_UNKNOWN_HINT
from puxti.models import EntityType, ScanRun, SourceFingerprint
from puxti.settings import settings

#: Exit code for `scan --if-changed --dry-run` when a rescan is needed, so CI
#: can tell "stale" from "up to date" (0) and from an error (1).
EXIT_RESCAN_NEEDED = 3

logger = logging.getLogger(__name__)


@app.command()
def scan(
    dbt_project_dir: Optional[str] = typer.Option(
        None, "--dbt-project-dir",
        help="Path to dbt project root (overrides .puxti.yml and DBT_PROJECT_DIR)",
    ),
    interactive: bool = typer.Option(
        False, "--interactive", "-i",
        help="Confirm each definition one by one (slower, higher accuracy).",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Estimate token count and cost without running the scan."
    ),
    if_changed: bool = typer.Option(
        False, "--if-changed",
        help="Skip the scan (exit 0) when no connector's inputs changed since the last "
        "completed scan. With --dry-run, exit 3 when a rescan is needed.",
    ),
) -> None:
    """Populate the Knowledge Graph so puxti can propagate changes safely.

    Scans every producer connector configured in .puxti.yml (dbt, prisma,
    sql_views — plus --dbt-project-dir/DBT_PROJECT_DIR for dbt), extracts all
    entities and lineage, infers starter definitions via LLM, and proposes
    semantic edges. Table references that cross connectors (e.g. a SQL view
    reading a Prisma-managed table) are resolved into lineage edges.

    dbt MetricFlow metrics become metric entities (metric.<project>.<name>)
    whose definitions come from their YAML, no LLM; a definition someone wrote
    is never overwritten.

    Two modes:
    - Default (auto): generates all definitions in one pass, shows a summary,
      you confirm everything at once before anything is written.
    - Interactive (--interactive): walks through each model one by one,
      you confirm or edit each definition before moving to the next.

    --if-changed compares a content hash of every connector's inputs (dbt
    manifest and model SQL, schema.prisma, SQL view files, DAG files) with the
    last completed scan, and skips when all of them match. Use it from a git
    hook or CI.

    Nothing is written to the Knowledge Graph without your explicit confirmation.
    Run this before using `puxti redefine`.
    """
    ws = _load_workspace()
    resolved_project_dir = dbt_project_dir or (ws.dbt.project_dir if ws.dbt else None)
    _run(
        _run_scan(
            dbt_project_dir=resolved_project_dir, interactive=interactive, dry_run=dry_run,
            if_changed=if_changed,
        ),
        command="scan",
    )


def _fingerprint(
    connector: BaseConnector, connectors: list[BaseConnector]
) -> SourceFingerprint | None:
    """A connector's input fingerprint combined with its config and the puxti
    version, since a settings change (a SQL dialect, a default schema) or an
    upgrade can change what extraction produces from the same files. None
    when the connector cannot produce one; errors mean "cannot tell", which
    always rescans."""
    try:
        fp = connector.source_fingerprint()
    except Exception as exc:
        logger.warning("Cannot fingerprint %s inputs, will rescan: %s", connector.name, exc)
        return None
    if fp is None:
        return None
    # The full connector set is part of every key: cross-connector references
    # resolve only when connectors are scanned together, so scanning Prisma and
    # SQL views separately and then together must not look unchanged.
    settings_key = json.dumps(
        {
            "config": connector.config,
            "puxti": __version__,
            "connectors": sorted(
                json.dumps([c.name, c.config], sort_keys=True, default=str) for c in connectors
            ),
        },
        sort_keys=True, default=str,
    )
    digest = hashlib.sha256(f"{fp.digest}\0{settings_key}".encode()).hexdigest()
    return SourceFingerprint(source=fp.source, digest=digest)


async def _rescan_reasons(
    graph: GraphStore, fingerprints: list[tuple[BaseConnector, SourceFingerprint | None]]
) -> list[str]:
    """Why a rescan is needed; empty means every connector is unchanged since
    its last completed scan. An empty graph always needs a scan."""
    if not await graph.has_entities():
        return ["the Knowledge Graph is empty"]
    reasons: list[str] = []
    for connector, fp in fingerprints:
        if fp is None:
            reasons.append(f"{connector.name}: inputs cannot be fingerprinted")
            continue
        last = await graph.get_last_scan_fingerprint(connector.name, fp.source)
        if last is None:
            reasons.append(f"{connector.name}: never scanned from {fp.source}")
        elif last != fp.digest:
            reasons.append(f"{connector.name}: changed since the last scan")
    return reasons


async def _report_staleness(
    fingerprints: list[tuple[BaseConnector, SourceFingerprint | None]],
    read_only: bool,
) -> bool:
    """Print whether a rescan is needed and return True when it is. With
    `read_only`, the graph is inspected without creating it or its schema."""
    graph = KnowledgeGraph()
    try:
        await graph.connect(read_only=read_only)
        reasons = await _rescan_reasons(graph, fingerprints)
    finally:
        await graph.close()
    if not reasons:
        console.print(
            "[green]✓[/green] No changes since the last scan "
            f"({', '.join(c.name for c, _ in fingerprints)}). Skipping."
        )
        return False
    console.print("[bold]Rescan needed:[/bold]")
    for reason in reasons:
        console.print(f"  • {reason}", markup=False)
    return True


def _project_name(connector: BaseConnector) -> str:
    try:
        return connector.get_project_name()
    except Exception as exc:
        logger.warning("Cannot read %s project name for its scan run: %s", connector.name, exc)
        return ""


async def _record_scan_runs(
    graph: GraphStore,
    fingerprints: list[tuple[BaseConnector, SourceFingerprint | None]],
    incomplete: set[str],
) -> None:
    for connector, fp in fingerprints:
        if fp is not None and connector.name not in incomplete:
            await graph.record_scan_run(ScanRun(
                connector=connector.name, source=fp.source,
                project=_project_name(connector), fingerprint=fp.digest,
            ))


async def _run_scan(
    dbt_project_dir: str | None, interactive: bool, dry_run: bool = False,
    if_changed: bool = False,
) -> None:
    project_dir = dbt_project_dir or settings.dbt_project_dir
    ws = _load_workspace()
    connectors = build_configured_connectors(ws, dbt_project_dir=project_dir)

    if not connectors:
        err_console.print(
            "[red]Error:[/red] no producer connectors configured. "
            "Pass --dbt-project-dir, set DBT_PROJECT_DIR, or configure "
            "connectors in .puxti.yml."
        )
        raise typer.Exit(1)

    # Fingerprinted before scanning, so an edit made mid-scan reads as a change
    # on the next --if-changed run instead of being recorded as scanned.
    # A plain dry run neither checks nor records, so it skips the hashing.
    fingerprints = (
        [(c, _fingerprint(c, connectors)) for c in connectors]
        if if_changed or not dry_run else []
    )

    rescan_needed = False
    if if_changed:
        if not await _report_staleness(fingerprints, read_only=dry_run):
            return
        rescan_needed = True

    scanner = SemanticScanner()

    if dry_run:
        total_cost: float | None = 0.0
        for connector in connectors:
            status_text = f"[bold]Reading {connector.name} sources and counting tokens...[/bold]"
            with console.status(status_text):
                estimate = await scanner.estimate_scan_cost(connector)
            approx = "" if estimate["tokens_exact"] else " (approximate)"
            cost_line = (
                f"Est. cost:              ${estimate['estimated_cost_usd']:.4f} USD"
                if estimate["estimated_cost_usd"] is not None
                else f"Est. cost:              {COST_UNKNOWN_HINT}"
            )
            if estimate["estimated_cost_usd"] is None:
                total_cost = None
            elif total_cost is not None:
                total_cost += estimate["estimated_cost_usd"]
            console.print(
                Panel(
                    f"Models to define:       {estimate['models']}\n"
                    f"Total entities:         {estimate['entities_total']}\n"
                    f"\n"
                    f"Definition calls\n"
                    f"  Input tokens:         {estimate['def_input_tokens']:,}\n"
                    f"  Est. output tokens:   {estimate['def_est_output_tokens']:,}\n"
                    f"\n"
                    f"Edge proposal call\n"
                    f"  Input tokens:         {estimate['edges_input_tokens']:,}\n"
                    f"  Est. output tokens:   {estimate['edges_est_output_tokens']:,}\n"
                    f"\n"
                    f"Total input tokens:     {estimate['total_input_tokens']:,}{approx}\n"
                    f"Total est. output:      {estimate['total_est_output_tokens']:,}\n"
                    f"{cost_line}",
                    title=f"[bold]Dry run — {connector.name} cost estimate[/bold]",
                    border_style="yellow",
                )
            )
        if len(connectors) > 1 and total_cost is not None:
            console.print(f"[bold]Total est. cost:[/bold] ${total_cost:.4f} USD")
        if rescan_needed:
            raise typer.Exit(EXIT_RESCAN_NEEDED)
        return

    # Cross-connector reference resolution — built from every producer's
    # entities so `sqlref.` placeholder edges resolve at write time.
    reference_index = None
    if len(connectors) > 1:
        all_entities = []
        for connector in connectors:
            all_entities.extend(await connector.extract_entities())
        reference_index = build_reference_index(all_entities)

    graph = KnowledgeGraph()
    try:
        await graph.connect()
        # Before anything is written: drop the runs of every connector about to
        # scan, from this checkout and any other of the same project. A scan
        # that fails or comes back incomplete then leaves nothing to skip on.
        for connector in connectors:
            await graph.invalidate_scan_runs(connector.name, _project_name(connector))
        incomplete = await _scan_connectors(
            scanner, connectors, graph, interactive, reference_index
        )

        # Reconcile proposed metrics once, after every connector — a proposed
        # metric can bind to an entity produced by any of them.
        await _reconcile_proposed(graph, interactive)

        # Every connector scanned without error: record what was scanned, so
        # the next --if-changed run can skip. Recorded even when definitions
        # were declined, since entities and lineage were written regardless.
        # A connector whose edge proposals were truncated or skipped is not
        # recorded: the scanner just asked for a re-run, which --if-changed must
        # not skip.
        await _record_scan_runs(graph, fingerprints, incomplete)
    finally:
        await graph.close()


async def _scan_connectors(
    scanner: SemanticScanner,
    connectors: list[BaseConnector],
    graph: GraphStore,
    interactive: bool,
    reference_index: object,
) -> set[str]:
    """Scan each connector in turn; return the names of those whose edge
    proposals were incomplete (truncated by the token limit, or a batch skipped
    for an unusable response)."""
    mode = "interactive" if interactive else "auto"
    incomplete: set[str] = set()
    for connector in connectors:
        console.print(f"\n[bold]Scanning {connector.name}[/bold] ({mode} mode)")
        result = await scanner.scan(
            connector, graph, interactive=interactive, console=console,
            reference_index=reference_index,
        )
        console.print(
            f"\n[green]✓[/green] {connector.name} scan complete: "
            f"{result.entities_upserted} entities, "
            f"{result.definitions_written} definitions, "
            f"{result.semantic_edges_written} semantic edges written."
        )
        if result.edge_batches_truncated or result.edge_batches_failed:
            incomplete.add(connector.name)
    return incomplete


def _build_name_index(entities: list) -> dict[str, str]:
    """Lowercase bare name → entity ID, dropping names claimed by more than one
    entity. Mirrors core.resolution.build_reference_index's ambiguous-drop rule
    ("dangling beats silently wrong"); kept separate because that function reads
    entity.metadata, which the graph does not persist."""
    _AMBIGUOUS = object()
    index: dict[str, object] = {}
    for e in entities:
        key = e.name.lower()
        existing = index.get(key)
        if existing is None:
            index[key] = e.id
        elif existing != e.id:
            index[key] = _AMBIGUOUS
    return {k: v for k, v in index.items() if v is not _AMBIGUOUS}


async def _reconcile_proposed(graph: GraphStore, interactive: bool) -> None:
    """Offer to bind proposed metrics to scanned entities with a matching name.

    Never binds without confirmation. Exact (case-insensitive) name matches only;
    anything scan cannot match stays unbound and is bound explicitly with
    `puxti bind`.

    A scanned metric with the proposed metric's name wins over a model, view or
    table with the same name (jaffle-shop has both an `orders` metric and an
    `orders` model). Names shared by two metrics stay unmatched."""
    proposed = await graph.get_proposed_entities()
    if not proposed:
        return

    candidates = await graph.get_reconcile_candidates()
    metrics = [e for e in candidates if e.type == EntityType.METRIC]
    metric_names = {e.name.lower() for e in metrics}
    metric_index = _build_name_index(metrics)
    other_index = _build_name_index([e for e in candidates if e.type != EntityType.METRIC])

    matches = []
    for p in proposed:
        key = p.name.lower()
        index = metric_index if key in metric_names else other_index
        if key in index:
            matches.append((p, index[key]))

    if not matches:
        console.print(
            f"\n[dim]{len(proposed)} proposed metric(s) still unbound: no scanned entity "
            f"matches by name. Bind explicitly with `puxti bind --proposed <id> --to <id>`.[/dim]"
        )
        return

    console.print(f"\n[bold]Proposed metrics matched to scanned entities ({len(matches)}):[/bold]")
    for p, target_id in matches:
        console.print(f"  [cyan]{p.name}[/cyan] [dim]({p.id})[/dim] → [cyan]{target_id}[/cyan]")

    confirmed: list[tuple] = []
    if interactive:
        for p, target_id in matches:
            choice = console.input(
                f"  [bold]Bind[/bold] {p.name} → {target_id}? "
                "([green]y[/green]=yes, [red]n[/red]=skip) > "
            ).strip().lower()
            if choice == "y":
                confirmed.append((p, target_id))
    else:
        choice = console.input(
            "\n[bold]Bind all?[/bold] ([green]y[/green]=yes, [red]n[/red]=cancel) > "
        ).strip().lower()
        if choice == "y":
            confirmed = matches

    for p, target_id in confirmed:
        await graph.bind_entity(p.id, target_id)

    bound = len(confirmed)
    remaining = len(proposed) - bound
    console.print(f"[green]✓[/green] Bound {bound} proposed metric(s).")
    if remaining:
        console.print(
            f"[dim]{remaining} still unbound. Bind explicitly with "
            f"`puxti bind --proposed <id> --to <id>`.[/dim]"
        )
