"""Terminating abandoned builds and releasing the claims their tasks hold.

Build status is derived from build-level events, so a build whose
orchestrator died without emitting a terminal event stays RUNNING forever:
interrupted local builds, crashed CI runs and failed triggers accumulate
permanently. Worse, cancelling a build has never cascaded to its tasks, so
a task left RUNNING keeps denying its execution claim — ``latest_status`` is
environment-global — to every future build that needs it, and keeps
occupying its concurrency-limit slots.

This module is the shared machinery for fixing that: selecting builds that
are genuinely abandoned, and cancelling a build together with the claims it
holds. ``routes/builds.py`` exposes it (single-build cascade, bulk cancel,
and the reaper — bulk cancel with an idleness filter); ``main.py`` can
optionally drive the sweep on a timer.

Everything here is idempotent. Cancelling a build that is already terminal
is a no-op, so a retried call, two racing operators, or two API replicas
running the same sweep produce the same end state.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import ColumnElement, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.models import Build, Event, EventType, Task, TaskStatus
from stardag_api.models.base import utc_now
from stardag_api.services.status import apply_event_to_task

logger = logging.getLogger(__name__)

# Build-level events that end a build, and the two that (re)start it. A
# build's derived status is decided by whichever of these came last — see
# services.status.get_build_status.
_TERMINAL_BUILD_EVENTS = (
    EventType.BUILD_COMPLETED,
    EventType.BUILD_FAILED,
    EventType.BUILD_CANCELLED,
    EventType.BUILD_EXIT_EARLY,
)
_ACTIVE_BUILD_EVENTS = (EventType.BUILD_STARTED, EventType.BUILD_RESUMED)

# Task statuses a build cancel cascades to. RUNNING holds the execution
# claim and the concurrency-limit slots — that is the whole point. SUSPENDED
# is an execution that yielded for dynamic dependencies and was then
# abandoned; it holds no slot but is equally unschedulable and equally
# permanent.
#
# PENDING is deliberately NOT cascaded, even though it is "non-terminal".
# A PENDING task holds no claim and blocks nothing another build cannot
# resolve by simply running it — whereas cancelling it *would* reach across
# builds: task rows are per environment, and a task this build registered
# may be referenced by a live build elsewhere (TASK_REFERENCED leaves
# latest_status_build_id alone, so ownership scoping cannot tell the two
# apart). Cancelling a build must not fail somebody else's. Use
# POST /builds/{id}/skip-blocked, or the per-task cancel, for pending work.
CASCADE_CANCEL_STATUSES = (TaskStatus.RUNNING, TaskStatus.SUSPENDED)


def build_is_running() -> ColumnElement[bool]:
    """SQL predicate: the correlated ``Build`` row's derived status is RUNNING.

    Build status is not a column, and the existing ``GET /builds?status=``
    filter therefore derives it in Python over a bounded scan
    (``_STATUS_FILTER_SCAN_CAP``). A reaper must not inherit that cap: its
    job is to find *every* stale RUNNING build, and the ones that matter are
    precisely the oldest — the ones a "most recently active first, first 500"
    scan drops first.

    So the rule is expressed in SQL. RUNNING means the most recent
    build-level event is BUILD_STARTED or BUILD_RESUMED, i.e.::

        max(created_at) over start/resume events  >  max(...) over terminals

    with a missing terminal max meaning "never ended". Two correlated
    aggregates over ``ix_events_build_created``; build-level events are a
    handful per build, and callers put a cheap index-backed filter in front
    (see :func:`select_cancellable_builds`).

    A tie — a terminal event sharing a timestamp with a start/resume — is
    read as NOT running, so a reaper leaves it alone. ``get_build_status``
    replays in timestamp order and would resolve the tie arbitrarily; when
    the two disagree, the conservative reading has to win.
    """
    latest_active = (
        select(func.max(Event.created_at))
        .where(
            Event.build_id == Build.id,
            Event.task_id.is_(None),
            Event.event_type.in_(_ACTIVE_BUILD_EVENTS),
        )
        .correlate(Build)
        .scalar_subquery()
    )
    latest_terminal = (
        select(func.max(Event.created_at))
        .where(
            Event.build_id == Build.id,
            Event.task_id.is_(None),
            Event.event_type.in_(_TERMINAL_BUILD_EVENTS),
        )
        .correlate(Build)
        .scalar_subquery()
    )
    return latest_active.is_not(None) & or_(
        latest_terminal.is_(None), latest_terminal < latest_active
    )


def last_event_at_subquery() -> ColumnElement[datetime]:
    """``max(events.created_at)`` for the correlated ``Build`` — ANY event.

    This — not ``Build.last_active_at`` — is the load-bearing part of the
    idleness signal. ``last_active_at`` is bumped by build-level lifecycle
    transitions only (see ``_touch_build_last_active``: task events skip it
    on purpose, so worker traffic doesn't serialise on the build row), so a
    build that has been running tasks continuously for three days still
    carries the timestamp of its BUILD_STARTED. Reaping on that column would
    cancel live work — the exact failure a reaper must never have.

    ``max(created_at)`` over the build's whole event stream *does* move with
    task registrations, starts, completions and failures, and it is served by
    ``ix_events_build_created`` as a one-row backward index scan.
    """
    return (
        select(func.max(Event.created_at))
        .where(Event.build_id == Build.id)
        .correlate(Build)
        .scalar_subquery()
    )


def _as_utc(value: datetime) -> datetime:
    """Attach UTC to a naive timestamp.

    Every timestamp we write is UTC (``utc_now``), but SQLite's DATETIME
    storage format carries no offset, so values read back from the test
    database are naive while values still in the session are aware. Mixing
    the two in a comparison raises. Postgres returns aware values
    throughout, so this is a no-op there.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def last_activity_at(build: Build, last_event_at: datetime | None) -> datetime | None:
    """The build's idleness signal: the newest of every liveness input.

    Three inputs, because each catches something the others miss:

    - ``last_event_at`` — the event stream, i.e. real work (see above).
    - ``build.last_active_at`` — lifecycle transitions, plus the one
      mutation that writes no event at all (appending roots).
    - ``build.needs_tick_at`` — a pending scheduler wake-up. Set by
      ``POST /builds/{id}/notify`` (a worker reporting it finished
      something) and cleared by the tick that consumes it; while it is set,
      somebody is waiting for a scheduler that hasn't run yet, which is the
      opposite of abandoned.
    """
    candidates = [
        _as_utc(ts)
        for ts in (last_event_at, build.last_active_at, build.needs_tick_at)
        if ts is not None
    ]
    return max(candidates) if candidates else None


def idle_filters(idle_before: datetime) -> list[ColumnElement[bool]]:
    """SQL for "this build's :func:`last_activity_at` is before ``idle_before``".

    **The single definition of "idle", used by both the reaper
    (:func:`select_cancellable_builds`) and ``GET /builds?idle_for_seconds=``.**
    They must not drift: the list endpoint is how an operator previews what
    the reaper will do, and a list that disagrees with the reaper is worse
    than no list at all.

    Written as an AND of "every liveness input is old" rather than
    ``max(inputs) < cutoff``: the two are equivalent, and the AND needs no
    ``GREATEST()`` (which SQLite lacks) and no dialect switch.

    ``Build.last_active_at < idle_before`` is logically redundant — the real
    signal is a max() that includes that column — but load-bearing for the
    plan: it is the only conjunct backed by an index
    (``ix_builds_environment_last_active``), and because it is one of the
    inputs to the max it can never exclude a build the full predicate would
    keep. It narrows the candidate set before the correlated aggregates run.
    """
    last_event_at = last_event_at_subquery()
    return [
        Build.last_active_at < idle_before,
        last_event_at < idle_before,
        or_(Build.needs_tick_at.is_(None), Build.needs_tick_at < idle_before),
    ]


# Ordering used whenever an idleness filter is applied — by both the reaper
# and GET /builds. ``last_active_at`` is a *proxy* for staleness, not the
# definition of it: sorting on the full last_activity_at signal would
# forfeit ix_builds_environment_last_active and sort the whole candidate
# set for a max over a correlated aggregate. Two builds can therefore
# appear slightly out of order with respect to the ``last_activity_at``
# each row reports. That is accepted — ordering only decides which of the
# already-correctly-filtered builds come first, and correctness lives in
# the filter. ``Build.id`` (UUID7) breaks ties so server-side pagination
# can neither duplicate nor skip a row.
STALEST_FIRST_ORDER = (Build.last_active_at.asc(), Build.id.asc())


@dataclass
class CancelledBuild:
    """One build cancelled by :func:`cancel_builds` (or selected in a dry run)."""

    build: Build
    last_activity_at: datetime | None
    cascaded_task_ids: list[str] = field(default_factory=list)


async def select_cancellable_builds(
    db: AsyncSession,
    *,
    environment_id: UUID | None = None,
    build_ids: list[UUID] | None = None,
    idle_before: datetime | None = None,
    reactive_app_name: str | None = None,
    include_reactive: bool = False,
    limit: int = 100,
) -> tuple[list[tuple[Build, datetime | None]], bool]:
    """Find RUNNING builds matching the filter, stalest first.

    Returns ``(rows, truncated)`` where each row is ``(build,
    last_event_at)`` and ``truncated`` says more builds matched than
    ``limit``. ``environment_id=None`` spans every environment (the
    in-process sweep; the HTTP endpoint always scopes to the caller's).

    Ordered stalest-first by :data:`STALEST_FIRST_ORDER` — see there for why
    the sort key is a proxy rather than the full signal. Correctness comes
    from the filter; convergence comes from cancellation itself, since a
    reaped build stops being RUNNING and drops out of the next call.
    """
    filters: list[ColumnElement[bool]] = [build_is_running()]
    if environment_id is not None:
        filters.append(Build.environment_id == environment_id)
    if build_ids is not None:
        filters.append(Build.id.in_(build_ids))
    if reactive_app_name is not None:
        filters.append(Build.reactive_app_name == reactive_app_name)
    elif not include_reactive:
        filters.append(Build.reactive_app_name.is_(None))
    if idle_before is not None:
        filters.extend(idle_filters(idle_before))

    result = await db.execute(
        select(Build, last_event_at_subquery().label("last_event_at"))
        .where(*filters)
        .order_by(*STALEST_FIRST_ORDER)
        .limit(limit + 1)
    )
    rows = [(build, ts) for build, ts in result.all()]
    return rows[:limit], len(rows) > limit


async def cascade_cancel_build_tasks(
    db: AsyncSession,
    build_id: UUID,
    *,
    event_metadata: dict | None = None,
) -> list[Task]:
    """Emit TASK_CANCELLED for the claims ``build_id`` holds. No commit.

    "Claims this build holds" means tasks that are RUNNING or SUSPENDED
    **and whose current status was produced by this build**
    (``latest_status_build_id``). The ownership scope is what makes cascading
    safe: a task this build merely referenced, while another build is
    actually running it, belongs to that build's cancel, not this one — the
    server cannot stop a live execution, it can only rewrite the registry's
    view of it, so cancelling somebody else's running task would leave a live
    worker writing into a task the registry has declared dead.

    Returns the affected task rows (already mutated in the session). The
    caller commits.
    """
    build_task_pks = (
        select(Event.task_id)
        .where(Event.build_id == build_id, Event.task_id.is_not(None))
        .distinct()
        .scalar_subquery()
    )
    tasks = (
        (
            await db.execute(
                select(Task)
                .where(
                    Task.id.in_(build_task_pks),
                    Task.latest_status.in_(CASCADE_CANCEL_STATUSES),
                    Task.latest_status_build_id == build_id,
                )
                # Deterministic lock order (task_id, matching bulk-register
                # and skip-blocked) so concurrent cancels can't deadlock.
                .order_by(Task.task_id.asc())
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    for task in tasks:
        event = Event(
            build_id=build_id,
            task_id=task.id,
            event_type=EventType.TASK_CANCELLED,
            event_metadata=event_metadata,
        )
        db.add(event)
        # Flush per event so event.id / created_at exist before
        # apply_event_to_task reads them (same pattern as skip-blocked).
        await db.flush()
        apply_event_to_task(task, event)
    return list(tasks)


async def cancel_builds(
    db: AsyncSession,
    rows: list[tuple[Build, datetime | None]],
    *,
    cascade: bool = True,
    reason: str | None = None,
    triggered_by_user_id: str | None = None,
    source: str = "bulk_cancel",
) -> list[CancelledBuild]:
    """Cancel each build (and optionally its claims) in one transaction.

    Commits once at the end: either the whole sweep lands or none of it
    does, and a crash mid-sweep leaves no build cancelled without its
    cascade. Re-running afterwards picks up exactly the builds that are
    still RUNNING.
    """
    event_metadata: dict = {"cancelled_by": source}
    if reason:
        event_metadata["reason"] = reason
    if triggered_by_user_id:
        event_metadata["triggered_by_user_id"] = triggered_by_user_id

    results: list[CancelledBuild] = []
    now = utc_now()
    for build, last_event_at in rows:
        cancelled_tasks: list[Task] = []
        if cascade:
            cancelled_tasks = await cascade_cancel_build_tasks(
                db, build.id, event_metadata=event_metadata
            )
        db.add(
            Event(
                build_id=build.id,
                task_id=None,
                event_type=EventType.BUILD_CANCELLED,
                error_message=reason,
                event_metadata=event_metadata,
            )
        )
        # Same bookkeeping the single-build cancel endpoint does. Note this
        # moves the build to the top of the last_active_at ordering — correct:
        # its status just changed, and it can no longer be re-selected
        # (build_is_running() is now false for it).
        await db.execute(
            update(Build).where(Build.id == build.id).values(last_active_at=now)
        )
        results.append(
            CancelledBuild(
                build=build,
                last_activity_at=last_activity_at(build, last_event_at),
                cascaded_task_ids=[t.task_id for t in cancelled_tasks],
            )
        )
    await db.commit()
    return results


async def sweep_stale_builds(
    db: AsyncSession,
    *,
    idle_before: datetime,
    environment_id: UUID | None = None,
    include_reactive: bool = False,
    cascade: bool = True,
    limit: int = 100,
) -> list[CancelledBuild]:
    """Select and cancel stale RUNNING builds — the reaper, in one call.

    Used by the optional in-process periodic invoker; the HTTP endpoint
    composes the same two steps itself so it can also serve explicit
    ``build_ids`` and dry runs.
    """
    rows, truncated = await select_cancellable_builds(
        db,
        environment_id=environment_id,
        idle_before=idle_before,
        include_reactive=include_reactive,
        limit=limit,
    )
    if not rows:
        return []
    if truncated:
        logger.info(
            "stale-build sweep: more than %d stale builds matched; "
            "cancelling the %d oldest this pass.",
            limit,
            limit,
        )
    return await cancel_builds(db, rows, cascade=cascade, source="reaper")


async def run_periodic_sweep(stop: asyncio.Event) -> None:
    """Sweep every ``interval_seconds`` until ``stop`` is set.

    The unattended half of the reaper, started from the app lifespan only
    when ``STARDAG_API_REAPER_ENABLED`` is true (see
    :class:`~stardag_api.config.ReaperSettings` for the multi-replica
    caveat). It waits one full interval before the first sweep so a
    crash-looping process never reaps, and swallows per-sweep exceptions so
    a transient database error can't kill the loop — the next interval
    retries, and the sweep is idempotent.
    """
    from stardag_api.config import reaper_settings
    from stardag_api.db import async_session_maker

    logger.info(
        "stale-build reaper enabled: sweeping every %ds for builds idle "
        "over %ds (include_reactive=%s, cascade=%s, max %d builds/sweep)",
        reaper_settings.interval_seconds,
        reaper_settings.idle_for_seconds,
        reaper_settings.include_reactive,
        reaper_settings.cascade,
        reaper_settings.max_builds_per_sweep,
    )
    while not stop.is_set():
        try:
            await asyncio.wait_for(
                stop.wait(), timeout=reaper_settings.interval_seconds
            )
            return  # stop was set — shutting down
        except TimeoutError:
            pass
        try:
            async with async_session_maker() as db:
                cancelled = await sweep_stale_builds(
                    db,
                    idle_before=utc_now()
                    - timedelta(seconds=reaper_settings.idle_for_seconds),
                    include_reactive=reaper_settings.include_reactive,
                    cascade=reaper_settings.cascade,
                    limit=reaper_settings.max_builds_per_sweep,
                )
            if cancelled:
                logger.warning(
                    "stale-build reaper cancelled %d abandoned build(s), "
                    "releasing %d task claim(s): %s",
                    len(cancelled),
                    sum(len(c.cascaded_task_ids) for c in cancelled),
                    ", ".join(str(c.build.id) for c in cancelled),
                )
        except Exception:  # pragma: no cover - defensive
            # Never let a bad sweep take the loop (or the app) down.
            logger.exception("stale-build reaper sweep failed; retrying next interval")
