"""``stardag builds``: list, inspect and end builds (registry v2).

    stardag builds list [--status S] [--app A]  # GET /builds
    stardag builds show <build-id>        # the build, its active plan and counts
    stardag builds frontier <build-id>    # discovery jobs / runnable / running
    stardag builds ticks <build-id>       # what each scheduler tick decided
    stardag builds stop <build-id>        # stop unended executions, then cancel
    stardag builds cancel <build-id>      # release the build's claims
    stardag builds complete <build-id>    # mark COMPLETED (plan must be complete)
    stardag builds fail <build-id>        # mark FAILED

``frontier`` is what a reactive scheduler tick sees when it decides whether
the build can progress, after the registry's closure step over the build's
active plan: members to expand (discovery jobs), members to claim
(runnable), members under a live claim (running). ``stop`` lives in
:mod:`stardag._cli.builds_stop`.

Machine-readable output: the read commands take ``--json``; stdout then
carries the JSON document and nothing else.
"""

import json
from typing import Any, Optional

import typer
from rich.table import Table

# Imported into this module's namespace so ``stardag._cli.builds.
# _resolve_registry`` is the patch point for this group.
from stardag._cli._output import (
    JSON_OPTION,
    YES_OPTION,
    emit_json,
    parse_uuid,
    short,
    stamp,
    task_label,
)
from stardag._cli._registry_ctx import (
    _ENV_OPTION,
    _PROFILE_OPTION,
    _fail,
    _resolve_registry,
    console,
    error_console,
)
from stardag._cli.builds_stop import builds_stop
from stardag.exceptions import StardagError
from stardag.registry import BuildFrontier, BuildInfo, FrontierMember

app = typer.Typer(
    help="List, inspect, stop and end builds in an environment.",
    no_args_is_help=True,
)

app.command("stop")(builds_stop)

_BUILD_ID = typer.Argument(..., help="Build ID")


def _parse_build_id(value: str):
    return parse_uuid(value, "build ID")


# -----------------------------------------------------------------------------
# list
# -----------------------------------------------------------------------------


@app.command("list")
def builds_list(
    status: Optional[str] = typer.Option(
        None,
        "--status",
        help="Only builds in this status: pending, running, completed, failed, "
        "cancelled.",
    ),
    app_name: Optional[str] = typer.Option(
        None, "--app", help="Only builds reactively scheduled by this app."
    ),
    limit: int = typer.Option(50, "--limit", "-n", min=1, max=500),
    stardag_profile: Optional[str] = _PROFILE_OPTION,
    stardag_env: Optional[str] = _ENV_OPTION,
    json_output: bool = JSON_OPTION,
) -> None:
    """List builds, most recently active first.

    Reads ``GET /builds``. Writes nothing.
    """
    registry = _resolve_registry(stardag_profile, stardag_env)
    try:
        builds = registry.build_list(
            status=status, reactive_app_name=app_name, limit=limit
        )
    except StardagError as e:
        _fail(e)
    finally:
        registry.close()
    if json_output:
        emit_json({"builds": [b.model_dump(mode="json") for b in builds]})
        return
    if not builds:
        console.print("No builds match.")
        return
    table = Table(title="Builds (most recently active first)")
    for col in ("Build ID", "Name", "Status", "Reactive app", "Roots", "Created"):
        table.add_column(col)
    for b in builds:
        table.add_row(
            str(b.id),
            b.name or "-",
            b.status or "-",
            b.reactive_app_name or "-",
            str(len(b.root_task_ids)),
            stamp(b.created_at),
        )
    console.print(table)


# -----------------------------------------------------------------------------
# show
# -----------------------------------------------------------------------------


def _active_plan_summary(
    frontier: BuildFrontier | None,
    settings: dict[str, str] | None,
    executions: tuple[int, int] | None,
) -> dict[str, Any]:
    if frontier is None:
        return {}
    summary: dict[str, Any] = {
        "plan_id": str(frontier.plan_id) if frontier.plan_id else None,
        "deployment_id": str(frontier.deployment_id)
        if frontier.deployment_id
        else None,
        "settings_hash": frontier.settings_hash,
        "settings": settings,
        "sealed": frontier.sealed,
        "plan_complete": frontier.plan_complete,
        "discovery_jobs": len(frontier.discovery_jobs),
        "runnable": len(frontier.runnable),
        "running": len(frontier.running),
    }
    if executions is not None:
        summary["unended_executions"], summary["orphaned_executions"] = executions
    return summary


@app.command("show")
def builds_show(
    build_id: str = _BUILD_ID,
    stardag_profile: Optional[str] = _PROFILE_OPTION,
    stardag_env: Optional[str] = _ENV_OPTION,
    json_output: bool = JSON_OPTION,
) -> None:
    """Show one build: status, roots, its active plan (deployment, settings)
    and the counts that say where it stands.

    Reads ``GET /builds/{id}``, ``GET /builds/{id}/frontier``,
    ``GET /settings/{hash}`` and ``GET /builds/{id}/executions``. Writes
    nothing (the frontier read runs the registry's closure step, which only
    admits members the plan's edges already imply).
    """
    parsed = _parse_build_id(build_id)
    registry = _resolve_registry(stardag_profile, stardag_env)
    settings: dict[str, str] | None = None
    executions: tuple[int, int] | None = None
    try:
        build = registry.build_get(parsed)
        frontier = registry.build_get_frontier(parsed)
        if frontier.settings_hash:
            try:
                settings = registry.settings_get(frontier.settings_hash).body
            except StardagError:
                settings = None
        if frontier.plan_id is not None:
            unended = registry.build_list_executions(parsed)
            executions = (len(unended), sum(not e.in_current_plan for e in unended))
    except StardagError as e:
        _fail(e)
    finally:
        registry.close()
    plan = _active_plan_summary(frontier, settings, executions)
    if json_output:
        emit_json({**build.model_dump(mode="json"), "active_plan": plan})
        return
    _render_build(build, plan)


def _render_build(build: BuildInfo, plan: dict[str, Any] | None = None) -> None:
    table = Table(title=f"Build {build.id}", show_header=False)
    table.add_column("Field", style="bold")
    table.add_column("Value")
    table.add_row("Name", build.name or "-")
    table.add_row("Status", build.status or "-")
    if build.is_resumed:
        table.add_row("Resumed", "yes")
    table.add_row("Description", build.description or "-")
    table.add_row("Created", stamp(build.created_at))
    table.add_row("Started", stamp(build.started_at))
    table.add_row("Completed", stamp(build.completed_at))
    table.add_row(
        "Reactive app", build.reactive_app_name or "- (not reactively scheduled)"
    )
    if build.reactive_tick_kwargs:
        table.add_row(
            "Reactive tick config",
            json.dumps(build.reactive_tick_kwargs, sort_keys=True),
        )
    table.add_row("Roots", str(len(build.root_task_ids)))
    if plan:
        table.add_row("Active plan", plan["plan_id"] or "- (none yet)")
    if plan and plan["plan_id"]:
        table.add_row("Deployment", plan["deployment_id"] or "-")
        table.add_row("Settings hash", short(plan["settings_hash"], 16))
        if plan["settings"]:
            table.add_row("Settings", json.dumps(plan["settings"], sort_keys=True))
        table.add_row("Sealed", "yes" if plan["sealed"] else "no")
        table.add_row("Plan complete", "yes" if plan["plan_complete"] else "no")
        table.add_row(
            "Outstanding",
            f"{plan['discovery_jobs']} to discover, {plan['runnable']} runnable, "
            f"{plan['running']} running",
        )
        if "unended_executions" in plan:
            table.add_row(
                "Unended executions",
                f"{plan['unended_executions']} "
                f"({plan['orphaned_executions']} not in the active plan)",
            )
    console.print(table)
    if build.root_task_ids:
        roots = Table(title="Root tasks")
        roots.add_column("Task ID")
        for task_id in build.root_task_ids:
            roots.add_row(task_id)
        console.print(roots)


# -----------------------------------------------------------------------------
# frontier / ticks
# -----------------------------------------------------------------------------


def _render_members(title: str, members: list[FrontierMember]) -> None:
    if not members:
        return
    table = Table(title=title)
    table.add_column("Task ID")
    table.add_column("Task")
    table.add_column("Status")
    table.add_column("Root")
    for member in members:
        table.add_row(
            member.task_id,
            task_label(member.body),
            member.status,
            "yes" if member.is_root else "",
        )
    console.print(table)


@app.command("frontier")
def builds_frontier(
    build_id: str = _BUILD_ID,
    stardag_profile: Optional[str] = _PROFILE_OPTION,
    stardag_env: Optional[str] = _ENV_OPTION,
    json_output: bool = JSON_OPTION,
) -> None:
    """Show a build's active plan as a scheduler tick sees it.

    Reads ``GET /builds/{id}/frontier`` (which runs the closure step first).
    """
    parsed = _parse_build_id(build_id)
    registry = _resolve_registry(stardag_profile, stardag_env)
    try:
        frontier = registry.build_get_frontier(parsed)
    except StardagError as e:
        _fail(e)
    finally:
        registry.close()
    if json_output:
        emit_json(frontier.model_dump(mode="json"))
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
    build_id: str = _BUILD_ID,
    stardag_profile: Optional[str] = _PROFILE_OPTION,
    stardag_env: Optional[str] = _ENV_OPTION,
    limit: int = typer.Option(
        20, "--limit", "-n", min=1, max=200, help="Summaries to show (newest first)."
    ),
    json_output: bool = JSON_OPTION,
) -> None:
    """Show the reactive scheduler's own account of its recent ticks.

    Reads ``GET /builds/{id}/tick-summaries``.
    """
    parsed = _parse_build_id(build_id)
    registry = _resolve_registry(stardag_profile, stardag_env)
    try:
        summaries = registry.build_list_tick_summaries(parsed, limit=limit)
    except StardagError as e:
        _fail(e)
    finally:
        registry.close()
    if json_output:
        emit_json({"summaries": [s.model_dump(mode="json") for s in summaries]})
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
        table.add_row(stamp(record.created_at), record.outcome, detail or "-")
    console.print(table)


# -----------------------------------------------------------------------------
# cancel / complete / fail
# -----------------------------------------------------------------------------


@app.command("cancel")
def builds_cancel(
    build_id: str = _BUILD_ID,
    stardag_profile: Optional[str] = _PROFILE_OPTION,
    stardag_env: Optional[str] = _ENV_OPTION,
    yes: bool = YES_OPTION,
) -> None:
    """Cancel a build: release the claims its plans hold, and stop there.

    Writes ``POST /builds/{id}/cancel``. **Nothing is stopped.** The
    released tasks are CANCELLED — actionable, so any other build holding
    them runs them — and a worker still running exits at its next
    cooperative checkpoint, or runs to completion if its ``run()`` has
    none; its report is recorded as late. Use ``builds stop`` to stop the
    executions first.
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


@app.command("complete")
def builds_complete(
    build_id: str = _BUILD_ID,
    force: bool = typer.Option(
        False,
        "--force",
        help="Complete although members are outstanding. Never overrides a "
        "missing seal or an excluded root.",
    ),
    stardag_profile: Optional[str] = _PROFILE_OPTION,
    stardag_env: Optional[str] = _ENV_OPTION,
    json_output: bool = JSON_OPTION,
) -> None:
    """Mark a build COMPLETED.

    Writes ``POST /builds/{id}/complete``, which the registry refuses (409
    ``plan_incomplete``) unless the active plan is sealed and every
    non-excluded member is COMPLETED; ``--force`` overrides outstanding
    members only. Completing releases any claim the build still holds.
    """
    parsed = _parse_build_id(build_id)
    registry = _resolve_registry(stardag_profile, stardag_env)
    try:
        build = registry.build_complete(parsed, force=force)
    except StardagError as e:
        _fail(e)
    finally:
        registry.close()
    if json_output:
        emit_json(build.model_dump(mode="json"))
        return
    console.print(f"[green]Build {build_id} is {build.status}[/green]")


@app.command("fail")
def builds_fail(
    build_id: str = _BUILD_ID,
    message: Optional[str] = typer.Option(
        None, "--message", "-m", help="Error message recorded on the build."
    ),
    stardag_profile: Optional[str] = _PROFILE_OPTION,
    stardag_env: Optional[str] = _ENV_OPTION,
    yes: bool = YES_OPTION,
    json_output: bool = JSON_OPTION,
) -> None:
    """Mark a build FAILED.

    Writes ``POST /builds/{id}/fail``. Releases the claims the build holds
    (like ``cancel``); stops nothing.
    """
    parsed = _parse_build_id(build_id)
    if not yes:
        if json_output:
            error_console.print(
                "[bold red]Error:[/bold red] refusing to prompt in --json mode; "
                "pass --yes to confirm."
            )
            raise typer.Exit(1)
        typer.confirm(f"Fail build {build_id}?", abort=True)
    registry = _resolve_registry(stardag_profile, stardag_env)
    try:
        build = registry.build_fail(parsed, message)
    except StardagError as e:
        _fail(e)
    finally:
        registry.close()
    if json_output:
        emit_json(build.model_dump(mode="json"))
        return
    console.print(f"[yellow]Build {build_id} is {build.status}[/yellow]")
