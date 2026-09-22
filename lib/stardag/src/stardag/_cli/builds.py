"""Build inspection and cleanup commands for the Stardag CLI.

Answering "what does the scheduler actually think the state is?" used to
mean hand-rolling calls against the registry API. These commands are that
question, in the order you ask it when something is wrong:

    stardag builds list [--status running] [--reactive-app NAME] [--older-than 24h]
    stardag builds show <build-id>       # status, roots, reactive meta, liveness
    stardag builds frontier <build-id>   # actionable / running / roots, WITH blockers
    stardag builds ticks <build-id>      # what each scheduler tick decided, and why
    stardag builds stop <build-id>       # stop its live executions, THEN cancel it
    stardag builds cancel <build-id>     # record the event; stop nothing
    stardag builds cleanup [--older-than 24h] [--apply]

``frontier`` is the diagnostic one. A reactive build with nothing
actionable and nothing running is not necessarily dead: task rows and
dependency edges are per *environment*, so an upstream that some other
build left non-COMPLETED gates this build's tasks while contributing to
none of the counts this build can see. That is what
``blocked_by_external`` reports, and rendering it is the difference
between "this build is stuck for no reason" and "task X is RUNNING under
build Y since T".

``stop`` is the one that ends a build that is still *doing* something,
and the order it works in is its whole reason to exist: it lists the
build's executions while the build still holds their claims — the only
moment the task row is exact — cancels those calls, and cancels the build
last. ``cancel`` is the other half, for a build already believed dead: it
records the event and stops nothing. ``--cascade``, which did the two in
the wrong order, is gone.

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
from typing import Any, NoReturn, Optional
from uuid import UUID

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

# Selection and Modal cancellation for ``builds stop``, kept out of this
# module so the rules can be tested without a CLI runner.
from stardag._cli import _stop

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
from stardag.exceptions import NotFoundError, StardagError, is_missing_route_error
from stardag.registry import BuildFrontier, BuildSummary, FrontierTaskRef
from stardag.build._reactive import _TERMINAL_BUILD_STATUSES

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


def _fail_missing_route(
    exc: NotFoundError, command: str, endpoint: str, hint: str | None = None
) -> NoReturn:
    """Report a 404 that means "this registry is too old", not "no such thing".

    A registry predating an endpoint serves it as FastAPI's generic
    missing-route 404, which this CLI otherwise renders as "resource not
    found" — the user reads that as a bad build id and goes looking for the
    build. Same distinction the SDK draws everywhere else (see
    ``is_missing_route_error``); a genuine resource-level 404 still falls
    through to the normal error path.
    """
    if not is_missing_route_error(exc):
        _fail(exc)
    error_console.print(
        f"[bold red]Error:[/bold red] this registry does not support "
        f"'stardag {command}' — its {endpoint} endpoint is missing. "
        "Upgrade stardag-api to a version matching this SDK."
    )
    if hint:
        error_console.print(f"[dim]{hint}[/dim]")
    raise typer.Exit(1)


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
    # Only for a failed build, and it is the most useful row on one: the
    # scheduler's reason names the blocking task, its owner and the remedy.
    # Wrapped rather than truncated — a truncated remedy is not a remedy.
    #
    # Truthiness rather than `is not None`, and the two agree: the server
    # excludes blank reasons from the field, so None is the only way "no reason
    # recorded" is expressed. A blank row headed "Failure reason" would be
    # noise, so if that ever changes server-side this is the behaviour to keep.
    if build.latest_error_message:
        table.add_row("Failure reason", build.latest_error_message)
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


# Build statuses that mean "this build is over". Wider than the scheduler's
# _TERMINAL_BUILD_STATUSES, which deliberately omits exit_early because a tick
# still has work to do on such a build. For *reporting*, exit_early is just as
# finished as the rest — a human asking "why is nothing happening?" about an
# exited build should not be told it might be stuck.
_FINISHED_BUILD_STATUSES = (*_TERMINAL_BUILD_STATUSES, "exit_early")


def _render_external_blockers(frontier: BuildFrontier) -> None:
    """Explain a build with nothing to do, and render blockers if any come.

    Dependency edges are scoped to the build's structure scope, and the
    plan is closed over that scope at registration and again whenever the
    build stalls — so a gate cannot point outside the plan any more, and a
    current server always answers an empty ``blocked_by_external``. A build
    with nothing actionable and nothing running is therefore finished or
    genuinely stuck on tasks of its own, and its status counts say which.

    The table below is kept for a server predating scopes, which still
    reports blockers when the build looks stalled.
    """
    terminal = frontier.build_status in _FINISHED_BUILD_STATUSES
    stalled = not terminal and not frontier.actionable and not frontier.running

    if not frontier.blocked_by_external:
        if terminal:
            console.print(
                f"\n[dim]Blockers: not applicable — this build is "
                f"{frontier.build_status}.[/dim]"
            )
        elif not stalled:
            console.print(
                "\n[dim]Blockers: none. Every upstream in this build's "
                "structure scope is part of its plan, and it has "
                f"{len(frontier.actionable)} actionable and "
                f"{len(frontier.running)} running.[/dim]"
            )
        else:
            console.print(
                "\n[yellow]Nothing actionable and nothing running.[/yellow]\n"
                "[dim]Every gate is inside this build's plan, so it is waiting "
                "on tasks of its own that nothing will move — a failed "
                "upstream under fail_mode=continue, or a status the tick "
                "cannot reset. The status counts above name them; a re-trigger "
                "resets the retryable set.[/dim]"
            )
        return

    table = Table(
        title=(
            f"External blockers ({len(frontier.blocked_by_external)}"
            f"{', truncated' if frontier.blocked_by_external_truncated else ''})"
        ),
        caption=(
            "Reported by a registry predating structure scopes: tasks of this "
            "build held back by an upstream whose status another build produced."
        ),
    )
    table.add_column("Blocked task")
    table.add_column("Waiting on")
    table.add_column("Status")
    table.add_column("For", justify="right")
    table.add_column("Owned by build")
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
        )
    console.print(table)

    if frontier.blocked_by_external_truncated:
        console.print(
            "[yellow]The list is truncated[/yellow] — there are more blockers "
            "than the server returns."
        )
    console.print(
        "\n[bold]What happens next depends on the status[/bold] — "
        "[bold]running[/bold] means another build holds the execution claim; "
        "it resolves when that build finishes or the claim expires. "
        "[bold]cancelled[/bold] and [bold]skipped[/bold] are this build's to "
        "reset and run within its attempt budget. [bold]failed[/bold] is a "
        "result — a tick leaves it to this build's fail_mode, so re-trigger "
        "the build to reset it and run it here."
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
    except NotFoundError as e:
        # The reactive scheduler's *reporting* side reads the same signal and
        # disables itself silently; here the user asked for the data, so they
        # get told why there is none.
        _fail_missing_route(e, "builds ticks", "tick-summaries")
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
            "\n[dim]Only reactively-scheduled builds report them. (A registry "
            "too old for the endpoint fails above rather than showing "
            "nothing.)[/dim]"
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
        # Both spellings, so a script that passed either keeps parsing.
        # ``--no-cascade`` asked for exactly today's behaviour, so failing
        # it would be gratuitous; ``--cascade`` asked for something that no
        # longer exists, and is refused below.
        "--cascade/--no-cascade",
        help="Removed — use 'stardag builds stop' instead.",
        hidden=True,
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip the confirmation prompt."
    ),
) -> None:
    """Cancel a build: record the event and stop there.

    Nothing is stopped and no claim is released. Task state is per
    environment, so a task this build left RUNNING keeps denying its claim
    to every future build that needs it until the claim's own expiry
    lapses — which is why this is the command for a build you believe is
    *already dead*, and `stardag builds stop` is the one for a build whose
    containers are still running.

    Cancelling a live build the other way round is the mistake the split
    exists to prevent: the release lets the next build take those tasks
    over, so within seconds the task row names a successor's execution and
    the one you meant to stop is no longer reachable by any query about
    the present.
    """
    parsed = _parse_build_id(build_id)
    if cascade:
        # Kept as a hidden flag rather than deleted so a script that still
        # passes it is told where the behaviour went, instead of getting
        # typer's "no such option" — or, far worse, silently cancelling
        # the build and leaving its containers running.
        error_console.print(
            "[bold red]Error:[/bold red] --cascade has been removed. It "
            "released the build's claims and left its containers running, "
            "so another build could take a task over while the old "
            "execution was still going.\n"
            f"Use: [bold]stardag builds stop {build_id}[/bold]  — it lists "
            "the build's live executions while the claims still make that "
            "list exact, stops them, and only then cancels the build."
        )
        raise typer.Exit(1)
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
        "[dim]Nothing was stopped. If this build still has containers "
        f"running, they will run to completion — 'stardag builds stop "
        f"{build_id}' is the command that stops them first.[/dim]"
    )


@app.command("stop")
def builds_stop(
    build_id: str = typer.Argument(..., help="Build ID"),
    stardag_profile: Optional[str] = _PROFILE_OPTION,
    stardag_env: Optional[str] = _ENV_OPTION,
    worker: Optional[str] = typer.Option(
        None,
        "--worker",
        help="Only executions on this worker (the name the app declares).",
    ),
    executor: Optional[str] = typer.Option(
        None, "--executor", help="Only executions on this executor, e.g. 'modal'."
    ),
    namespace: Optional[str] = typer.Option(
        None, "--namespace", help="Only tasks whose namespace starts with this."
    ),
    older_than: Optional[str] = typer.Option(
        None,
        "--older-than",
        help="Only executions running at least this long (e.g. 30m, 6h, 2d).",
    ),
    task_id: Optional[list[str]] = typer.Option(
        None, "--task-id", help="Only this task (repeatable)."
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print the list and exit. Stops nothing and cancels nothing.",
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip the confirmation prompt."
    ),
    json_output: bool = _JSON_OPTION,
) -> None:
    """Stop a build's running executions, then cancel the build.

    The order is the whole design. This lists the executions the build
    holds *while it still holds their claims*, which is the only moment
    the task row is exact: a cancel releases those claims, and from then
    on another build may take a task over, so the row names a successor's
    call while yours is still running. Stopping first removes the question
    entirely — no event-log reconstruction, no ranking, no "is this one
    mine?".

    What it does, in order: read the list, show it, cancel each selected
    Modal call, and only then cancel the build (which releases the
    claims). Executions on another executor are listed and left alone —
    stardag reaches Modal and nothing else.

    What "exact" covers: every execution the list names is this build's,
    and nothing it names has been taken over. What it does not cover is
    being stoppable. A task claimed a moment ago has no call id on its row
    until its spawn reports one, so it is listed and marked not stoppable
    rather than dropped — re-run to catch it once the spawn lands.

    `--dry-run` prints the list and stops there. Anything a filter
    excludes keeps running after the build is cancelled: it no longer
    holds a claim, so its output is the only thing that can land, and
    COMPLETED is sticky. Filter deliberately.

    Hard kills are the Modal dashboard's job — a container that ignores
    its cancellation is outside what stardag can reach.
    """
    parsed = _parse_build_id(build_id)
    older_than_seconds = _parse_stop_older_than(older_than)
    filters = _stop.Filters(
        worker=worker,
        executor=executor,
        namespace=namespace,
        older_than_seconds=older_than_seconds,
        task_ids=tuple(task_id or ()),
    )

    registry = _resolve_registry(stardag_profile, stardag_env)
    try:
        try:
            executions, server_filtered = _stop.collect_executions(registry, parsed)
        except _stop.TooManyClaimHolders as e:
            error_console.print(f"[bold red]Error:[/bold red] {e}")
            raise typer.Exit(1)
        except StardagError as e:
            _fail(e)

        # One evaluation per execution, and both lists read off it. Asking
        # ``matches`` twice would re-read the clock, so an execution right
        # on an ``--older-than`` boundary could land in both lists or in
        # neither.
        if not server_filtered:
            # The list is still right — every row is re-checked client-side
            # — but this registry scanned its whole task table to produce
            # it, which is what the pause was.
            error_console.print(
                "[bold yellow]Warning:[/bold yellow] this registry does not "
                "support filtering tasks by status, so the whole task table "
                "was scanned. The list below is correct; upgrade "
                "stardag-api to make it cheap."
            )

        verdicts = [(e, filters.matches(e)) for e in executions]
        selected = [e for e, ok in verdicts if ok]
        excluded = [e for e, ok in verdicts if not ok]
        stoppable = [e for e in selected if e.stoppable]
        unstoppable = [e for e in selected if not e.stoppable]

        if json_output and not dry_run and not yes:
            # Before the document, not after: a caller parsing stdout would
            # otherwise get a complete, successful-looking selection from a
            # run that exited non-zero and stopped nothing.
            error_console.print(
                "[bold red]Error:[/bold red] refusing to prompt in --json "
                "mode; pass --yes to confirm."
            )
            raise typer.Exit(1)

        # The document describes the *whole run*, and is therefore written
        # once, at the end of it — after the calls have been stopped and
        # the build cancelled. Emitting the selection up front and acting
        # afterwards is what left a complete, successful-looking document
        # on stdout for a run that aborted partway (Modal not importable,
        # say) and stopped nothing. A caller reading stdout cannot see an
        # exit code, so the document must not exist unless it is true.
        payload: dict[str, Any] = {
            "build_id": str(parsed),
            "selected": [_stop_json(e) for e in selected],
            "excluded_by_filter": [_stop_json(e) for e in excluded],
            "dry_run": dry_run,
        }

        if dry_run:
            # Nothing happens, so there is nothing to wait for.
            if json_output:
                _emit_json(payload)
            else:
                _render_executions(selected, excluded, build_id)
                console.print(
                    "\n[bold]Dry run — nothing was stopped and the build "
                    "was not cancelled.[/bold]"
                )
            return

        if not json_output:
            _render_executions(selected, excluded, build_id)

        if not yes:
            # --json with no --yes already exited above, before anything
            # reached stdout.
            typer.confirm(
                _stop_confirmation(build_id, stoppable, unstoppable, excluded),
                abort=True,
            )

        # Progress reporting; in --json mode it goes to stderr so that
        # stdout stays the one document.
        report = error_console if json_output else console
        stop_results: list[dict[str, Any]] = []

        if stoppable:
            try:
                outcomes = _stop.cancel_modal_calls(stoppable)
            except _stop.ModalUnavailable as e:
                error_console.print(f"[bold red]Error:[/bold red] {e}")
                error_console.print(
                    "[dim]The build was NOT cancelled — its claims are "
                    "still held, so this list stays exact and the command "
                    "can be re-run.[/dim]"
                )
                raise typer.Exit(1)
            _render_cancel_outcomes(outcomes, report)
            stop_results = [
                {
                    "task_id": o.execution.task_id,
                    "executor_ref": o.execution.executor_ref,
                    "stopped": o.ok,
                    "error": o.error,
                }
                for o in outcomes
            ]
            if any(not o.ok for o in outcomes):
                # Reported, not fatal. Every failure here is "this one call
                # could not be reached"; refusing to release the claims
                # because of it would leave the build holding every other
                # task too, and the operator has the list in front of them.
                error_console.print(
                    "[bold yellow]Warning:[/bold yellow] some calls could "
                    "not be cancelled (above). They may already have "
                    "ended; if not, stop them from the Modal dashboard."
                )

        try:
            registry.build_cancel(parsed, cascade=True)
        except StardagError as e:
            _fail(e)

        if json_output:
            # Now, and only now: every field below is a statement about
            # something that has already happened.
            payload["stop_results"] = stop_results
            payload["stopped_count"] = sum(1 for r in stop_results if r["stopped"])
            payload["build_cancelled"] = True
            _emit_json(payload)
    finally:
        registry.close()

    report.print(
        f"[green]Cancelled build[/green] {build_id} "
        f"— stopped {len(stoppable)} execution(s), released its claims."
    )
    if unstoppable:
        # Selected, not stopped. Louder than the excluded note below
        # because nobody asked for this one: a filter leaving something
        # running is the operator's own decision, whereas this is the
        # command falling short of what they asked for.
        report.print(
            f"[yellow]{len(unstoppable)} selected execution(s) could not be "
            "stopped[/yellow] and keep running:"
        )
        for execution in unstoppable:
            report.print(
                f"  [dim]{execution.task_id}  {execution.qualified_name}  "
                f"— {execution.not_stoppable_reason}[/dim]"
            )
    if excluded:
        report.print(
            f"[dim]{len(excluded)} execution(s) were excluded by a filter "
            "and keep running. They no longer hold a claim, so a result "
            "they produce still counts (COMPLETED is sticky).[/dim]"
        )


def _parse_stop_older_than(value: str | None) -> int | None:
    """Parse ``builds stop --older-than``, with no server minimum.

    Deliberately not ``_parse_older_than``: that one enforces a 60-second
    floor because it filters *builds* for a reaper, where a threshold
    short enough to race a live build is a foot-gun. Here the operator is
    looking at their own build's executions and asking "which of these has
    been going a while" — ``--older-than 30s`` is a reasonable question
    and nothing acts on the answer unattended.
    """
    if value is None:
        return None
    try:
        return parse_duration(value)
    except ValueError as e:
        error_console.print(f"[bold red]Error:[/bold red] {e}")
        raise typer.Exit(1)


def _stop_json(execution: "_stop.Execution") -> dict[str, Any]:
    """One execution as a JSON object (the fields the table renders)."""
    return {
        "task_id": execution.task_id,
        "task_namespace": execution.task_namespace,
        "task_name": execution.task_name,
        "status": execution.status,
        "executor": execution.executor,
        "executor_ref": execution.executor_ref,
        "executor_metadata": execution.executor_metadata,
        "worker": execution.worker,
        "status_at": execution.status_at,
        "restart_due": execution.restart_due,
        "stoppable": execution.stoppable,
        # Null when it is stoppable. Present so a caller parsing this can
        # tell "on another executor, never stoppable from here" from
        # "claimed a moment ago, stoppable once its spawn reports".
        "not_stoppable_reason": execution.not_stoppable_reason,
    }


def _render_executions(
    selected: Sequence["_stop.Execution"],
    excluded: Sequence["_stop.Execution"],
    build_id: str,
) -> None:
    """The table this command is mostly about."""
    if not selected:
        if excluded:
            console.print(
                f"No execution of build {build_id} matches these filters "
                f"({len(excluded)} excluded)."
            )
        else:
            console.print(f"Build {build_id} holds no running executions.")
            console.print(
                "\n[dim]Either it has none left, or another build has "
                "taken its tasks over — 'stardag builds frontier "
                f"{build_id}' says which.[/dim]"
            )
        return

    table = Table(title=f"Executions held by build {build_id}")
    table.add_column("Task ID")
    table.add_column("Task")
    table.add_column("Status")
    table.add_column("Executor")
    table.add_column("Ref")
    table.add_column("Worker")
    table.add_column("Running for", justify="right")
    for execution in selected:
        status = execution.status
        if execution.restart_due:
            status += " (restart due)"
        table.add_row(
            execution.task_id,
            execution.qualified_name,
            status,
            execution.executor
            if execution.executor == _stop.MODAL_EXECUTOR
            else f"{execution.executor} [yellow](not stoppable here)[/yellow]",
            execution.executor_ref or "[yellow](not recorded yet)[/yellow]",
            execution.worker or "-",
            _age(execution.status_at),
        )
    console.print(table)

    # The rows that are about to be left running, said once and plainly.
    # A reader who skims the table sees a Modal executor and a task id and
    # assumes it is handled; the ref cell is the only thing that says
    # otherwise, and it is the easiest column to miss.
    unspawned = [e for e in selected if e.executor_ref is None]
    if unspawned:
        console.print(
            f"[yellow]{len(unspawned)} of these were claimed but have not "
            "reported a call id yet[/yellow], so there is nothing to "
            "cancel and they keep running. Their spawn reports within a "
            "container start — re-run this command to catch them."
        )

    workspaces = _stop.modal_workspaces(e for e in selected if e.stoppable)
    if workspaces:
        console.print(
            "[dim]Modal workspace(s): "
            + ", ".join(sorted(workspaces))
            + " — the active Modal profile must be authenticated to them, "
            "or a call simply will not be found.[/dim]"
        )
    if excluded:
        console.print(
            f"[dim]{len(excluded)} further execution(s) excluded by the "
            "filters; they keep running.[/dim]"
        )


def _stop_confirmation(
    build_id: str,
    stoppable: Sequence["_stop.Execution"],
    unstoppable: Sequence["_stop.Execution"],
    excluded: Sequence["_stop.Execution"],
) -> str:
    """The prompt, saying exactly what is about to happen and to how many.

    The count left running leads, because it is the part that is easy to
    get wrong: a filter narrows what is *stopped*, never what the cancel
    releases.
    """
    action = f"Stop {len(stoppable)} execution(s) and cancel build {build_id}?"
    left_running = len(unstoppable) + len(excluded)
    if not left_running:
        return action
    return (
        f"{left_running} execution(s) will keep running with their claims "
        f"released. {action}"
    )


def _render_cancel_outcomes(
    outcomes: Sequence["_stop.CancelOutcome"], report: Console
) -> None:
    """Per-call result, because a partial stop has to be visible."""
    for outcome in outcomes:
        ref = outcome.execution.executor_ref or "(no call id)"
        name = outcome.execution.qualified_name
        if outcome.ok:
            report.print(f"  [green]stopped[/green] {ref}  {name}")
        else:
            # Escaped: an exception message is arbitrary text, and a `[...]`
            # in it would be eaten as rich markup.
            report.print(
                f"  [red]failed[/red]  {ref}  {name}  {escape(outcome.error or '')}"
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
            # Naming a reactive app *is* the request to include reactive
            # builds — the flag's help says so. Forwarding a False here
            # would let the server exclude precisely the builds the user
            # asked for by name, and report zero matches for a filter that
            # matched.
            include_reactive=include_reactive or reactive_app is not None,
            cascade=cascade,
            dry_run=not do_apply,
            limit=limit,
            reason=reason,
        )
    except NotFoundError as e:
        # Especially confusing here: a dry run against an old registry reads
        # as "not found" even though nothing was looked up by id yet.
        _fail_missing_route(
            e,
            "builds cleanup",
            "bulk-cancel",
            hint="Until then, handle abandoned builds one at a time: "
            "stardag builds stop <build-id>",
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
