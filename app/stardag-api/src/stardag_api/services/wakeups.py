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

Build rows are flagged ``FOR NO KEY UPDATE SKIP LOCKED``: a task transition
holds a task row lock, and every lifecycle path takes build then tasks, so
waiting here would invert that order. A build locked by someone else is
being moved by a lifecycle call and needs no wake-up from this write.

The scheduler lease is two owner-checked columns on ``build``: at most one
tick drives a build; a lapsed lease is taken over by the next acquire.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import Select, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.models import (
    Build,
    BuildStatus,
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
    targets = (
        select(Build.id)
        .where(
            Build.id.in_(build_ids),
            Build.environment_id == environment_id,
            Build.status == BuildStatus.RUNNING,
            Build.reactive_app_name.is_not(None),
        )
        .order_by(Build.id)
        .with_for_update(key_share=True, skip_locked=True)
    )
    if exclude_build_id is not None:
        targets = targets.where(Build.id != exclude_build_id)
    await session.execute(
        update(Build)
        .where(Build.id.in_(targets))
        .values(needs_tick_at=now)
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
    """Flag the other builds a status change of ``task_pk`` is news for.

    Every transition flags, into RUNNING included: a spurious flag costs one
    tick pass that finds nothing, collapsed by the scheduler lease, and a
    rule with no exceptions is one nobody has to re-derive. No-op when the
    status did not change.
    """
    if previous == current:
        return
    await _flag(
        session,
        environment_id,
        _holding_builds([task_pk]),
        exclude_build_id=source_build_id,
        now=now,
    )
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


async def _locked_build(
    session: AsyncSession, environment_id: UUID, build_id: UUID
) -> Build:
    build = await session.scalar(
        select(Build)
        .where(Build.environment_id == environment_id, Build.id == build_id)
        .with_for_update(key_share=True)
        .execution_options(populate_existing=True)
    )
    if build is None:
        raise NotFound("unknown_build", f"no build {build_id}", build_id=str(build_id))
    return build


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
    seeing it. If it was, the stamp is put back.
    """
    async with transaction(session):
        build = await _locked_build(session, environment_id, build_id)
        now = utc_now()
        running = build.status == BuildStatus.RUNNING
        if running:
            build.needs_tick_at = now
        previous_stamp = build.tick_requested_at
        stamped = can_spawn and running
        if stamped:
            build.tick_requested_at = now
    async with transaction(session):
        build = await _locked_build(session, environment_id, build_id)
        live = _lease_live(build, utc_now())
        if live and stamped and build.tick_requested_at == now:
            build.tick_requested_at = previous_stamp
    return NotifyState(build_id=build_id, needs_tick=running, scheduler_live=live)


async def read_notify(
    session: AsyncSession, environment_id: UUID, build_id: UUID
) -> NotifyState:
    """The flag, one row, nothing derived (a lingering tick's poll)."""
    build = await session.scalar(
        select(Build).where(
            Build.environment_id == environment_id, Build.id == build_id
        )
    )
    if build is None:
        raise NotFound("unknown_build", f"no build {build_id}", build_id=str(build_id))
    return NotifyState(build_id=build_id, needs_tick=build.needs_tick_at is not None)


async def clear_notify(
    session: AsyncSession, environment_id: UUID, build_id: UUID
) -> NotifyState:
    """Clear the flag: a tick does this right before computing the frontier,
    so a notify landing mid-tick re-sets it and is never lost."""
    async with transaction(session):
        build = await _locked_build(session, environment_id, build_id)
        build.needs_tick_at = None
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
    ``tick_requested_at`` in this transaction, rows taken ``SKIP LOCKED``,
    so concurrent callers get disjoint answers."""
    async with transaction(session):
        now = utc_now()
        limit = max(1, min(limit, MAX_WAKE_CANDIDATES))
        chosen = (
            await session.scalars(
                select(Build)
                .where(
                    Build.environment_id == environment_id,
                    Build.status == BuildStatus.RUNNING,
                    Build.reactive_app_name.is_not(None),
                    Build.needs_tick_at.is_not(None),
                    Build.tick_requested_at.is_(None)
                    | (Build.tick_requested_at < now - WAKE_HANDOUT_WINDOW),
                    Build.scheduler_lease_until.is_(None)
                    | (Build.scheduler_lease_until <= now),
                )
                .order_by(Build.needs_tick_at, Build.id)
                .limit(limit)
                .with_for_update(key_share=True, skip_locked=True)
            )
        ).all()
        for build in chosen:
            build.tick_requested_at = now
        return [
            WakeCandidate(build_id=b.id, reactive_app_name=b.reactive_app_name)
            for b in chosen
            if b.reactive_app_name is not None
        ]


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
