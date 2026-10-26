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
  ``tick_requested_at``, so N concurrent askers produce one spawn. The
  stamp lasts until the window lapses or the tick it spawned releases its
  lease, whichever is first (:func:`release_lease`).

The flags live on ``build_wake`` (one row per build), not on ``build``: a
claiming start holds its build row ``FOR SHARE`` while it holds a task row,
so a flagger — itself inside a task transition — could neither wait for
the build row (the lock order is build → task; waiting would deadlock
against the claim's task lock) nor ``SKIP LOCKED`` it without losing the
wake-up of every build with a claim in flight. Flagging reads ``build``
without a lock and locks only ``build_wake`` rows.

**The invariant: news is seen by the frontier read that follows a clear,
or its flag survives the clear.** The tick clears the flag right before it
reads the frontier, so the one thing that can lose news is a clear that commits while a
transition that is news for the build is still in flight: the frontier read
misses the transition, and the clear leaves no flag. The flagger's
transaction can run on long after it flags (more transitions, the
``release_claims`` and ``skip_blocked`` loops), so the clear must not pass
it, and it must hold something the clear conflicts with even when it writes
nothing. So the lockers of a wake row, and their modes, are:

- **Flaggers** (:func:`flag_after_transition`): every target row ``FOR KEY
  SHARE``, waited for and held until commit; then ``needs_tick_at`` set on
  the still-unflagged ones ``FOR NO KEY UPDATE SKIP LOCKED``, in build-id
  order. Flaggers never wait for each other (``KEY SHARE`` is compatible
  with every mode but ``FOR UPDATE``), which matters because a flagger
  holds what it writes until commit and may wait on task rows meanwhile.
- **clear_notify**: ``FOR UPDATE``, the one mode that conflicts with ``KEY
  SHARE``, for one statement, waited for at most
  :data:`CLEAR_LOCK_TIMEOUT_MS`. Granted, it clears only with no flagger
  of the build in flight, written or skipped, so their news is committed
  before the frontier read; a flagger arriving meanwhile waits for it (it
  waits on nothing once granted) and then flags the cleared row afresh.
  Not granted in time, it leaves the flag set, which costs the tick one
  spare pass. The bound is required, not tuning: Postgres grants a ``KEY
  SHARE`` past a *queued* ``FOR UPDATE``, so overlapping flaggers (a busy
  build's own completions flag it through its limit keys) would starve an
  unbounded clear indefinitely.
- **notify**: ``FOR NO KEY UPDATE`` in both transactions, and each leaves a
  RUNNING build flagged (the second re-asserts a flag a tick cleared in
  between).
- **wake-candidates**: ``FOR NO KEY UPDATE SKIP LOCKED`` on flagged rows,
  which it leaves flagged.
- A build delete cascades the row away; nothing flags a deleted build.

So a flagger's ``SKIP LOCKED`` skips only a row whose holder leaves it
flagged, and no clear can pass the flagger before it commits. Residual
windows, all far narrower than the clear's own was:

- a skipping flagger's holder that rolls back;
- a ``wake-candidates`` whose snapshot predates a clear committing on a row
  it then locks (Postgres keeps the lock on a row the re-check drops);
- the targeting gap: a flagger's targets are read once, at its step 1, so
  a seal, reactivation or resume that makes a build hold the task and
  commits between that read and the flagger's commit leaves the flagger
  holding nothing on that build's row.

Each costs one wake-up, recovered by the next flag, ``stardag builds tick``
or the watchdog (STA-122). One deadlock is possible and detected, not
silent: a build delete's cascade holds the wake row and needs a task row
that a flagger waiting on that wake row holds. Postgres aborts one side;
the transaction retry for deadlock victims (STA-63) re-runs it.

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

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import (
    ARRAY,
    Select,
    Uuid,
    any_,
    bindparam,
    case,
    func,
    select,
    text,
    update,
)
from sqlalchemy.exc import DBAPIError
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

logger = logging.getLogger(__name__)

#: How long a build stays "handed out" after a caller was told to spawn a
#: tick for it: a second asker inside the window is told nothing.
WAKE_HANDOUT_WINDOW = timedelta(seconds=120)
#: How long a clear waits for the wake row before leaving the flag set
#: (module docstring): long enough for the brief holders, short against a
#: tick's request timeout.
CLEAR_LOCK_TIMEOUT_MS = 200
#: Postgres ``lock_not_available``: a ``lock_timeout`` expired.
_LOCK_NOT_AVAILABLE = "55P03"
#: Upper bound on one wake-candidates response.
MAX_WAKE_CANDIDATES = 20
#: A hand-out of a build flagged longer ago than this is logged as a delayed
#: wake-up: every one is a flag nobody served for that long.
DELAYED_WAKE_LOG_AFTER = timedelta(minutes=5)
#: Default and bounds of ``older_than`` for :func:`stalled_builds`.
DEFAULT_STALL_AGE = timedelta(minutes=5)
MIN_STALL_AGE_SECONDS = 0
MAX_STALL_AGE_SECONDS = 7 * 24 * 3600
#: Upper bound on one stalled-builds response.
MAX_STALLED_BUILDS = 200
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
    )
    if exclude_build_id is not None:
        targets = targets.where(BuildWake.build_id != exclude_build_id)
    # 1. Hold every target ``FOR KEY SHARE`` until commit, waited for. Only a
    #    clear conflicts with it (``FOR UPDATE``, one statement, waiting on
    #    nothing once granted), so no clear can pass this transaction before
    #    its news is committed, whether or not step 2 writes the row.
    held = list(
        (
            await session.scalars(
                targets.with_for_update(of=BuildWake, read=True, key_share=True)
            )
        ).all()
    )
    if not held:
        return
    # 2. Set the flag where it is unset, ``SKIP LOCKED``: flaggers hold the
    #    rows they write until commit and may wait on task rows after this,
    #    so they must not wait for each other. A row skipped here is held by
    #    a writer that leaves it flagged (module docstring), and an already
    #    flagged row needs no write: step 1 keeps any clear off it until this
    #    transaction ends.
    # One array parameter, not one per build: the fan-out is unbounded and
    # asyncpg caps a statement's parameters.
    held_ids = bindparam("held_ids", held, type_=ARRAY(Uuid))
    unflagged = (
        select(BuildWake.build_id)
        .where(
            BuildWake.build_id == any_(held_ids),
            BuildWake.needs_tick_at.is_(None),
        )
        .order_by(BuildWake.build_id)
        .with_for_update(key_share=True, skip_locked=True)
    )
    await session.execute(
        update(BuildWake)
        .where(BuildWake.build_id.in_(unflagged))
        # Kept from the first flag until a tick clears it: the flag's age is
        # how long the news has waited, which a re-flag must not reset (the
        # stall count and the oldest-first hand-out both read it).
        .values(needs_tick_at=func.coalesce(BuildWake.needs_tick_at, now))
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


async def _shared_build(
    session: AsyncSession, environment_id: UUID, build_id: UUID
) -> Build:
    """The build row ``FOR SHARE``: excludes the lease writers only."""
    build = await session.scalar(
        select(Build)
        .where(Build.environment_id == environment_id, Build.id == build_id)
        .with_for_update(read=True)
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
    session: AsyncSession,
    environment_id: UUID,
    build_id: UUID,
    *,
    exclusive: bool = False,
) -> BuildWake:
    """The build's wake row ``FOR NO KEY UPDATE``, or ``FOR UPDATE`` if
    ``exclusive`` (a clear: it also waits out every flagger in flight, which
    holds the row ``FOR KEY SHARE`` until its commit; module docstring)."""
    wake = await session.scalar(
        select(BuildWake)
        .where(
            BuildWake.environment_id == environment_id,
            BuildWake.build_id == build_id,
        )
        .with_for_update(key_share=not exclusive)
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
        if running and wake.needs_tick_at is None:
            # Kept from the first flag, as in ``_flag``.
            wake.needs_tick_at = now
        previous_stamp = wake.tick_requested_at
        stamped = can_spawn and running
        if stamped:
            wake.tick_requested_at = now
    async with transaction(session):
        wake = await _locked_wake(session, environment_id, build_id)
        # FOR SHARE, so the lease check and the re-stamp below are ordered
        # against every lease write (acquire, renew, release take the row FOR
        # NO KEY UPDATE): a release either committed before this read -- and
        # the re-stamp is newer than it -- or waits for this commit. Wake row,
        # then build row: nothing takes the two the other way round (the
        # lease writers never touch the wake row; flaggers SKIP LOCKED).
        build = await _shared_build(session, environment_id, build_id)
        if build.status == BuildStatus.RUNNING and wake.needs_tick_at is None:
            # Re-asserted, on the status read under this transaction's lock
            # (the build may have resumed since the first): a tick may have
            # cleared the flag between the two transactions, and a flagger
            # finding this row held now skips it on the strength of its
            # holder leaving it flagged (module docstring). At worst one
            # spare tick pass.
            wake.needs_tick_at = utc_now()
        live = _lease_live(build, utc_now())
        if stamped and wake.tick_requested_at == now:
            if live:
                wake.tick_requested_at = previous_stamp
            else:
                # Re-stamped after the lease check, so a release that
                # committed while the first stamp was in flight cannot spend
                # the hand-out made here: its tick has yet to run.
                wake.tick_requested_at = utc_now()
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
    so a notify landing mid-tick re-sets it and is never lost.

    The wake row is taken ``FOR UPDATE``, the one mode a flagger's ``FOR KEY
    SHARE`` conflicts with, so the clear never passes a flagger of this
    build still in flight, and a flagger arriving meanwhile waits for the
    clear and then sets the flag afresh (STA-122). The lock is waited for
    at most :data:`CLEAR_LOCK_TIMEOUT_MS`; if it is not granted, the flag is
    left set and ``needs_tick`` reports it (the tick ignores the answer and
    finds the flag on its next poll: one spare pass). Idempotent, so safe to
    re-run."""
    try:
        async with transaction(session):
            await session.execute(
                text(f"SET LOCAL lock_timeout = '{CLEAR_LOCK_TIMEOUT_MS}ms'")
            )
            await _clear_flag(session, environment_id, build_id)
    except DBAPIError as exc:
        if _sqlstate(exc) != _LOCK_NOT_AVAILABLE:
            raise
        return NotifyState(build_id=build_id, needs_tick=True)
    return NotifyState(build_id=build_id, needs_tick=False)


def _sqlstate(exc: DBAPIError) -> str | None:
    orig = exc.orig
    return getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)


async def _clear_flag(
    session: AsyncSession, environment_id: UUID, build_id: UUID
) -> None:
    """:func:`clear_notify`'s statement, inside the caller's transaction."""
    wake = await _locked_wake(session, environment_id, build_id, exclusive=True)
    wake.needs_tick_at = None


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
    handed out within :data:`WAKE_HANDOUT_WINDOW` (a hand-out older than the
    build's last lease release counts as spent; strictly older, so a
    hand-out stamped in the same microsecond keeps its window), oldest flag first (at
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
                    | (BuildWake.tick_requested_at < now - WAKE_HANDOUT_WINDOW)
                    | (BuildWake.tick_requested_at < Build.scheduler_lease_released_at),
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
            waited = now - wake.needs_tick_at
            if waited > DELAYED_WAKE_LOG_AFTER:
                # The measurement half of STA-123: every hand-out this late is
                # a flag nobody served, and the environment happened to drain.
                logger.warning(
                    "Delayed wake-up: build %s handed out %.0fs after it was "
                    "flagged (environment %s).",
                    wake.build_id,
                    waited.total_seconds(),
                    environment_id,
                )
            wake.tick_requested_at = now
            if app_name is not None:
                chosen.append(
                    WakeCandidate(build_id=wake.build_id, reactive_app_name=app_name)
                )
        return chosen


# ---------------------------------------------------------------------------
# Stalls
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StalledBuild:
    build_id: UUID
    reactive_app_name: str
    #: "flagged_unserved": flagged at ``since`` with no live lease now;
    #: "lease_lapsed": a lease that expired at ``since`` was never released
    #: (its tick died holding it).
    reason: str
    since: datetime


async def stalled_builds(
    session: AsyncSession,
    environment_id: UUID,
    *,
    older_than: timedelta = DEFAULT_STALL_AGE,
    limit: int = MAX_STALLED_BUILDS,
) -> list[StalledBuild]:
    """RUNNING reactive builds nobody has served for ``older_than``, oldest
    first: the residual stall risk made countable (STA-123).

    Two signals, reported separately because they recover differently:

    - **flagged_unserved** -- the flag is older than ``older_than`` and no
      lease is live. A tick that ran since would have cleared it, so no
      scheduler has looked at the build since the news arrived. Recovered
      by the next drain once any hand-out window has passed, by the
      watchdog, or by ``stardag builds tick``.
    - **lease_lapsed** -- a lease that expired ``older_than`` ago and was
      never released: its tick died holding it, with no drain and no
      hand-off. Unflagged, nothing will ever look at the build again short
      of the watchdog or a manual tick.

    A build that is both is reported once, as ``flagged_unserved``. Reads
    only; nothing is locked.
    """
    now = utc_now()
    cutoff = now - older_than
    limit = max(1, min(limit, MAX_STALLED_BUILDS))
    rows = (
        await session.execute(
            select(
                Build.id,
                Build.reactive_app_name,
                BuildWake.needs_tick_at,
                Build.scheduler_lease_owner,
                Build.scheduler_lease_until,
            )
            .join(BuildWake, BuildWake.build_id == Build.id)
            .where(
                Build.environment_id == environment_id,
                Build.status == BuildStatus.RUNNING,
                Build.reactive_app_name.is_not(None),
                Build.scheduler_lease_until.is_(None)
                | (Build.scheduler_lease_until <= now),
                (BuildWake.needs_tick_at <= cutoff)
                | (
                    Build.scheduler_lease_owner.is_not(None)
                    & (Build.scheduler_lease_until <= cutoff)
                ),
            )
            # The ``since`` each row is reported with, so a limit cuts the
            # newest stalls, never an older one classified the other way.
            .order_by(
                case(
                    (BuildWake.needs_tick_at <= cutoff, BuildWake.needs_tick_at),
                    else_=Build.scheduler_lease_until,
                ),
                Build.id,
            )
            .limit(limit)
        )
    ).all()
    stalled: list[StalledBuild] = []
    for build_id, app_name, needs_tick_at, owner, lease_until in rows:
        if needs_tick_at is not None and needs_tick_at <= cutoff:
            stalled.append(
                StalledBuild(build_id, app_name, "flagged_unserved", needs_tick_at)
            )
        elif owner is not None and lease_until is not None:
            stalled.append(
                StalledBuild(build_id, app_name, "lease_lapsed", lease_until)
            )
    return stalled


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
    whether it did (a lost tick cannot clear its successor's lease).

    A released lease also **consumes the hand-out** that spawned its tick:
    the release time is recorded on the build (``scheduler_lease_released_at``),
    and :func:`wake_candidates` treats a hand-out older than it as spent. The
    hand-out window exists to collapse the askers between a hand-out and its
    tick taking the lease into one spawn; once that tick has run and ended,
    the window has done its job, and holding it any longer only hides a flag
    that landed after the tick looked (STA-34). Without this, a build
    re-flagged within the window after its tick exited is handed out to
    nobody until the window lapses -- and then only if something else happens
    to ask, which in an environment with no other builds and no watchdog is
    never.

    Written on the build row, which this transaction already holds, and not
    on the wake row: locking the wake row here would make a concurrent
    flagger ``SKIP LOCKED`` past it and lose its flag. A tick that dies
    without releasing, or releases a lease that had already lapsed, leaves
    no release time, so its hand-out keeps the whole window, as before.
    """
    owner_id = _owner(owner_id)
    async with transaction(session):
        build = await _locked_build(session, environment_id, build_id)
        if build.scheduler_lease_owner != owner_id:
            return LeaseState(build_id, held=False)
        now = utc_now()
        # Only a release of a *live* lease is evidence the tick ran to its
        # end as the build's scheduler. A holder whose lease lapsed may
        # already have a successor handed out; spending that hand-out would
        # spawn a second one.
        if _lease_live(build, now):
            build.scheduler_lease_released_at = now
        build.scheduler_lease_owner = None
        build.scheduler_lease_until = None
    return LeaseState(build_id, held=True)


__all__ = [
    "DEFAULT_STALL_AGE",
    "MAX_STALLED_BUILDS",
    "MAX_WAKE_CANDIDATES",
    "WAKE_HANDOUT_WINDOW",
    "LeaseState",
    "NotifyState",
    "StalledBuild",
    "WakeCandidate",
    "acquire_lease",
    "clear_notify",
    "flag_after_transition",
    "notify",
    "read_notify",
    "release_lease",
    "renew_lease",
    "stalled_builds",
    "wake_candidates",
]
