"""Blocked and given-up members: skip-blocked and the exclusion cascade.

See design.md, "The runnable rule" (skip-blocked, exclusion, a failed
discovery job) and the ``plan_member`` entity. Both walk **instance
edges within one plan** — edges belong to the instance, membership to the
plan, so a walk only ever steps between members.

- **Skip-blocked** (v1's recursive walk, re-keyed): seeds are the active
  plan's non-excluded members whose task is FAILED, CANCELLED or SKIPPED;
  the walk goes downstream through members in {FAILED, CANCELLED, SKIPPED,
  PENDING, SUSPENDED, INTERRUPTED} (a COMPLETED or RUNNING one stops it),
  and the PENDING, SUSPENDED and INTERRUPTED members it reaches become
  SKIPPED through ``transition_task()``, in ``task_id`` order.
- **Exclusion** ("given up on", STA-104) is per plan and never touches the
  global status: an excluded member is not scheduled, is never a discovery
  job again, and does not gate the build's completion. It cascades
  (``upstream_excluded``) to every downstream member that is not COMPLETED
  and has an excluded upstream that is not COMPLETED — otherwise that
  downstream would be neither runnable nor excluded. A COMPLETED member
  blocks nobody, so the cascade neither passes through nor starts from one.
  An excluded root fails the build: the request cannot be met. The result
  names the roots *this* call excluded, and whether it failed the build.
- A **failed discovery job** (the class cannot be imported, or
  ``requires()`` raised) is an exclusion with ``discovery_failed`` and the
  error: a property of this plan's code, not of the promise.

Both lock the build row first (``FOR NO KEY UPDATE``), then task rows in
``task_id`` order, as every lifecycle path does.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from sqlalchemy import String, Uuid, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.models import (
    EventType,
    ExclusionReason,
    Plan,
    PlanMember,
    Task,
)
from stardag_api.models.base import utc_now
from stardag_api.services import event_log
from stardag_api.services.builds import active_plan, fail_locked_build
from stardag_api.services.errors import Conflict
from stardag_api.services.event_log import EventClock
from stardag_api.services.registration import get_plan, lock_build
from stardag_api.services.transitions import (
    Transition,
    member_task_pk,
    transition_task,
)
from stardag_api.services.tx import transaction

#: Statuses that block a downstream and that the skip-blocked walk passes
#: through.
_BLOCKING = ("failed", "cancelled", "skipped")
_PASSABLE = (*_BLOCKING, "pending", "suspended", "interrupted")
#: What the walk skips.
_SKIPPABLE = ("pending", "suspended", "interrupted")


@dataclass(frozen=True)
class SkipBlockedResult:
    plan_id: UUID | None
    #: Task ids moved to SKIPPED, in ``task_id`` order.
    skipped: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ExclusionResult:
    plan_id: UUID
    #: Task ids excluded by this call (the member first, then its cascade,
    #: in ``task_id`` order); empty on a re-delivery.
    excluded: list[str] = field(default_factory=list)
    #: The plan's roots this call excluded (the member itself, or reached by
    #: its cascade), in ``task_id`` order; empty when this call reached none.
    roots_excluded: list[str] = field(default_factory=list)
    #: This call failed the build (it excluded a root of a build that was not
    #: already terminal).
    build_failed: bool = False


# ---------------------------------------------------------------------------
# skip-blocked
# ---------------------------------------------------------------------------

_BLOCKED_MEMBERS = text(
    f"""
    WITH RECURSIVE blocked(instance_id) AS (
        SELECT m.instance_id
        FROM plan_member m JOIN task t ON t.id = m.task_pk
        WHERE m.plan_id = :plan AND m.excluded_at IS NULL
          AND t.status::text IN ({", ".join(repr(s) for s in _BLOCKING)})
        UNION
        SELECT d.instance_id
        FROM blocked b
        JOIN task_instance_dependency e ON e.upstream_instance_id = b.instance_id
        JOIN plan_member d
          ON d.plan_id = :plan AND d.instance_id = e.downstream_instance_id
        JOIN task t ON t.id = d.task_pk
        WHERE d.excluded_at IS NULL
          AND t.status::text IN ({", ".join(repr(s) for s in _PASSABLE)})
    )
    SELECT t.task_id, t.id AS task_pk
    FROM blocked b
    JOIN plan_member m ON m.plan_id = :plan AND m.instance_id = b.instance_id
    JOIN task t ON t.id = m.task_pk
    WHERE t.status::text IN ({", ".join(repr(s) for s in _SKIPPABLE)})
    ORDER BY t.task_id
    """
).columns(task_id=String, task_pk=Uuid)


async def skip_blocked(
    session: AsyncSession, environment_id: UUID, build_id: UUID
) -> SkipBlockedResult:
    """Skip the active plan's members blocked by a failed, cancelled or
    skipped upstream, in one transaction. Idempotent: a re-delivery finds
    them SKIPPED and skips nothing."""
    async with transaction(session):
        build = await lock_build(session, environment_id, build_id)
        plan = await active_plan(session, build.id)
        if plan is None:
            return SkipBlockedResult(plan_id=None)
        rows = (await session.execute(_BLOCKED_MEMBERS, {"plan": plan.id})).all()
        clock = EventClock(utc_now())
        skipped = []
        for row in rows:
            try:
                outcome = await transition_task(
                    session,
                    environment_id,
                    task_pk=row.task_pk,
                    plan_id=plan.id,
                    transition=Transition.skip(reason="upstream_blocked"),
                    now=clock.tick(),
                )
            except Conflict:
                # Moved since the walk read it (claimed, or completed by an
                # observation); the next pass sees its new state.
                continue
            if outcome.applied:
                skipped.append(row.task_id)
        return SkipBlockedResult(plan_id=plan.id, skipped=skipped)


# ---------------------------------------------------------------------------
# exclusion
# ---------------------------------------------------------------------------

_EXCLUSION_CASCADE = text(
    """
    WITH RECURSIVE excluded(instance_id) AS (
        SELECT m.instance_id
        FROM plan_member m JOIN task t ON t.id = m.task_pk
        WHERE m.plan_id = :plan AND m.excluded_at IS NOT NULL
          AND t.status::text <> 'completed'
        UNION
        SELECT d.instance_id
        FROM excluded x
        JOIN task_instance_dependency e ON e.upstream_instance_id = x.instance_id
        JOIN plan_member d
          ON d.plan_id = :plan AND d.instance_id = e.downstream_instance_id
        JOIN task t ON t.id = d.task_pk
        WHERE t.status::text <> 'completed'
    )
    SELECT t.task_id, m.task_pk
    FROM excluded x
    JOIN plan_member m ON m.plan_id = :plan AND m.instance_id = x.instance_id
    JOIN task t ON t.id = m.task_pk
    WHERE m.excluded_at IS NULL
    ORDER BY t.task_id
    """
).columns(task_id=String, task_pk=Uuid)


#: Every member reachable downstream, within the plan, from ``:seed`` or an
#: already-excluded member — whatever the statuses. A superset of what the
#: cascade can reach, so it can be locked before the cascade reads any
#: status.
_EXCLUSION_REACH = text(
    """
    WITH RECURSIVE reach(instance_id) AS (
        SELECT m.instance_id
        FROM plan_member m
        WHERE m.plan_id = :plan
          AND (m.task_pk = :seed OR m.excluded_at IS NOT NULL)
        UNION
        SELECT d.instance_id
        FROM reach r
        JOIN task_instance_dependency e ON e.upstream_instance_id = r.instance_id
        JOIN plan_member d
          ON d.plan_id = :plan AND d.instance_id = e.downstream_instance_id
    )
    SELECT m.task_pk
    FROM reach r
    JOIN plan_member m ON m.plan_id = :plan AND m.instance_id = r.instance_id
    """
).columns(task_pk=Uuid)


async def _lock_reach(
    session: AsyncSession, environment_id: UUID, plan_id: UUID, seed: UUID
) -> None:
    """Lock the task rows the cascade may read, ``FOR NO KEY UPDATE`` in
    ``task_id`` order (after the build: build → task), so that no claim,
    report or observation moves one of them between the cascade's status
    read and the membership update — an excluded member cannot be claimed
    once the exclusion commits, nor a completion be excluded behind it."""
    pks = (
        await session.scalars(_EXCLUSION_REACH, {"plan": plan_id, "seed": seed})
    ).all()
    await session.execute(
        select(Task.id)
        .where(Task.environment_id == environment_id, Task.id.in_(pks))
        .order_by(Task.task_id)
        .with_for_update(key_share=True)
    )


async def exclude_member(
    session: AsyncSession,
    environment_id: UUID,
    *,
    plan_id: UUID,
    task_id: str,
    reason: ExclusionReason = ExclusionReason.OPERATOR,
    error_message: str | None = None,
    note: str | None = None,
) -> ExclusionResult:
    """Give up on a member of ``plan_id`` (``operator`` or
    ``discovery_failed``), cascade ``upstream_excluded`` downstream within
    the plan, and fail the build if a root is excluded — one transaction.

    ``error_message`` is a failed discovery job's error; ``note`` an
    operator's reason, recorded on the ``TASK_EXCLUDED`` event.

    Refused on a superseded plan (409 ``plan_superseded``): what a
    superseded request gives up on is moot. Idempotent by state: an
    already-excluded member is left as it is and nothing is written.
    """
    if reason is ExclusionReason.UPSTREAM_EXCLUDED:
        raise ValueError("upstream_excluded is written by the cascade only")
    async with transaction(session):
        plan = await get_plan(session, environment_id, plan_id)
        build = await lock_build(session, environment_id, plan.build_id)
        await session.refresh(plan)
        if plan.superseded_at is not None:
            raise Conflict(
                "plan_superseded",
                "the plan is superseded; exclusions apply to the build's"
                " current request",
                plan_id=str(plan.id),
            )
        task_pk = await member_task_pk(session, environment_id, plan.id, task_id)
        await _lock_reach(session, environment_id, plan.id, task_pk)
        clock = EventClock(utc_now())
        excluded: list[str] = []
        first = await _exclude(session, plan, [task_pk], reason, at=clock.now)
        if first:
            excluded.append(task_id)
            await _record(
                session,
                environment_id,
                plan,
                [(task_pk, None)],
                reason,
                clock,
                error_message=error_message,
                note=note,
            )
            cascade = (
                await session.execute(_EXCLUSION_CASCADE, {"plan": plan.id})
            ).all()
            pks = [row.task_pk for row in cascade]
            if pks:
                await _exclude(
                    session,
                    plan,
                    pks,
                    ExclusionReason.UPSTREAM_EXCLUDED,
                    at=clock.now,
                )
                await _record(
                    session,
                    environment_id,
                    plan,
                    [(pk, task_id) for pk in pks],
                    ExclusionReason.UPSTREAM_EXCLUDED,
                    clock,
                )
                excluded.extend(row.task_id for row in cascade)

        roots = set(
            (
                await session.scalars(
                    select(Task.task_id)
                    .join(PlanMember, PlanMember.task_pk == Task.id)
                    .where(PlanMember.plan_id == plan.id, PlanMember.is_root)
                )
            ).all()
        )
        roots_now = sorted(roots.intersection(excluded))
        build_failed = False
        if roots_now:
            build_failed = await fail_locked_build(
                session,
                build,
                at=clock.tick(),
                error_message=(
                    f"root_excluded: the build's request cannot be met; root(s)"
                    f" {', '.join(roots_now)} excluded after {task_id} was excluded"
                    f" ({reason.value})"
                    + (f": {error_message}" if error_message else "")
                ),
                metadata={
                    "reason": "root_excluded",
                    "plan_id": str(plan.id),
                    "root_task_ids": roots_now,
                    "excluded_task_id": task_id,
                },
            )
        return ExclusionResult(
            plan_id=plan.id,
            excluded=excluded,
            roots_excluded=roots_now,
            build_failed=build_failed,
        )


async def _exclude(
    session: AsyncSession,
    plan: Plan,
    task_pks: list[UUID],
    reason: ExclusionReason,
    *,
    at: datetime,
) -> bool:
    """Set ``excluded_at``/``excluded_reason`` on members not yet excluded;
    True if any row changed."""
    changed = await session.execute(
        update(PlanMember)
        .where(
            PlanMember.plan_id == plan.id,
            PlanMember.task_pk.in_(task_pks),
            PlanMember.excluded_at.is_(None),
        )
        .values(excluded_at=at, excluded_reason=reason)
        .returning(PlanMember.task_pk)
    )
    return bool(changed.all())


async def _record(
    session: AsyncSession,
    environment_id: UUID,
    plan: Plan,
    rows: list[tuple[UUID, str | None]],
    reason: ExclusionReason,
    clock: EventClock,
    *,
    error_message: str | None = None,
    note: str | None = None,
) -> None:
    await event_log.append(
        session,
        [
            event_log.event_row(
                environment_id,
                EventType.TASK_EXCLUDED,
                at=clock.tick(),
                build_id=plan.build_id,
                task_pk=task_pk,
                plan_id=plan.id,
                error_message=error_message,
                metadata={
                    "reason": reason.value,
                    **({"excluded_by": origin} if origin else {}),
                    **({"note": note} if note else {}),
                },
            )
            for task_pk, origin in rows
        ],
    )
