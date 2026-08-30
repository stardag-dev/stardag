"""Cross-build wake-ups: who needs a scheduler tick, and who has been asked.

A reactive build has no process. It progresses only while one of its ticks
runs, and a tick runs only when something spawns one. The server cannot
spawn — it has no executor and never pushes — but it sees **every** write
that can change a build's frontier, which makes it the one place the
question "which builds need a tick?" can be answered completely. So the
wake-up is split in two:

- **Flagging** is server state, done here, in the transaction of the write
  that caused it. :func:`flag_after_task_transition` runs after every
  change to a task's ``latest_status``, whatever path wrote it, and flags
  every *other* live reactive build holding the task (a build holds a task
  if it has ever recorded an event for it — the same relation the frontier
  uses). A transition out of RUNNING also flags the builds queued on the
  concurrency-limit keys the task held.

- **Spawning** belongs to whoever has an executor — the scheduler ticks and
  the resident engine when it runs against Modal. They ask
  :func:`select_wake_candidates` for flagged builds nobody is serving, and
  that call *hands each build out once per window* by stamping
  ``tick_requested_at``. Twenty ticks asking at once therefore produce one
  spawn per flagged build, not twenty; a spawner that dies after being
  handed a build costs one window of delay before the next caller gets it.

Neither half consults "who is the caller related to". A flagged build with
no live scheduler needs a tick regardless of who happens to ask, and
keeping the relation out is what keeps every query here on the ``builds``
table alone.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import ColumnElement, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.models import (
    Build,
    BuildStatus,
    Event,
    Task,
    TaskLimitKey,
    TaskStatus,
)
from stardag_api.models.base import as_utc, utc_now

logger = logging.getLogger(__name__)

# How long a build stays "handed out" after a caller was told to spawn a
# tick for it. Bounds a cold start with margin: a second spawner asking
# inside the window is told nothing, so a flagged build gets one container,
# not one per asker. Past the window with the flag still set, the spawn
# evidently did not happen (the app was deleted, the spawner died) and the
# build is handed out again.
WAKE_HANDOUT_WINDOW = timedelta(seconds=120)

# Upper bound on one wake-candidates response. A caller spawns one tick per
# entry, so this bounds the work a single tick takes on for its neighbours;
# whatever is left is handed to the next caller.
MAX_WAKE_CANDIDATES = 20


def _reactive_build_filters(environment_id: UUID, statuses: tuple) -> list:
    return [
        Build.environment_id == environment_id,
        Build.latest_status.in_(statuses),
        Build.reactive_app_name.is_not(None),
        Build.reactive_app_name != "",
    ]


def _live_reactive_build_filters(environment_id: UUID) -> list:
    """Reactive builds a task transition is news for: the RUNNING ones."""
    return _reactive_build_filters(environment_id, (BuildStatus.RUNNING,))


# Statuses a flagged build may be handed out in. RUNNING is the ordinary
# case. CANCELLED is the build-level cancel (UI, CLI, reaper): the build is
# terminal in the registry but its detached executions are still running,
# and only a tick can stop them — so a cancelled build needs exactly one
# more tick, which is what its flag asks for.
_CANDIDATE_STATUSES = (BuildStatus.RUNNING, BuildStatus.CANCELLED)


async def _flag_builds(
    db: AsyncSession,
    build_ids_subquery,
    *,
    environment_id: UUID,
    now: datetime,
) -> None:
    """Set ``needs_tick_at`` on the live reactive builds in a subquery.

    Locks the rows it touches with ``SKIP LOCKED`` (a no-op on SQLite,
    which has no row locks). That is what keeps this hook off the
    documented build-then-task lock order: a build row currently locked by
    someone else is being cancelled or cleaned up, and a build going
    terminal needs no wake-up. Waiting for it would invert the order every
    cancel path takes — build first, then its tasks — against this path's
    task-then-build, and a deadlock is the outcome the order exists to
    prevent.
    """
    targets = (
        select(Build.id)
        .where(
            Build.id.in_(build_ids_subquery),
            *_live_reactive_build_filters(environment_id),
        )
        .order_by(Build.id)
        .with_for_update(skip_locked=True)
    )
    await db.execute(
        update(Build)
        .where(Build.id.in_(targets))
        .values(needs_tick_at=now)
        .execution_options(synchronize_session=False)
    )


async def flag_build(
    db: AsyncSession, build: Build, *, now: datetime | None = None
) -> None:
    """Flag one build for a tick if it is reactively scheduled.

    For state changes that concern the build *itself* rather than one of
    its tasks — a cancel from the UI, say, whose running executions only a
    tick can stop. The flag makes the next scheduler pass anywhere in the
    environment pick it up instead of leaving it to the watchdog.
    """
    if build.reactive_app_name:
        build.needs_tick_at = now or utc_now()


async def flag_after_task_transition(
    db: AsyncSession,
    task: Task,
    *,
    previous_status: TaskStatus | str,
    build_id: UUID,
    now: datetime | None = None,
) -> None:
    """Flag the other builds a task's status change is news for.

    Called by ``services.status.transition_task``, in the same transaction —
    the one path that changes ``latest_status``. A no-op when the status did
    not change: an event landing on an already-completed task (COMPLETED is
    sticky) changes nothing for anyone.

    Every transition flags, including into RUNNING. The ones that matter
    most are out of RUNNING — a completion unblocks downstreams, and any
    release lets a neighbour claim the task — but a retry to PENDING makes
    a blocker runnable for a neighbour too, and the cost of a spurious flag
    is one tick pass that finds nothing, collapsed by the scheduler lease.
    A rule with no exceptions is also one nobody has to re-derive.
    """
    if task.latest_status == previous_status:
        return
    now = now or utc_now()
    holders = (
        select(Event.build_id)
        .where(Event.task_id == task.id, Event.build_id != build_id)
        .distinct()
        .scalar_subquery()
    )
    await _flag_builds(db, holders, environment_id=task.environment_id, now=now)

    if previous_status != TaskStatus.RUNNING:
        return
    # The task held concurrency-limit slots and just released them. The
    # builds waiting on those keys do not hold this task, so the relation
    # above does not reach them; what they hold is a PENDING task recorded
    # under one of the same keys (registered at plan time, see the bulk
    # registration route).
    keys = (
        (
            await db.execute(
                select(TaskLimitKey.key).where(TaskLimitKey.task_pk == task.id)
            )
        )
        .scalars()
        .all()
    )
    if not keys:
        return
    waiting_task_pks = (
        select(TaskLimitKey.task_pk)
        .join(Task, TaskLimitKey.task_pk == Task.id)
        .where(
            TaskLimitKey.key.in_(keys),
            Task.environment_id == task.environment_id,
            Task.latest_status == TaskStatus.PENDING,
            Task.id != task.id,
        )
        .scalar_subquery()
    )
    waiting_builds = (
        select(Event.build_id)
        .where(Event.task_id.in_(waiting_task_pks))
        .distinct()
        .scalar_subquery()
    )
    await _flag_builds(db, waiting_builds, environment_id=task.environment_id, now=now)


# Bounds on a scheduler lease's TTL, in seconds. The lower bound stops a
# lease so short it lapses mid-pass (which would let a second tick in while
# the first is still acting); the upper bound stops a dead tick from
# holding a build hostage for longer than a person will wait.
MIN_LEASE_TTL_SECONDS = 5
MAX_LEASE_TTL_SECONDS = 3600


def lease_is_live(build: Build, now: datetime | None = None) -> bool:
    """Whether ``build`` currently has a scheduler driving it.

    Expiry is part of the question, not a detail: a tick whose container
    died leaves the column set until the TTL lapses, and treating that as a
    live scheduler would suppress wake-ups for exactly the build that most
    needs them.
    """
    if build.scheduler_lease_until is None:
        return False
    return as_utc(build.scheduler_lease_until) > (now or utc_now())


def live_lease_filter(now: datetime | None = None) -> ColumnElement[bool]:
    """SQL for :func:`lease_is_live`, for filtering builds.

    The two must agree: a candidate query that counted expired leases as
    live would hide precisely the abandoned builds the wake-up exists to
    rescue.
    """
    return Build.scheduler_lease_until > (now or utc_now())


def acquire_scheduler_lease(
    build: Build,
    *,
    owner_id: str,
    ttl_seconds: int,
    now: datetime | None = None,
) -> tuple[bool, datetime | None]:
    """Take the build's scheduler lease if it is free or has lapsed.

    Returns ``(acquired, expires_at)``. Pure over the ORM instance — the
    caller owns the transaction, and must have loaded ``build`` FOR UPDATE:
    two ticks racing for one build have to serialize somewhere, and the
    build row is where.

    A lapsed lease denies nothing — this acquire takes it over, replacing
    the dead holder's owner and expiry together. That takeover *is* the
    healing mechanism; nothing has to release the old lease first, and
    across containers there is nothing to release it with.
    """
    now = now or utc_now()
    if lease_is_live(build, now):
        # Live implies non-null; narrowed for the type checker.
        assert build.scheduler_lease_until is not None
        return False, as_utc(build.scheduler_lease_until)
    build.scheduler_lease_owner = owner_id
    build.scheduler_lease_until = now + timedelta(seconds=ttl_seconds)
    return True, build.scheduler_lease_until


def renew_scheduler_lease(
    build: Build,
    *,
    owner_id: str,
    ttl_seconds: int,
    now: datetime | None = None,
) -> datetime | None:
    """Extend the lease, but only for the holder. None = not the holder.

    Owner-checked rather than blind, and the check is what makes a slow
    tick safe: one whose lease lapsed and was taken over must not be able
    to extend the new holder's lease out from under it. It learns it lost
    by being refused here.
    """
    now = now or utc_now()
    if build.scheduler_lease_owner != owner_id or not lease_is_live(build, now):
        return None
    build.scheduler_lease_until = now + timedelta(seconds=ttl_seconds)
    return build.scheduler_lease_until


def release_scheduler_lease(
    build: Build,
    *,
    owner_id: str,
) -> bool:
    """Drop the lease if ``owner_id`` still holds it.

    Owner-checked for the same reason renew is: a tick that lost its lease
    to a takeover and then exits must not clear the successor's.
    """
    if build.scheduler_lease_owner != owner_id:
        return False
    build.scheduler_lease_owner = None
    build.scheduler_lease_until = None
    return True


async def mark_tick_requested(
    db: AsyncSession, build: Build, *, now: datetime | None = None
) -> None:
    """Record that a caller is about to spawn a tick for ``build``.

    Used by ``POST /builds/{id}/notify``, in the same transaction as the
    flag it sets: the notifying worker will spawn unless a scheduler is
    live, so a concurrent wake-candidates call must never see the build
    flagged and unstamped. The route restores the previous stamp when the
    lease read says a scheduler is live after all.
    """
    build.tick_requested_at = now or utc_now()


async def select_wake_candidates(
    db: AsyncSession,
    *,
    environment_id: UUID,
    limit: int = MAX_WAKE_CANDIDATES,
    now: datetime | None = None,
) -> list[Build]:
    """Hand out the flagged builds nobody is serving. No commit.

    A build qualifies when it is RUNNING (or CANCELLED with the flag its
    cancel set — see ``_CANDIDATE_STATUSES``), reactively scheduled, has a
    pending wake-up (``needs_tick_at``), holds no live scheduler lease, and
    was not handed out within :data:`WAKE_HANDOUT_WINDOW`. Every build
    returned is stamped ``tick_requested_at = now`` in the same transaction,
    which is what makes concurrent callers get disjoint answers (rows are
    taken ``FOR UPDATE SKIP LOCKED``, so two drains never wait on each
    other either). Oldest wake-up first, so a starved build is not starved
    by newer ones.

    One query. The lease used to live in ``distributed_locks`` under a name
    built by concatenating a prefix with the build id, so "has a live
    scheduler" was a second round-trip over the candidates — the join was
    not worth spelling portably. On the build row it is a column
    comparison in the same WHERE, and the over-fetch that existed to absorb
    the post-filter goes with it.
    """
    now = now or utc_now()
    limit = max(1, min(limit, MAX_WAKE_CANDIDATES))
    handout_before = now - WAKE_HANDOUT_WINDOW
    chosen = list(
        (
            await db.execute(
                select(Build)
                .where(
                    *_reactive_build_filters(environment_id, _CANDIDATE_STATUSES),
                    Build.needs_tick_at.is_not(None),
                    (Build.tick_requested_at.is_(None))
                    | (Build.tick_requested_at < handout_before),
                    # No live scheduler. NULL is "never leased", which SQL
                    # comparison drops, so it is spelled out — a build that
                    # has never been leased is exactly one that needs a tick.
                    (Build.scheduler_lease_until.is_(None)) | ~live_lease_filter(now),
                )
                .order_by(Build.needs_tick_at.asc(), Build.id.asc())
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    for build in chosen:
        build.tick_requested_at = now
    return chosen
