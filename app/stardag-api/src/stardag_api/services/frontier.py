"""The frontier of a build's active plan: what can run, what needs
discovery, what is running. See design.md, "The runnable rule".

For a member ``m`` of the active plan, with instance ``i`` and task ``t``::

    ACTIONABLE       := {PENDING, SUSPENDED, INTERRUPTED, CANCELLED, SKIPPED}
                        ∪ {RUNNING with claim_expires_at <= now}
    discovery_job(m) := t.status <> COMPLETED AND m.excluded_at IS NULL
                        AND i.expanded_at IS NULL
    runnable(m)      := t.status ∈ ACTIONABLE AND m.excluded_at IS NULL
                        AND i.expanded_at IS NOT NULL
                        AND NOT EXISTS edge(u -> i) WITH u.task.status <> COMPLETED
    running(m)       := t.status = RUNNING AND claim live

evaluated after the closure step, in the same transaction. The frontier is
a hint; a claiming start re-checks the predicate under its lock.

Only a RUNNING build has work to hand out: for any other status (a closure
conflict found in this very call fails the build) ``runnable`` and
``discovery_jobs`` are empty and ``build_status`` says why. ``running``
stays, as a diagnostic of claims still live. A claiming start refuses a
build that is not RUNNING on its own (409 ``build_not_running``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.models import (
    BuildStatus,
    Plan,
    PlanMember,
    Task,
    TaskInstance,
    TaskInstanceDependency,
    TaskStatus,
)
from stardag_api.models.base import utc_now
from stardag_api.services.plans import ClosureResult, close_plan
from stardag_api.services.registration import lock_build
from stardag_api.services.transitions import ACTIONABLE_STATUSES
from stardag_api.services.tx import transaction

__all__ = ["ACTIONABLE_STATUSES", "Frontier", "FrontierMember", "get_frontier"]


@dataclass(frozen=True)
class FrontierMember:
    task_id: str
    task_pk: UUID
    instance_id: UUID
    instance_hash: str
    status: TaskStatus
    is_root: bool
    body: dict[str, Any]


@dataclass(frozen=True)
class Frontier:
    build_id: UUID
    #: The active plan, or None when the build has none yet.
    plan_id: UUID | None
    deployment_id: UUID | None
    settings_hash: str | None
    sealed: bool
    runnable: list[FrontierMember] = field(default_factory=list)
    discovery_jobs: list[FrontierMember] = field(default_factory=list)
    running: list[FrontierMember] = field(default_factory=list)
    #: Diagnostic: sealed, and every non-excluded member COMPLETED.
    plan_complete: bool = False
    #: The closure step's outcome (a conflict fails the build).
    closure: ClosureResult | None = None
    build_status: BuildStatus | None = None


async def get_frontier(
    session: AsyncSession, environment_id: UUID, build_id: UUID
) -> Frontier:
    """The closure step, then the three predicates over the active plan.

    One transaction: the closure step's admissions (and a build failed over
    a closure conflict) commit with the read.
    """
    async with transaction(session):
        # The build row first (lock order: build → plan → task rows), in
        # the mode the closure step needs, before choosing the active plan:
        # a seal holding it may be switching which plan that is.
        build = await lock_build(session, environment_id, build_id)
        plan = await session.scalar(
            select(Plan).where(
                Plan.build_id == build_id,
                Plan.activated_at.is_not(None),
                Plan.superseded_at.is_(None),
            )
        )
        if plan is None:
            return Frontier(
                build_id=build_id,
                plan_id=None,
                deployment_id=None,
                settings_hash=None,
                sealed=False,
                build_status=build.status,
            )
        closure = await close_plan(session, environment_id, plan, now=utc_now())
        now = utc_now()
        rows = (await session.execute(_members_query(plan.id))).all()

        runnable, discovery, running = [], [], []
        plan_complete = plan.sealed_at is not None
        for row in rows:
            if row.excluded_at is not None:
                continue
            status = row.status
            if status != TaskStatus.COMPLETED:
                plan_complete = False
            live = (
                status == TaskStatus.RUNNING
                and row.claim_expires_at is not None
                and row.claim_expires_at > now
            )
            actionable = status in ACTIONABLE_STATUSES or (
                status == TaskStatus.RUNNING and not live
            )
            if live:
                running.append(row)
            elif row.expanded_at is None:
                if status != TaskStatus.COMPLETED:
                    discovery.append(row)
            elif actionable and not row.blocked:
                runnable.append(row)

        await session.refresh(build)
        if build.status != BuildStatus.RUNNING:
            runnable, discovery = [], []
        bodies = await _bodies(
            session, [r.instance_id for r in (*runnable, *discovery, *running)]
        )
        return Frontier(
            build_id=build_id,
            plan_id=plan.id,
            deployment_id=plan.deployment_id,
            settings_hash=plan.settings_hash,
            sealed=plan.sealed_at is not None,
            runnable=[_member(r, bodies) for r in runnable],
            discovery_jobs=[_member(r, bodies) for r in discovery],
            running=[_member(r, bodies) for r in running],
            plan_complete=plan_complete,
            closure=closure,
            build_status=build.status,
        )


def _members_query(plan_id: UUID):
    upstream_instance = TaskInstance.__table__.alias("upstream_instance")
    upstream_task = Task.__table__.alias("upstream_task")
    blocked = exists().where(
        TaskInstanceDependency.downstream_instance_id == TaskInstance.id,
        upstream_instance.c.id == TaskInstanceDependency.upstream_instance_id,
        upstream_task.c.id == upstream_instance.c.task_pk,
        upstream_task.c.status != TaskStatus.COMPLETED,
    )
    return (
        select(
            Task.task_id,
            PlanMember.task_pk,
            PlanMember.instance_id,
            PlanMember.is_root,
            PlanMember.excluded_at,
            TaskInstance.instance_hash,
            TaskInstance.expanded_at,
            Task.status,
            Task.claim_expires_at,
            blocked.label("blocked"),
        )
        .select_from(PlanMember)
        .join(TaskInstance, TaskInstance.id == PlanMember.instance_id)
        .join(Task, Task.id == PlanMember.task_pk)
        .where(PlanMember.plan_id == plan_id)
        .order_by(Task.task_id)
    )


async def _bodies(
    session: AsyncSession, instance_ids: list[UUID]
) -> dict[UUID, dict[str, Any]]:
    if not instance_ids:
        return {}
    rows = await session.execute(
        select(TaskInstance.id, TaskInstance.body).where(
            TaskInstance.id.in_(instance_ids)
        )
    )
    return {instance_id: body for instance_id, body in rows.tuples()}


def _member(row: Any, bodies: dict[UUID, dict[str, Any]]) -> FrontierMember:
    return FrontierMember(
        task_id=row.task_id,
        task_pk=row.task_pk,
        instance_id=row.instance_id,
        instance_hash=row.instance_hash,
        status=row.status,
        is_root=row.is_root,
        body=bodies[row.instance_id],
    )
