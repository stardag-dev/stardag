"""Cross-build wake-ups: who needs a scheduler tick, and who has been asked.

Unchanged in mechanism from v1, re-keyed onto plans (design.md, "Wake-ups,
limits, locks"). A reactive build has no process; it progresses only while
one of its ticks runs, and the server — which cannot spawn — sees every
write that can change a build's frontier. So the wake-up is split in two:

- **Flagging** is server state, written in the transaction of the write
  that caused it. :func:`flag_after_transition` runs after every change of
  ``task.status`` (``transition_task()`` calls it) and sets ``needs_tick_at``
  on every *other* RUNNING reactive build **whose active plan holds the
  task** (``plan_member`` of active plans — not "any event in the build",
  v1's relation, which woke builds for tasks outside their plan). A move
  out of RUNNING also flags the builds with actionable members queued on
  the limit keys the task held.
- **Spawning** belongs to whoever has an executor. They ask
  :func:`wake_candidates` for flagged builds nobody is serving; each is
  handed out once per :data:`WAKE_HANDOUT_WINDOW` by stamping
  ``tick_requested_at``, so N concurrent askers produce one spawn.

The flags live on ``build_wake`` (one row per build), not on ``build``: a
claiming start holds its build row ``FOR SHARE`` while it holds a task row,
so a flagger — itself inside a task transition — could neither wait for
the build row (the lock order is build → task; waiting would deadlock
against the claim's task lock) nor ``SKIP LOCKED`` it without losing the
wake-up of every build with a claim in flight. Flagging reads ``build``
without a lock and locks only ``build_wake`` rows, ``FOR NO KEY UPDATE SKIP
LOCKED`` in build-id order; those rows are locked only by other flaggers,
``notify`` and ``wake-candidates``, each for one short statement and none
while waiting on anything else, so a skipped row is one whose flag another
writer is setting at that moment.

The scheduler lease is two owner-checked columns on ``build``: at most one
tick drives a build; a lapsed lease is taken over by the next acquire.

``flag_after_transition`` also bumps ``build.last_active_at`` on every
RUNNING build holding the task (reactive or not — a resident build has no
flag but still has task activity), as v1 did. This ``SKIP LOCKED``s
``build`` directly rather than going through ``build_wake``: unlike the
flag, a missed bump has no correctness consequence, so the lock-avoidance
that motivates the separate table doesn't apply here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import Select, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.models import (
    Build,
    BuildStatus,
    BuildWake,
    Plan,
    PlanMember,
    Task,
    TaskLimitKey,
    TaskStatus,
)
from stardag_api.models.base import utc_now
from stardag_api.services.errors import BadRequest, NotFound
from stardag_api.services.transition_types import ACTIONABLE_STATUSES
from stardag_api.services.tx import transaction

#: How long a build stays "handed out" after a caller was told to spawn a
#: tick for it: a second asker inside the window is told nothing.
WAKE_HANDOUT_WINDOW = timedelta(seconds=120)
#: Upper bound on one wake-candidates response.
MAX_WAKE_CANDIDATES = 20
#: Bounds on a scheduler lease's TTL, in seconds.
MIN_LEASE_TTL_SECONDS = 5
MAX_LEASE_TTL_SECONDS = 3600


# ---------------------------------------------------------------------------
# Flagging
# ---------------------------------------------------------------------------


def _holding_builds(task_pks: Any) -> Select[tuple[UUID]]:
    """Builds whose **active** plan holds (a non-excluded member for) any of
    ``task_pks`` — a list or a subquery."""
    return (
        select(Plan.build_id)
        .join(PlanMember, PlanMember.plan_id == Plan.id)
        .where(
            PlanMember.task_pk.in_(task_pks),
            PlanMember.excluded_at.is_(None),
            Plan.activated_at.is_not(None),
            Plan.superseded_at.is_(None),
        )
    )


async def _flag(
    session: AsyncSession,
    environment_id: UUID,
    build_ids: Select[tuple[UUID]],
    *,
    exclude_build_id: UUID | None,
    now: datetime,
) -> None:
    # The build row is read, never locked (see the module docstring); only
    # the wake rows are.
    targets = (
        select(BuildWake.build_id)
        .join(Build, Build.id == BuildWake.build_id)
        .where(
            BuildWake.build_id.in_(build_ids),
            BuildWake.environment_id == environment_id,
            Build.status == BuildStatus.RUNNING,
            Build.reactive_app_name.is_not(None),
        )
        .order_by(BuildWake.build_id)
        .with_for_update(of=BuildWake, key_share=True, skip_locked=True)
    )
    if exclude_build_id is not None:
        targets = targets.where(BuildWake.build_id != exclude_build_id)
    await session.execute(
        update(BuildWake)
        .where(BuildWake.build_id.in_(targets))
        .values(needs_tick_at=now)
        .execution_options(synchronize_session=False)
    )


async def _bump_last_active(
    session: AsyncSession,
    environment_id: UUID,
    build_ids: Select[tuple[UUID]],
    *,
    now: datetime,
) -> None:
    """Task activity bumps ``last_active_at``, as it did in v1. Wider than
    ``_flag``'s targets: a resident build has no wake row and no tick to
    spawn, but its ``last_active_at`` should move on its own task events the
    same as a reactive build's, so this reaches every RUNNING build in
    ``build_ids``, not only the reactive ones.

    ``SKIP LOCKED`` directly on ``build`` — the thing the module docstring
    says flagging must not do, because losing a flag can stall a build's
    scheduling. Missing a ``last_active_at`` bump has no such consequence
    (it self-heals on the build's next task event, or its next lifecycle
    write): so a build a claiming start or a terminal transition holds
    concurrently just misses this one bump. No new lock is taken to get
    there — this reuses ``build_ids`` and adds one more ``SKIP LOCKED``
    UPDATE of the same shape as the flag's, on ``build`` instead of
    ``build_wake`` (the two tables can't share one UPDATE statement).
    """
    targets = (
        select(Build.id)
        .where(
            Build.id.in_(build_ids),
            Build.environment_id == environment_id,
            Build.status == BuildStatus.RUNNING,
        )
        .with_for_update(skip_locked=True)
    )
    await session.execute(
        update(Build)
        .where(Build.id.in_(targets))
        # Monotonic: ``now`` is the caller's pre-lock timestamp, so a transition
        # delayed behind the task lock must not move a newer stamp backwards.
        .values(
            last_active_at=func.greatest(func.coalesce(Build.last_active_at, now), now)
        )
        .execution_options(synchronize_session=False)
    )


async def flag_after_transition(
    session: AsyncSession,
    environment_id: UUID,
    task_pk: UUID,
    *,
    previous: TaskStatus,
    current: TaskStatus,
    source_build_id: UUID | None,
    now: datetime,
) -> None:
    """Flag the other builds a status change of ``task_pk`` is news for, and
    bump ``last_active_at`` on every RUNNING build holding it — the source
    build included, unlike the flag (a build's own task activity is exactly
    what should keep it "active"; only the flag, which wakes a build up for
    something *else*, has no reason to tell a build about itself).

    Every transition flags, into RUNNING included: a spurious flag costs one
    tick pass that finds nothing, collapsed by the scheduler lease, and a
    rule with no exceptions is one nobody has to re-derive. No-op when the
    status did not change.
    """
    if previous == current:
        return
    holding = _holding_builds([task_pk])
    await _flag(
        session,
        environment_id,
        holding,
        exclude_build_id=source_build_id,
        now=now,
    )
    await _bump_last_active(session, environment_id, holding, now=now)
    if previous != TaskStatus.RUNNING:
        return
    # The task's limit slots are free. The builds waiting on those keys may
    # not hold this task; what they hold is an actionable member whose last
    # claim attempt recorded the same keys.
    keys = select(TaskLimitKey.key).where(TaskLimitKey.task_pk == task_pk)
    waiting = (
        select(TaskLimitKey.task_pk)
        .join(Task, Task.id == TaskLimitKey.task_pk)
        .where(
            TaskLimitKey.environment_id == environment_id,
            TaskLimitKey.key.in_(keys),
            Task.id != task_pk,
            Task.status.in_(ACTIONABLE_STATUSES),
        )
    )
    await _flag(
        session,
        environment_id,
        _holding_builds(waiting),
        exclude_build_id=None,
        now=now,
    )


# ---------------------------------------------------------------------------
# Notify and wake-candidates
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NotifyState:
    build_id: UUID
    #: A RUNNING build has a pending wake-up.
    needs_tick: bool
    #: A scheduler held the lease once the flag was durable (POST only).
    scheduler_live: bool | None = None


def _lease_live(build: Build, now: datetime) -> bool:
    return build.scheduler_lease_until is not None and (
        build.scheduler_lease_until > now
    )


async def _build(session: AsyncSession, environment_id: UUID, build_id: UUID) -> Build:
    """The build row, read without a lock and re-read from the database."""
    build = await session.scalar(
        select(Build)
        .where(Build.environment_id == environment_id, Build.id == build_id)
        .execution_options(populate_existing=True)
    )
    if build is None:
        raise NotFound("unknown_build", f"no build {build_id}", build_id=str(build_id))
    return build


async def _locked_build(
    session: AsyncSession, environment_id: UUID, build_id: UUID
) -> Build:
    """The build row ``FOR NO KEY UPDATE``: the lease writers only."""
    build = await session.scalar(
        select(Build)
        .where(Build.environment_id == environment_id, Build.id == build_id)
        .with_for_update(key_share=True)
        .execution_options(populate_existing=True)
    )
    if build is None:
        raise NotFound("unknown_build", f"no build {build_id}", build_id=str(build_id))
    return build


async def _locked_wake(
    session: AsyncSession, environment_id: UUID, build_id: UUID
) -> BuildWake:
    """The build's wake row ``FOR NO KEY UPDATE`` (waited for: its holders
    hold it for one statement and wait on nothing)."""
    wake = await session.scalar(
        select(BuildWake)
        .where(
            BuildWake.environment_id == environment_id,
            BuildWake.build_id == build_id,
        )
        .with_for_update(key_share=True)
        .execution_options(populate_existing=True)
    )
    if wake is None:
        raise NotFound("unknown_build", f"no build {build_id}", build_id=str(build_id))
    return wake


async def notify(
    session: AsyncSession,
    environment_id: UUID,
    build_id: UUID,
    *,
    can_spawn: bool = True,
) -> NotifyState:
    """Set the build's wake-up flag (RUNNING builds only) and report whether
    a scheduler holds its lease.

    The caller spawns a tick unless ``scheduler_live``. So the hand-out mark
    is stamped in the flag's own transaction (a concurrent wake-candidates
    call must never see the build flagged and unstamped), and the lease is
    read **after** that commit: ``scheduler_live`` then means the lease was
    still held once the flag was durable, so its holder cannot exit without
    seeing it. If it was, the stamp is put back. Only the wake row is
    locked; the build row is read.
    """
    async with transaction(session):
        wake = await _locked_wake(session, environment_id, build_id)
        build = await _build(session, environment_id, build_id)
        now = utc_now()
        running = build.status == BuildStatus.RUNNING
        if running:
            wake.needs_tick_at = now
        previous_stamp = wake.tick_requested_at
        stamped = can_spawn and running
        if stamped:
            wake.tick_requested_at = now
    async with transaction(session):
        wake = await _locked_wake(session, environment_id, build_id)
        build = await _build(session, environment_id, build_id)
        live = _lease_live(build, utc_now())
        if live and stamped and wake.tick_requested_at == now:
            wake.tick_requested_at = previous_stamp
    return NotifyState(build_id=build_id, needs_tick=running, scheduler_live=live)


async def read_notify(
    session: AsyncSession, environment_id: UUID, build_id: UUID
) -> NotifyState:
    """The flag, one row, nothing derived (a lingering tick's poll)."""
    needs_tick_at = await session.scalar(
        select(BuildWake.needs_tick_at).where(
            BuildWake.environment_id == environment_id,
            BuildWake.build_id == build_id,
        )
    )
    if needs_tick_at is None:
        # Unflagged, or no such build: tell the two apart only then.
        await _build(session, environment_id, build_id)
    return NotifyState(build_id=build_id, needs_tick=needs_tick_at is not None)


async def clear_notify(
    session: AsyncSession, environment_id: UUID, build_id: UUID
) -> NotifyState:
    """Clear the flag: a tick does this right before computing the frontier,
    so a notify landing mid-tick re-sets it and is never lost."""
    async with transaction(session):
        wake = await _locked_wake(session, environment_id, build_id)
        wake.needs_tick_at = None
    return NotifyState(build_id=build_id, needs_tick=False)


@dataclass(frozen=True)
class WakeCandidate:
    build_id: UUID
    reactive_app_name: str


async def wake_candidates(
    session: AsyncSession,
    environment_id: UUID,
    *,
    limit: int = MAX_WAKE_CANDIDATES,
) -> list[WakeCandidate]:
    """Hand out flagged RUNNING reactive builds with no live lease, not
    handed out within :data:`WAKE_HANDOUT_WINDOW`, oldest flag first (at
    most :data:`MAX_WAKE_CANDIDATES`). Each returned build is stamped
    ``tick_requested_at`` in this transaction, its wake row taken ``SKIP
    LOCKED``, so concurrent callers get disjoint answers.

    The build row (status, lease) is read, not locked: a lease being
    acquired concurrently may not be seen yet, and the tick spawned for it
    finds the lease held and exits — one spare container, bounded by the
    hand-out window, where locking would put the build row back in the
    flagging path."""
    async with transaction(session):
        now = utc_now()
        limit = max(1, min(limit, MAX_WAKE_CANDIDATES))
        rows = (
            await session.execute(
                select(BuildWake, Build.reactive_app_name)
                .join(Build, Build.id == BuildWake.build_id)
                .where(
                    BuildWake.environment_id == environment_id,
                    BuildWake.needs_tick_at.is_not(None),
                    BuildWake.tick_requested_at.is_(None)
                    | (BuildWake.tick_requested_at < now - WAKE_HANDOUT_WINDOW),
                    Build.status == BuildStatus.RUNNING,
                    Build.reactive_app_name.is_not(None),
                    Build.scheduler_lease_until.is_(None)
                    | (Build.scheduler_lease_until <= now),
                )
                .order_by(BuildWake.needs_tick_at, BuildWake.build_id)
                .limit(limit)
                .with_for_update(of=BuildWake, key_share=True, skip_locked=True)
            )
        ).all()
        chosen: list[WakeCandidate] = []
        for wake, app_name in rows:
            wake.tick_requested_at = now
            if app_name is not None:
                chosen.append(
                    WakeCandidate(build_id=wake.build_id, reactive_app_name=app_name)
                )
        return chosen


# ---------------------------------------------------------------------------
# The scheduler lease
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LeaseState:
    build_id: UUID
    held: bool
    expires_at: datetime | None = None


def _lease_ttl(ttl_seconds: int) -> timedelta:
    if not MIN_LEASE_TTL_SECONDS <= ttl_seconds <= MAX_LEASE_TTL_SECONDS:
        raise BadRequest(
            "invalid_lease_ttl",
            f"ttl_seconds must be in [{MIN_LEASE_TTL_SECONDS},"
            f" {MAX_LEASE_TTL_SECONDS}]",
            ttl_seconds=ttl_seconds,
        )
    return timedelta(seconds=ttl_seconds)


def _owner(owner_id: str) -> str:
    # Two callers both sending "" would hold each other's lease.
    if not owner_id:
        raise BadRequest("invalid_lease_owner", "owner_id must be non-empty")
    return owner_id


async def acquire_lease(
    session: AsyncSession,
    environment_id: UUID,
    build_id: UUID,
    *,
    owner_id: str,
    ttl_seconds: int,
) -> LeaseState:
    """Take the lease if it is free, lapsed, or already this owner's (a
    retried acquire is not a lost race; owner ids are per tick)."""
    ttl, owner_id = _lease_ttl(ttl_seconds), _owner(owner_id)
    async with transaction(session):
        build = await _locked_build(session, environment_id, build_id)
        now = utc_now()
        if _lease_live(build, now) and build.scheduler_lease_owner != owner_id:
            return LeaseState(
                build_id, held=False, expires_at=build.scheduler_lease_until
            )
        build.scheduler_lease_owner = owner_id
        build.scheduler_lease_until = now + ttl
        return LeaseState(build_id, held=True, expires_at=build.scheduler_lease_until)


async def renew_lease(
    session: AsyncSession,
    environment_id: UUID,
    build_id: UUID,
    *,
    owner_id: str,
    ttl_seconds: int,
) -> LeaseState:
    """Extend the lease, for its live holder only: a tick whose lease lapsed
    and was taken over learns it here."""
    ttl, owner_id = _lease_ttl(ttl_seconds), _owner(owner_id)
    async with transaction(session):
        build = await _locked_build(session, environment_id, build_id)
        now = utc_now()
        if build.scheduler_lease_owner != owner_id or not _lease_live(build, now):
            return LeaseState(build_id, held=False)
        build.scheduler_lease_until = now + ttl
        return LeaseState(build_id, held=True, expires_at=build.scheduler_lease_until)


async def release_lease(
    session: AsyncSession, environment_id: UUID, build_id: UUID, *, owner_id: str
) -> LeaseState:
    """Drop the lease if ``owner_id`` still holds it; ``held`` reports
    whether it did (a lost tick cannot clear its successor's lease)."""
    owner_id = _owner(owner_id)
    async with transaction(session):
        build = await _locked_build(session, environment_id, build_id)
        if build.scheduler_lease_owner != owner_id:
            return LeaseState(build_id, held=False)
        build.scheduler_lease_owner = None
        build.scheduler_lease_until = None
        return LeaseState(build_id, held=True)


__all__ = [
    "MAX_WAKE_CANDIDATES",
    "WAKE_HANDOUT_WINDOW",
    "LeaseState",
    "NotifyState",
    "WakeCandidate",
    "acquire_lease",
    "clear_notify",
    "flag_after_transition",
    "notify",
    "read_notify",
    "release_lease",
    "renew_lease",
    "wake_candidates",
]
