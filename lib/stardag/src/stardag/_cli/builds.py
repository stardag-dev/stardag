"""Build inspection and cleanup commands for the Stardag CLI.

Answering "what does the scheduler actually think the state is?" used to
mean hand-rolling calls against the registry API. These commands are that
question, in the order you ask it when something is wrong:

    stardag builds list [--status running] [--reactive-app NAME] [--older-than 24h]
    stardag builds show <build-id>       # status, roots, reactive meta, liveness
    stardag builds frontier <build-id>   # actionable / running / roots, WITH blockers
    stardag builds ticks <build-id>      # what each scheduler tick decided, and why
    stardag builds cancel <build-id> [--cascade]
    stardag builds cleanup [--older-than 24h] [--apply]

``frontier`` is the diagnostic one. A reactive build with nothing
actionable and nothing running is not necessarily dead: task rows and
dependency edges are per *environment*, so an upstream that some other
build left non-COMPLETED gates this build's tasks while contributing to
none of the counts this build can see. That is what
``blocked_by_external`` reports, and rendering it is the difference
between "this build is stuck for no reason" and "task X is RUNNING under
build Y since T".

``cleanup`` is the runbook for abandoned builds. Build status is derived
from build-level events, so a build whose orchestrator died without
emitting one stays RUNNING forever, holding whatever execution claims and
concurrency-limit slots its tasks had at that moment. It defaults to a dry
run and needs ``--apply`` to act.

Machine-readable output: every read-only command (and ``cleanup``, whose
dry run is read-only) takes ``--json``. In that mode stdout carries the
JSON document and nothing else — hints, warnings and confirmations all go
to stderr — so ``stardag builds list --json | jq`` is safe. The document
is the SDK's model of the API payload (``model_dump(mode="json")``): same
field names and nesting as the REST response, minus any field this SDK
version does not model.
"""

import json
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import UUID

import typer
from rich.table import Table

# Shared by every registry-backed CLI group; imported into this module's
# namespace so ``stardag._cli.builds._resolve_registry`` is the patch point.
from stardag._cli._duration import format_duration, parse_duration
from stardag._cli._registry_ctx import (
    _ENV_OPTION,
    _PROFILE_OPTION,
    _fail,
    _resolve_registry,
    console,
    error_console,
)
from stardag.exceptions import StardagError
from stardag.registry import BuildFrontier, BuildSummary, FrontierTaskRef

app = typer.Typer(
    help="Inspect, cancel and clean up builds in an environment",
    no_args_is_help=True,
)

# Server-side minimum for the idle filters (a threshold small enough to
# race a live build is a foot-gun, not a feature). Checked client-side too
# so ``--older-than 30s`` fails with the grammar in front of the user
# rather than as a 422 from three layers down.
_MIN_IDLE_SECONDS = 60

_JSON_OPTION = typer.Option(
    False,
    "--json",
    help="Emit the API payload as JSON on stdout (nothing else goes to stdout).",
)


def _emit_json(payload: Any) -> None:
    """Write one JSON document to stdout, and nothing else.

    ``typer.echo`` rather than the rich console on purpose: rich would
    syntax-highlight and soft-wrap, which is exactly the contamination
    ``--json`` promises not to produce.
    """
    typer.echo(json.dumps(payload, indent=2, default=str))


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    """Interpret a naive timestamp as UTC (the API's own convention)."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _age(value: datetime | None) -> str:
    """Render "how long ago" for a table cell."""
    if value is None:
        return "-"
    return format_duration((_utcnow() - _as_utc(value)).total_seconds())


def _stamp(value: datetime | None) -> str:
    """Render an absolute timestamp for a table cell (seconds resolution)."""
    if value is None:
        return "-"
    return _as_utc(value).strftime("%Y-%m-%d %H:%M:%SZ")


def _parse_older_than(value: str | None) -> int | None:
    """Parse an ``--older-than`` flag into seconds, or exit with the grammar."""
    if value is None:
        return None
    try:
        seconds = parse_duration(value)
    except ValueError as e:
        error_console.print(f"[bold red]Error:[/bold red] {e}")
        raise typer.Exit(1)
    if seconds < _MIN_IDLE_SECONDS:
        error_console.print(
            f"[bold red]Error:[/bold red] --older-than must be at least "
            f"{_MIN_IDLE_SECONDS}s; a shorter staleness threshold can race a "
            "build that is merely between events."
        )
        raise typer.Exit(1)
    return seconds


@app.command("list")
def builds_list(
    stardag_profile: Optional[str] = _PROFILE_OPTION,
    stardag_env: Optional[str] = _ENV_OPTION,
    status: Optional[str] = typer.Option(
        None,
        "--status",
        help="Derived build status: pending / running / completed / failed / cancelled.",
    ),
    reactive_app: Optional[str] = typer.Option(
        None,
        "--reactive-app",
        help="Only builds driven by this reactive app.",
    ),
    older_than: Optional[str] = typer.Option(
        None,
        "--older-than",
        help="Only builds idle at least this long (e.g. 24h, 90m, 3d; min 60s).",
    ),
    page: int = typer.Option(1, "--page", min=1, help="Page number (1-based)."),
    limit: int = typer.Option(
        20, "--limit", "-n", min=1, max=100, help="Builds per page (max 100)."
    ),
    json_output: bool = _JSON_OPTION,
) -> None:
    """List builds, most recently active first.

    `--older-than` measures *activity* — the newest of the build's whole
    event stream, its lifecycle column and any pending scheduler wake-up —
    not the list's ordering column, which task events deliberately never
    touch. Filtering on the ordering column would call a build that has
    been running tasks for three days "idle". The filter is applied
    server-side against the same SQL predicate the reaper and
    `builds cleanup` use, so this list and that cleanup agree on what is
    stale; with it set the server orders stalest-first.
    """
    idle_seconds = _parse_older_than(older_than)
    if idle_seconds is not None and status is not None and status != "running":
        # Only RUNNING has a SQL predicate for the derived status; the other
        # statuses are filtered after a bounded scan, which would pair an
        # exact-looking ``total`` with an approximate one. The server rejects
        # the combination with a 422 — say so here instead of surfacing it raw.
        error_console.print(
            "[bold red]Error:[/bold red] --older-than can only be combined "
            f"with --status running (got {status!r}). Idleness is only "
            "meaningful for builds that are still running."
        )
        raise typer.Exit(1)

    registry = _resolve_registry(stardag_profile, stardag_env)
    try:
        result = registry.build_list(
            page=page,
            page_size=limit,
            status=status,
            reactive_app_name=reactive_app,
            idle_for_seconds=idle_seconds,
        )
    except StardagError as e:
        _fail(e)
    finally:
        registry.close()

    builds = list(result.builds)
    if idle_seconds is not None:
        # A CLI can be newer than the registry it talks to, and an older
        # server *ignores* a query param it does not know rather than
        # rejecting it — so "--older-than 24h" would quietly mean
        # "everything". Detect that and say so loudly.
        #
        # Deliberately a warning, not a local filter: the server paginates
        # and counts, so cutting the page here would drop rows from a page
        # already chosen without the filter — under-reporting precisely the
        # oldest builds, which are the entire population --older-than exists
        # to find. A row with no ``last_activity_at`` counts as evidence too:
        # a server that cannot report the field cannot have filtered on it.
        cutoff = _utcnow() - timedelta(seconds=idle_seconds)
        unfiltered = [
            b
            for b in builds
            if b.last_activity_at is None or _as_utc(b.last_activity_at) > cutoff
        ]
        if unfiltered:
            error_console.print(
                "[bold yellow]Warning:[/bold yellow] this registry does not "
                "support --older-than (idle_for_seconds); it ignored the "
                f"filter and returned {len(unfiltered)} build(s) newer than "
                "the cutoff. [bold]The results below are unfiltered.[/bold] "
                "Upgrade stardag-api to filter by idleness."
            )

    if json_output:
        _emit_json(
            {
                "builds": [b.model_dump(mode="json") for b in builds],
                "total": result.total,
                "page": result.page,
                "page_size": result.page_size,
            }
        )
        return

    if not builds:
        console.print("No builds match this filter.")
        console.print(
            "\n[dim]Try a wider filter, e.g. stardag builds list --status running[/dim]"
        )
        return

    table = Table(title=f"Builds (page {result.page}, {result.total} total)")
    table.add_column("Build ID")
    table.add_column("Name")
    table.add_column("Status")
    table.add_column("Reactive app")
    table.add_column("Last activity")
    table.add_column("Idle", justify="right")
    for build in builds:
        table.add_row(
            str(build.id),
            build.name,
            build.status or "-",
            build.reactive_app_name or "-",
            _stamp(build.last_activity_at),
            _age(build.last_activity_at),
        )
    console.print(table)


@app.command("show")
def builds_show(
    build_id: str = typer.Argument(..., help="Build ID"),
    stardag_profile: Optional[str] = _PROFILE_OPTION,
    stardag_env: Optional[str] = _ENV_OPTION,
    json_output: bool = _JSON_OPTION,
) -> None:
    """Show one build: status, roots, reactive metadata and liveness."""
    parsed = _parse_build_id(build_id)
    registry = _resolve_registry(stardag_profile, stardag_env)
    try:
        build = registry.build_get_summary(parsed)
    except StardagError as e:
        _fail(e)
    finally:
        registry.close()

    if json_output:
        _emit_json(build.model_dump(mode="json"))
        return

    _render_build(build)


def _render_build(build: BuildSummary) -> None:
    table = Table(title=f"Build {build.id}", show_header=False)
    table.add_column("Field", style="bold")
    table.add_column("Value")
    table.add_row("Name", build.name)
    table.add_row("Status", build.status or "-")
    if build.is_resumed:
        table.add_row("Resumed", "yes")
    table.add_row("Description", build.description or "-")
    table.add_row("Commit", build.commit_hash or "-")
    table.add_row("Created", _stamp(build.created_at))
    table.add_row("Started", _stamp(build.started_at))
    table.add_row("Completed", _stamp(build.completed_at))
    # Two different numbers; the labels spell out which is which because
    # confusing them is how live work gets reaped.
    table.add_row("Last lifecycle event", _stamp(build.last_active_at))
    table.add_row(
        "Last activity (any)",
        f"{_stamp(build.last_activity_at)}  ({_age(build.last_activity_at)} ago)",
    )
    table.add_row(
        "Reactive app",
        build.reactive_app_name or "- (not reactively scheduled)",
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


@app.command("frontier")
def builds_frontier(
    build_id: str = typer.Argument(..., help="Build ID"),
    stardag_profile: Optional[str] = _PROFILE_OPTION,
    stardag_env: Optional[str] = _ENV_OPTION,
    json_output: bool = _JSON_OPTION,
) -> None:
    """Show a build's scheduling frontier, including cross-build blockers.

    The diagnostic command: what a reactive scheduler tick sees when it
    decides whether the build can progress. `actionable` are the tasks
    it would act on now, `running` the executions it would probe, and
    the external-blocker section explains a build that has neither.
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
        _emit_json(frontier.model_dump(mode="json"))
        return

    summary = Table(title=f"Frontier of build {frontier.build_id}", show_header=False)
    summary.add_column("Field", style="bold")
    summary.add_column("Value")
    summary.add_row("Build status", frontier.build_status)
    summary.add_row(
        "Reactive app", frontier.reactive_app_name or "- (not reactively scheduled)"
    )
    summary.add_row("Needs tick", "yes" if frontier.needs_tick else "no")
    summary.add_row("Actionable", str(len(frontier.actionable)))
    summary.add_row("Running", str(len(frontier.running)))
    summary.add_row(
        "Status counts",
        ", ".join(f"{k}={v}" for k, v in sorted(frontier.status_counts.items())) or "-",
    )
    console.print(summary)

    _render_task_refs("Actionable tasks", frontier.actionable)
    _render_task_refs("Running tasks", frontier.running)

    roots_done = sum(1 for r in frontier.roots if r.latest_status == "completed")
    console.print(
        f"Roots: {roots_done}/{len(frontier.root_task_ids)} completed"
        + (
            ""
            if len(frontier.roots) == len(frontier.root_task_ids)
            else f" ({len(frontier.roots)} of them have recorded status)"
        )
    )

    _render_external_blockers(frontier)


def _render_task_refs(title: str, refs: Sequence[FrontierTaskRef]) -> None:
    if not refs:
        return
    table = Table(title=title)
    table.add_column("Task ID")
    table.add_column("Status")
    table.add_column("Since")
    table.add_column("Executor")
    table.add_column("Ref")
    for ref in refs:
        table.add_row(
            ref.task_id,
            ref.latest_status,
            _stamp(ref.latest_status_at),
            ref.latest_executor or "-",
            ref.latest_executor_ref or "-",
        )
    console.print(table)


def _render_external_blockers(frontier: BuildFrontier) -> None:
    """Render (or honestly explain the absence of) the external blockers.

    The server populates ``blocked_by_external`` **only** when the build
    has nothing actionable and nothing running — a per-edge join is not
    worth doing on every healthy build's poll. So an empty list means
    "not externally blocked OR not stalled", and printing "no blockers"
    for a build that is merely progressing would be a lie of exactly the
    kind this command exists to stop telling.
    """
    stalled = not frontier.actionable and not frontier.running

    if not frontier.blocked_by_external:
        if not stalled:
            console.print(
                "\n[dim]External blockers: not evaluated. The server computes "
                "them only for a build with nothing actionable and nothing "
                "running; this build has "
                f"{len(frontier.actionable)} actionable and "
                f"{len(frontier.running)} running, so it is progressing.[/dim]"
            )
        else:
            console.print(
                "\n[yellow]No external blockers reported, and this build has "
                "nothing actionable and nothing running.[/yellow]\n"
                "[dim]Either it is genuinely stuck (a tick will fail it), or "
                "the registry API predates the blocker fields.[/dim]"
            )
        return

    table = Table(
        title=(
            f"External blockers ({len(frontier.blocked_by_external)}"
            f"{', truncated' if frontier.blocked_by_external_truncated else ''})"
        ),
        caption=(
            "Tasks of this build held back by an upstream whose current "
            "status another build produced."
        ),
    )
    table.add_column("Blocked task")
    table.add_column("Waiting on")
    table.add_column("Status")
    table.add_column("For", justify="right")
    table.add_column("Owned by build")
    table.add_column("In this build")
    for blocker in frontier.blocked_by_external:
        qualified = (
            f"{blocker.blocking_task_namespace}.{blocker.blocking_task_name}"
            if blocker.blocking_task_namespace
            else blocker.blocking_task_name
        )
        table.add_row(
            blocker.task_id,
            f"{qualified}\n[dim]{blocker.blocking_task_id}[/dim]",
            blocker.blocking_status,
            _age(blocker.blocking_status_at),
            str(blocker.blocking_status_build_id or "unknown"),
            "yes" if blocker.blocking_in_build else "no",
        )
    console.print(table)

    if frontier.blocked_by_external_truncated:
        console.print(
            "[yellow]The list is truncated[/yellow] — there are more blockers "
            "than the server returns. It is a diagnostic, not a work queue; a "
            "truncated list still proves the build is waiting, not stuck."
        )

    # The two cases need different remedies, so say which applies.
    out_of_build = [b for b in frontier.blocked_by_external if not b.blocking_in_build]
    in_build = [b for b in frontier.blocked_by_external if b.blocking_in_build]
    if out_of_build:
        console.print(
            f"\n[bold]{len(out_of_build)} blocker(s) are not in this build's task "
            "set[/bold] — this build will never schedule them and can only wait "
            "for the build that owns them. Check that owner with "
            "'stardag builds show <owning-build-id>'; if it is gone, release the "
            "claim with 'stardag tasks cancel <owning-build-id> <task-id>'."
        )
    if in_build:
        console.print(
            f"\n[bold]{len(in_build)} blocker(s) are in this build's task set[/bold] "
            "but another build produced their current status. They will resolve "
            "when that build finishes them; a retry from here would not release "
            "the claim."
        )


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
    """Show the reactive scheduler's own account of its recent ticks.

    Each reactive tick runs in its own short-lived container, so its
    reasoning used to reach nobody but that container's log. Retention is
    finite server-side — this is the recent past, which is what a stalled
    build's diagnosis needs (it repeats the same outcome forever).
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
        _emit_json({"summaries": [s.model_dump(mode="json") for s in summaries]})
        return

    if not summaries:
        console.print(f"No tick summaries recorded for build {build_id}.")
        console.print(
            "\n[dim]Only reactively-scheduled builds report them, and only "
            "against a registry API that supports the endpoint.[/dim]"
        )
        return

    table = Table(title=f"Tick summaries for build {build_id} (newest first)")
    table.add_column("When")
    table.add_column("Outcome")
    table.add_column("Detail")
    for record in summaries:
        # The summary is an open blob the server stores verbatim, so render
        # whatever it holds rather than a fixed field list — a newer SDK's
        # extra counters must not go invisible here.
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
    cascade: bool = typer.Option(
        False,
        "--cascade/--no-cascade",
        help=(
            "Also cancel this build's RUNNING/SUSPENDED tasks, releasing their "
            "execution claims and concurrency-limit slots."
        ),
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip the confirmation prompt."
    ),
) -> None:
    """Cancel a build, optionally releasing the claims its tasks hold.

    Without `--cascade` this records a build-level event and nothing
    else — which is why cancelling a build has never actually cleaned
    anything up. Task state is per environment, so a task this build left
    RUNNING keeps denying its claim to every future build that needs it.

    The server cannot stop anything: a worker whose task is cancelled here
    keeps running until it notices, and a completion that lands afterwards
    wins (COMPLETED is sticky). Cancel builds you believe are dead.
    """
    parsed = _parse_build_id(build_id)
    if not yes:
        typer.confirm(
            f"Cancel build {build_id}"
            + (" and its running tasks' claims" if cascade else "")
            + "?",
            abort=True,
        )

    registry = _resolve_registry(stardag_profile, stardag_env)
    try:
        result = registry.build_cancel(parsed, cascade=cascade)
    except StardagError as e:
        _fail(e)
    finally:
        registry.close()

    console.print(f"[green]Cancelled build[/green] {build_id}")
    if result is not None and result.cascaded_task_ids:
        console.print(
            f"Released {len(result.cascaded_task_ids)} task claim(s): "
            + ", ".join(result.cascaded_task_ids)
        )
    elif cascade:
        console.print(
            "[dim]No task claims to release (none of its tasks were "
            "RUNNING/SUSPENDED under this build).[/dim]"
        )


@app.command("cleanup")
def builds_cleanup(
    stardag_profile: Optional[str] = _PROFILE_OPTION,
    stardag_env: Optional[str] = _ENV_OPTION,
    older_than: Optional[str] = typer.Option(
        None,
        "--older-than",
        help="Select builds idle at least this long (e.g. 24h, 3d; min 60s).",
    ),
    build_id: Optional[list[str]] = typer.Option(
        None,
        "--build-id",
        help="Select an explicit build (repeatable). Combinable with --older-than.",
    ),
    reactive_app: Optional[str] = typer.Option(
        None,
        "--reactive-app",
        help="Restrict to builds driven by this reactive app (implies --include-reactive).",
    ),
    include_reactive: bool = typer.Option(
        False,
        "--include-reactive",
        help="Include reactive builds, which are quiet between ticks by design.",
    ),
    cascade: bool = typer.Option(
        True,
        "--cascade/--no-cascade",
        help="Also cancel each build's claim-holding tasks (on by default).",
    ),
    limit: int = typer.Option(
        100, "--limit", "-n", min=1, max=500, help="Cap on builds handled per call."
    ),
    reason: Optional[str] = typer.Option(
        None, "--reason", help="Note recorded on each cancellation event."
    ),
    apply: bool = typer.Option(
        False,
        "--apply",
        help="Actually cancel. Without it this is a dry run that writes nothing.",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help=(
            "Skip the confirmation prompt. Does NOT imply --apply; pair the "
            "two to run unattended."
        ),
    ),
    json_output: bool = _JSON_OPTION,
) -> None:
    """Find and cancel abandoned builds, releasing the claims they hold.

    Defaults to a dry run: it prints exactly what a real run would cancel
    — the builds, the task claims that would be released, and why any
    explicitly-named build was skipped — and writes nothing.

    `--apply` is the only thing that makes this act. `-y/--yes`
    only skips the confirmation prompt: on a command that is a dry run by
    default, `-y` alone must not turn into a cascade of cancellations —
    that is exactly the surprise a destructive command may not have. Use
    `--apply` interactively and `--apply --yes` on a timer.

    The selection is the *server's*, both times: the dry run and the real
    run take the same filter through the same endpoint, so what you review
    is what you get. Reactive builds are excluded unless you ask for them,
    because a reactive build is quiet between ticks by design and already
    has a watchdog for the case where it wedges.
    """
    idle_seconds = _parse_older_than(older_than)
    build_ids = list(build_id or [])
    if not build_ids and idle_seconds is None:
        error_console.print(
            "[bold red]Error:[/bold red] pass --older-than and/or --build-id. "
            "Cancelling every running build in an environment unconditionally "
            "is not a cleanup operation."
        )
        raise typer.Exit(1)

    # --apply is the sole switch from "report" to "act"; -y only silences the
    # prompt (see the docstring).
    do_apply = apply
    if do_apply and not yes:
        if json_output:
            error_console.print(
                "[bold red]Error:[/bold red] refusing to prompt in --json mode; "
                "pass --yes to confirm."
            )
            raise typer.Exit(1)
        typer.confirm(
            "Cancel the builds matching this filter"
            + (" and release their task claims" if cascade else "")
            + "?",
            abort=True,
        )

    registry = _resolve_registry(stardag_profile, stardag_env)
    try:
        result = registry.build_bulk_cancel(
            build_ids=build_ids or None,
            idle_for_seconds=idle_seconds,
            reactive_app_name=reactive_app,
            include_reactive=include_reactive,
            cascade=cascade,
            dry_run=not do_apply,
            limit=limit,
            reason=reason,
        )
    except StardagError as e:
        _fail(e)
    finally:
        registry.close()

    if json_output:
        _emit_json(result.model_dump(mode="json"))
        return

    if result.dry_run:
        console.print("[bold]Dry run — nothing was cancelled.[/bold]")

    if not result.builds:
        console.print("No builds match this filter.")
    else:
        verb = "Would cancel" if result.dry_run else "Cancelled"
        console.print(
            f"{verb} {result.build_count} build(s), releasing "
            f"{result.task_count} task claim(s)."
        )
        table = Table(title="Builds")
        table.add_column("Build ID")
        table.add_column("Name")
        table.add_column("Reactive app")
        table.add_column("Last activity")
        table.add_column("Idle", justify="right")
        table.add_column("Claims", justify="right")
        for ref in result.builds:
            table.add_row(
                str(ref.build_id),
                ref.name,
                ref.reactive_app_name or "-",
                _stamp(ref.last_activity_at),
                _age(ref.last_activity_at),
                str(len(ref.cascaded_task_ids)),
            )
        console.print(table)

    if result.skipped:
        skipped = Table(title="Skipped")
        skipped.add_column("Build ID")
        skipped.add_column("Reason")
        for skipped_id, why in sorted(result.skipped.items()):
            skipped.add_row(skipped_id, _SKIP_REASONS.get(why, why))
        console.print(skipped)

    if result.truncated:
        console.print(
            "[yellow]More builds matched than --limit allowed.[/yellow] Run "
            "again to continue."
        )

    if result.dry_run and result.builds:
        console.print("\n[dim]Re-run with --apply to cancel these builds.[/dim]")


# Server-side skip codes, spelled out. The raw codes stay in --json output;
# only the human rendering is expanded.
_SKIP_REASONS = {
    "not_found": "not found (unknown id, or another environment)",
    "not_running": "not running (already terminal)",
    "reactive": "reactive build (pass --include-reactive)",
    "not_idle": "active more recently than --older-than",
}


def _parse_build_id(value: str) -> UUID:
    """Parse a build id argument, failing with a CLI error rather than a stack trace."""
    try:
        return UUID(value)
    except ValueError:
        error_console.print(
            f"[bold red]Error:[/bold red] {value!r} is not a valid build ID (UUID)."
        )
        raise typer.Exit(1)
