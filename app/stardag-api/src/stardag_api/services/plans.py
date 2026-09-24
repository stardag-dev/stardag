"""Plan-level operations of the static path: seal and closure.

See design.md, "Registration" (``/seal``, "Closure is kept as a
mechanism") and "Rollover" (the seal's deployment check).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, NoReturn
from uuid import UUID

from sqlalchemy import String, Uuid, func, select, text, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.models import (
    AdmittedBy,
    Plan,
    PlanMember,
    Task,
    TaskInstance,
    TaskStatus,
)
from stardag_api.models.base import utc_now
from stardag_api.services import event_log
from stardag_api.services.deployments import verify_deployment_current
from stardag_api.services.builds import fail_build_for_conflicts
from stardag_api.services.errors import Conflict, RecordedConflict
from stardag_api.services.event_log import EventClock
from stardag_api.services.registration import PlanState, get_plan, lock_build
from stardag_api.services.registration_chunk import admit_members, differing_fields
from stardag_api.services.tx import transaction


@dataclass(frozen=True)
class ClosureConflict:
    """Two instances of one completion that one plan cannot both hold."""

    task_id: str
    member_instance_id: UUID
    other_instance_id: UUID
    fields: list[str]


@dataclass(frozen=True)
class ClosureResult:
    admitted: int
    conflicts: list[ClosureConflict] = field(default_factory=list)
    #: The closure found a conflict and the build is failed (BUILD_FAILED).
    build_failed: bool = False


# ---------------------------------------------------------------------------
# seal
# ---------------------------------------------------------------------------


async def seal_plan(
    session: AsyncSession, environment_id: UUID, plan_id: UUID
) -> PlanState:
    """Run the closure step, verify the static phase is fully stated, then
    seal (and, for a replacement, activate and supersede) in one
    transaction.

    Closure first: an edge another plan added from a shared instance to an
    instance this plan does not hold must not make a correct seal fail. A
    conflict the closure finds fails the build (committed) and the seal is
    refused with ``instance_conflict``.

    Idempotent by state: a sealed plan is returned unchanged — after the
    closure step, which a retried seal runs too, so a member another plan's
    expansion made reachable since the seal is admitted (or its conflict
    fails the build) either way.
    """
    async with transaction(session):
        plan = await get_plan(session, environment_id, plan_id)
        await lock_build(session, environment_id, plan.build_id)
        await session.refresh(plan)

        closed = await close_plan(session, environment_id, plan, now=utc_now())
        if closed.build_failed:
            refuse_closure_conflict(plan, closed)
        if plan.sealed_at is not None:
            return PlanState.of(plan)
        await _verify_registration(session, plan)
        await verify_deployment_current(session, environment_id, plan.deployment_id)
        higher = await session.scalar(
            select(func.count())
            .select_from(Plan)
            .where(Plan.build_id == plan.build_id, Plan.generation > plan.generation)
        )
        if higher:
            raise Conflict(
                "plan_superseded",
                "a later request for this build exists; the latest request wins",
                plan_id=str(plan.id),
            )

        now = utc_now()
        plan.sealed_at = now
        if plan.activated_at is None:
            # Supersede first: the one-active-plan index is checked per
            # statement.
            await session.execute(
                update(Plan)
                .where(
                    Plan.build_id == plan.build_id,
                    Plan.id != plan.id,
                    Plan.activated_at.is_not(None),
                    Plan.superseded_at.is_(None),
                )
                .values(superseded_at=now)
            )
            plan.activated_at = now
        await session.flush()
        return PlanState.of(plan)


def refuse_closure_conflict(plan: Plan, closed: ClosureResult) -> NoReturn:
    """The refusal of a call whose closure step failed the build: 409
    ``instance_conflict``, recorded (the ``BUILD_FAILED`` is committed)."""
    raise RecordedConflict(
        "instance_conflict",
        "the closure step reached a second instance of a member's"
        " completion; the build is failed",
        plan_id=str(plan.id),
        conflicts=[
            {"task_id": c.task_id, "fields": c.fields} for c in closed.conflicts
        ],
    )


async def _verify_registration(session: AsyncSession, plan: Plan) -> None:
    roots = (
        await session.scalars(
            select(Task.task_id)
            .select_from(PlanMember)
            .join(TaskInstance, TaskInstance.id == PlanMember.instance_id)
            .join(Task, Task.id == PlanMember.task_pk)
            .where(
                PlanMember.plan_id == plan.id,
                PlanMember.is_root,
                TaskInstance.expanded_at.is_(None),
                Task.status != TaskStatus.COMPLETED,
            )
            .order_by(Task.task_id)
        )
    ).all()
    if roots:
        raise Conflict(
            "plan_incomplete_registration",
            "roots are neither expanded nor COMPLETED",
            reason="roots_unexpanded",
            task_ids=list(roots),
        )
    open_edges = (
        await session.execute(
            text(
                "SELECT e.downstream_instance_id, e.upstream_instance_id"
                " FROM plan_member m"
                " JOIN task_instance_dependency e"
                "   ON e.downstream_instance_id = m.instance_id"
                " WHERE m.plan_id = :plan AND NOT EXISTS ("
                "   SELECT 1 FROM plan_member u WHERE u.plan_id = :plan"
                "   AND u.instance_id = e.upstream_instance_id)"
                " ORDER BY 1, 2 LIMIT 20"
            ),
            {"plan": plan.id},
        )
    ).all()
    if open_edges:
        raise Conflict(
            "plan_incomplete_registration",
            "an edge from a member reaches an instance that is not a member",
            reason="closure_open",
            edges=[[str(d), str(u)] for d, u in open_edges],
        )


# ---------------------------------------------------------------------------
# closure
# ---------------------------------------------------------------------------

_REACHABLE_NON_MEMBERS = text(
    """
    WITH RECURSIVE reach(instance_id) AS (
        SELECT m.instance_id FROM plan_member m WHERE m.plan_id = :plan
        UNION
        SELECT e.upstream_instance_id
        FROM task_instance_dependency e
        JOIN reach r ON e.downstream_instance_id = r.instance_id
    )
    SELECT t.task_id, i.task_pk, i.id AS instance_id, i.body
    FROM reach r
    JOIN task_instance i ON i.id = r.instance_id
    JOIN task t ON t.id = i.task_pk
    WHERE NOT EXISTS (
        SELECT 1 FROM plan_member m
        WHERE m.plan_id = :plan AND m.instance_id = r.instance_id
    )
    ORDER BY t.task_id, i.instance_hash
    """
).columns(task_id=String, task_pk=Uuid, instance_id=Uuid, body=JSONB)


async def closure(
    session: AsyncSession, environment_id: UUID, plan_id: UUID
) -> ClosureResult:
    """Admit every instance reachable over edges from the plan's members
    that is not yet a member, whatever its status (``admitted_by =
    closure``), so the frontier never gates on a member it does not hold.

    A completion reached under an instance other than the one the plan
    holds cannot be admitted: the build is failed naming both instances
    (``BUILD_FAILED``), and the conflicts are returned.
    """
    async with transaction(session):
        plan = await get_plan(session, environment_id, plan_id)
        return await close_plan(session, environment_id, plan, now=utc_now())


async def close_plan(
    session: AsyncSession, environment_id: UUID, plan: Plan, *, now: datetime
) -> ClosureResult:
    """:func:`closure` inside the caller's transaction.

    Takes the build row lock (``FOR NO KEY UPDATE``, as plan creation and
    sealing do) before reading or admitting anything, so the lock order is
    the registration one — build, then plan, then task rows. Admitting
    first and locking the build only to fail it over a conflict would hold
    ``plan_member`` inserts while waiting for a lock that a plan retry
    holds while it waits on those same rows.
    """
    await lock_build(session, environment_id, plan.build_id)
    reached = (await session.execute(_REACHABLE_NON_MEMBERS, {"plan": plan.id})).all()
    if not reached:
        return ClosureResult(admitted=0)
    held = {
        task_pk: (instance_id, body)
        for task_pk, instance_id, body in (
            await session.execute(
                select(PlanMember.task_pk, PlanMember.instance_id, TaskInstance.body)
                .join(TaskInstance, TaskInstance.id == PlanMember.instance_id)
                .where(
                    PlanMember.plan_id == plan.id,
                    PlanMember.task_pk.in_({r.task_pk for r in reached}),
                )
            )
        ).tuples()
    }
    by_task: dict[UUID, list[Any]] = {}
    for row in reached:
        by_task.setdefault(row.task_pk, []).append(row)

    conflicts: list[ClosureConflict] = []
    admit: list[tuple[str, UUID, UUID, Mapping[str, Any]]] = []
    for task_pk, rows in by_task.items():
        first = rows[0]
        if task_pk in held:
            member_instance, member_body = held[task_pk]
            for row in rows:
                conflicts.append(
                    ClosureConflict(
                        task_id=row.task_id,
                        member_instance_id=member_instance,
                        other_instance_id=row.instance_id,
                        fields=differing_fields(member_body, row.body),
                    )
                )
        elif len(rows) > 1:
            for row in rows[1:]:
                conflicts.append(
                    ClosureConflict(
                        task_id=row.task_id,
                        member_instance_id=first.instance_id,
                        other_instance_id=row.instance_id,
                        fields=differing_fields(first.body, row.body),
                    )
                )
        else:
            admit.append((first.task_id, task_pk, first.instance_id, first.body))

    clock = EventClock(now)
    admitted, events = await admit_members(
        session, environment_id, plan, admit, AdmittedBy.CLOSURE, clock=clock
    )
    await event_log.append(session, events)
    build_failed = False
    if conflicts:
        build_failed = await fail_build_for_conflicts(
            session, environment_id, plan, conflicts, at=clock.tick()
        )
    return ClosureResult(
        admitted=admitted, conflicts=conflicts, build_failed=build_failed
    )
