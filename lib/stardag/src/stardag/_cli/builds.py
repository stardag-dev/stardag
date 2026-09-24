"""Build inspection commands for the Stardag CLI (registry v2).

    stardag builds show <build-id>       # status, roots, reactive meta
    stardag builds frontier <build-id>   # the active plan: discovery jobs / runnable / running
    stardag builds ticks <build-id>      # what each scheduler tick decided, and why
    stardag builds cancel <build-id>     # release the build's claims; stop nothing

``frontier`` is what a reactive scheduler tick sees when it decides whether
the build can progress, after the registry's closure step over the build's
active plan: members to expand (discovery jobs), members to claim
(runnable), members under a live claim (running).

Machine-readable output: the read-only commands take ``--json``; stdout then
carries the JSON document and nothing else.

``builds list``, ``builds stop`` and ``builds cleanup`` (and the ``tasks`` /
``concurrency-limits`` groups) wait for their v2 read and stop routes; they
are rebuilt in work package I8.
"""

import json
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

import typer
from rich.table import Table

# Shared by every registry-backed CLI group; imported into this module's
# namespace so ``stardag._cli.builds._resolve_registry`` is the patch point.
from stardag._cli._registry_ctx import (
    _ENV_OPTION,
    _PROFILE_OPTION,
    _fail,
    _resolve_registry,
    console,
    error_console,
)
from stardag.exceptions import StardagError
from stardag.registry import BuildInfo, FrontierMember

app = typer.Typer(
    help="Inspect and cancel builds in an environment",
    no_args_is_help=True,
)

_JSON_OPTION = typer.Option(
    False,
    "--json",
    help="Emit the API payload as JSON on stdout (nothing else goes to stdout).",
)


def _emit_json(payload: Any) -> None:
    """Write one JSON document to stdout, and nothing else."""
    typer.echo(json.dumps(payload, indent=2, default=str))


def _stamp(value: datetime | None) -> str:
    if value is None:
        return "-"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.strftime("%Y-%m-%d %H:%M:%SZ")


def _parse_build_id(value: str) -> UUID:
    """Parse a build id argument, failing with a CLI error rather than a
    stack trace."""
    try:
        return UUID(value)
    except ValueError:
        error_console.print(
            f"[bold red]Error:[/bold red] {value!r} is not a valid build ID (UUID)."
        )
        raise typer.Exit(1)


@app.command("show")
def builds_show(
    build_id: str = typer.Argument(..., help="Build ID"),
    stardag_profile: Optional[str] = _PROFILE_OPTION,
    stardag_env: Optional[str] = _ENV_OPTION,
    json_output: bool = _JSON_OPTION,
) -> None:
    """Show one build: status, roots and reactive metadata."""
    parsed = _parse_build_id(build_id)
    registry = _resolve_registry(stardag_profile, stardag_env)
    try:
        build = registry.build_get(parsed)
    except StardagError as e:
        _fail(e)
    finally:
        registry.close()
    if json_output:
        _emit_json(build.model_dump(mode="json"))
        return
    _render_build(build)


def _render_build(build: BuildInfo) -> None:
    table = Table(title=f"Build {build.id}", show_header=False)
    table.add_column("Field", style="bold")
    table.add_column("Value")
    table.add_row("Name", build.name or "-")
    table.add_row("Status", build.status or "-")
    if build.is_resumed:
        table.add_row("Resumed", "yes")
    table.add_row("Description", build.description or "-")
    table.add_row("Created", _stamp(build.created_at))
    table.add_row("Started", _stamp(build.started_at))
    table.add_row("Completed", _stamp(build.completed_at))
    table.add_row(
        "Reactive app", build.reactive_app_name or "- (not reactively scheduled)"
    )
    if build.reactive_tick_kwargs:
        table.add_row(
            "Reactive tick config",
            json.dumps(build.reactive_tick_kwargs, sort_keys=True),
        )
    table.add_row("Roots", str(len(build.root_task_ids)))
    console.print(table)
    if build.root_task_ids:
        roots = Table(title="Root tasks")
        roots.add_column("Task ID")
        for task_id in build.root_task_ids:
            roots.add_row(task_id)
        console.print(roots)


def _render_members(title: str, members: list[FrontierMember]) -> None:
    if not members:
        return
    table = Table(title=title)
    table.add_column("Task ID")
    table.add_column("Task")
    table.add_column("Status")
    table.add_column("Root")
    for member in members:
        label = ".".join(
            str(part)
            for part in (
                member.body.get("__namespace"),
                member.body.get("__name"),
            )
            if part
        )
        table.add_row(
            member.task_id, label or "-", member.status, "yes" if member.is_root else ""
        )
    console.print(table)


@app.command("frontier")
def builds_frontier(
    build_id: str = typer.Argument(..., help="Build ID"),
    stardag_profile: Optional[str] = _PROFILE_OPTION,
    stardag_env: Optional[str] = _ENV_OPTION,
    json_output: bool = _JSON_OPTION,
) -> None:
    """Show a build's active plan as a scheduler tick sees it."""
    parsed = _parse_build_id(build_id)
    registry = _resolve_registry(stardag_profile, stardag_env)
    try:
        frontier = registry.build_get_frontier(parsed)
    except StardagError as e:
        _fail(e)
    finally:
        registry.close()
    if json_output:
        _emit_json(frontier.model_dump(mode="json"))
        return
    summary = Table(title=f"Frontier of build {frontier.build_id}", show_header=False)
    summary.add_column("Field", style="bold")
    summary.add_column("Value")
    summary.add_row("Build status", frontier.build_status or "-")
    summary.add_row(
        "Reactive app", frontier.reactive_app_name or "- (not reactively scheduled)"
    )
    summary.add_row("Plan", str(frontier.plan_id) if frontier.plan_id else "- (none)")
    summary.add_row(
        "Deployment", str(frontier.deployment_id) if frontier.deployment_id else "-"
    )
    summary.add_row("Settings", frontier.settings_hash or "-")
    summary.add_row("Sealed", "yes" if frontier.sealed else "no")
    summary.add_row("Plan complete", "yes" if frontier.plan_complete else "no")
    summary.add_row("Discovery jobs", str(len(frontier.discovery_jobs)))
    summary.add_row("Runnable", str(len(frontier.runnable)))
    summary.add_row("Running", str(len(frontier.running)))
    console.print(summary)
    _render_members("Discovery jobs", frontier.discovery_jobs)
    _render_members("Runnable", frontier.runnable)
    _render_members("Running", frontier.running)
    if frontier.closure is not None and frontier.closure.conflicts:
        conflicts = ", ".join(c.task_id for c in frontier.closure.conflicts)
        console.print(f"[bold red]Closure conflicts:[/bold red] {conflicts}")


@app.command("ticks")
def builds_ticks(
    build_id: str = typer.Argument(..., help="Build ID"),
    stardag_profile: Optional[str] = _PROFILE_OPTION,
    stardag_env: Optional[str] = _ENV_OPTION,
    limit: int = typer.Option(
        20, "--limit", "-n", min=1, max=200, help="Summaries to show (newest first)."
    ),
    json_output: bool = _JSON_OPTION,
) -> None:
    """Show the reactive scheduler's own account of its recent ticks."""
    parsed = _parse_build_id(build_id)
    registry = _resolve_registry(stardag_profile, stardag_env)
    try:
        summaries = registry.build_list_tick_summaries(parsed, limit=limit)
    except StardagError as e:
        _fail(e)
    finally:
        registry.close()
    if json_output:
        _emit_json({"summaries": [s.model_dump(mode="json") for s in summaries]})
        return
    if not summaries:
        console.print(f"No tick summaries recorded for build {build_id}.")
        console.print("\n[dim]Only reactively-scheduled builds report them.[/dim]")
        return
    table = Table(title=f"Tick summaries for build {build_id} (newest first)")
    table.add_column("When")
    table.add_column("Outcome")
    table.add_column("Detail")
    for record in summaries:
        detail = ", ".join(
            f"{k}={v}"
            for k, v in sorted(record.summary.items())
            if k != "outcome" and v not in (0, None)
        )
        table.add_row(_stamp(record.created_at), record.outcome, detail or "-")
    console.print(table)


@app.command("cancel")
def builds_cancel(
    build_id: str = typer.Argument(..., help="Build ID"),
    stardag_profile: Optional[str] = _PROFILE_OPTION,
    stardag_env: Optional[str] = _ENV_OPTION,
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip the confirmation prompt."
    ),
) -> None:
    """Cancel a build: release the claims its plans hold, and stop there.

    **Nothing is stopped.** The released tasks are CANCELLED — actionable,
    so any other build holding them runs them — and a worker still running
    exits at its next cooperative checkpoint, or runs to completion if its
    ``run()`` has none; its report is recorded as late.
    """
    parsed = _parse_build_id(build_id)
    if not yes:
        typer.confirm(f"Cancel build {build_id}?", abort=True)
    registry = _resolve_registry(stardag_profile, stardag_env)
    try:
        registry.build_cancel(parsed)
    except StardagError as e:
        _fail(e)
    finally:
        registry.close()
    console.print(f"[green]Cancelled build[/green] {build_id}")
    console.print(
        "[dim]Its claims were released, so its tasks are available to the next "
        "build now. Nothing was stopped: a worker still running exits at its "
        "next cooperative checkpoint, or runs to completion.[/dim]"
    )
