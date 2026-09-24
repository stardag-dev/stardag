"""Reads of plans: one plan with its counts, a build's plans, a plan's
roots, and a plan's graph.

- ``GET /plans/{id}`` (:func:`get_plan_detail`) and ``GET
  /builds/{id}/plans`` (:func:`list_build_plans`, newest generation first):
  the plan's lifecycle timestamps, its scope (the deployment, resolved, and
  the settings hash) and its member counts by task status. Excluded members
  are counted apart, not under their status: they are given up on and gate
  nothing (design.md, ``plan_member``).
- ``GET /plans/{id}/roots`` (:func:`plan_roots`): the plan's root members
  with their instance bodies, which a rolling-over tick rehydrates under its
  own code and re-hashes against ``build.root_task_ids`` (design.md,
  "Rollover", step 2).
- ``GET /plans/{id}/graph`` (:func:`plan_graph`): every member (closure
  admissions included) with its task's identity and global status, and the
  instance edges between member instances, static and dynamic. Edges belong
  to the scope, not the plan, so an edge to an instance the plan does not
  hold (a closure step not yet run) is not in the graph; the closure step
  runs at the next frontier read or seal.

Plain reads: no locks, no writes. The graph is one response, not paged:
the view it serves draws the whole plan.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.models import (
    AdmittedBy,
    ExclusionReason,
    Plan,
    PlanMember,
    Task,
    TaskInstance,
    TaskInstanceDependency,
    TaskStatus,
)
from stardag_api.services.builds import get_build
from stardag_api.services.deployments import DeploymentState, get_deployments
from stardag_api.services.frontier import FrontierMember, attempt_counts
from stardag_api.services.registration import get_plan


@dataclass(frozen=True)
class PlanDetail:
    id: UUID
    build_id: UUID
    deployment_id: UUID
    deployment: DeploymentState
    settings_hash: str
    generation: int
    created_at: datetime
    activated_at: datetime | None
    sealed_at: datetime | None
    superseded_at: datetime | None
    #: Activated and not superseded: the build's one active plan.
    is_active: bool
    member_count: int
    root_count: int
    excluded_count: int
    #: Non-excluded members by their task's global status.
    member_counts: dict[TaskStatus, int] = field(default_factory=dict)


async def _counts(
    session: AsyncSession, plan_ids: list[UUID]
) -> dict[UUID, tuple[int, int, int, dict[TaskStatus, int]]]:
    """Per plan: (members, roots, excluded, non-excluded by status)."""
    rows = await session.execute(
        select(
            PlanMember.plan_id,
            Task.status,
            PlanMember.excluded_at.is_not(None).label("excluded"),
            func.count(),
            func.count().filter(PlanMember.is_root.is_(True)),
        )
        .join(Task, Task.id == PlanMember.task_pk)
        .where(PlanMember.plan_id.in_(plan_ids))
        .group_by(PlanMember.plan_id, Task.status, "excluded")
    )
    out: dict[UUID, tuple[int, int, int, dict[TaskStatus, int]]] = {}
    for plan_id, status, excluded, n, roots in rows.tuples():
        members, root_count, excluded_count, by_status = out.get(plan_id, (0, 0, 0, {}))
        by_status = Counter(by_status)
        if excluded:
            excluded_count += n
        else:
            by_status[status] += n
        out[plan_id] = (members + n, root_count + roots, excluded_count, by_status)
    return out


async def _details(
    session: AsyncSession, environment_id: UUID, plans: list[Plan]
) -> list[PlanDetail]:
    counts = await _counts(session, [p.id for p in plans])
    deployments = await get_deployments(
        session, environment_id, [p.deployment_id for p in plans]
    )
    out = []
    for plan in plans:
        members, roots, excluded, by_status = counts.get(plan.id, (0, 0, 0, {}))
        out.append(
            PlanDetail(
                id=plan.id,
                build_id=plan.build_id,
                deployment_id=plan.deployment_id,
                deployment=deployments[plan.deployment_id],
                settings_hash=plan.settings_hash,
                generation=plan.generation,
                created_at=plan.created_at,
                activated_at=plan.activated_at,
                sealed_at=plan.sealed_at,
                superseded_at=plan.superseded_at,
                is_active=plan.activated_at is not None and plan.superseded_at is None,
                member_count=members,
                root_count=roots,
                excluded_count=excluded,
                member_counts=dict(by_status),
            )
        )
    return out


async def get_plan_detail(
    session: AsyncSession, environment_id: UUID, plan_id: UUID
) -> PlanDetail:
    plan = await get_plan(session, environment_id, plan_id)
    return (await _details(session, environment_id, [plan]))[0]


async def list_build_plans(
    session: AsyncSession, environment_id: UUID, build_id: UUID
) -> list[PlanDetail]:
    """Every plan of the build, newest generation first (superseded and
    never-activated replacements included)."""
    build = await get_build(session, environment_id, build_id)
    plans = list(
        (
            await session.scalars(
                select(Plan)
                .where(Plan.build_id == build.id)
                .order_by(Plan.generation.desc())
            )
        ).all()
    )
    return await _details(session, environment_id, plans)


@dataclass(frozen=True)
class PlanRoots:
    plan_id: UUID
    build_id: UUID
    deployment_id: UUID
    settings_hash: str
    roots: list[FrontierMember]


async def plan_roots(
    session: AsyncSession, environment_id: UUID, plan_id: UUID
) -> PlanRoots:
    """The plan's root members with their instance bodies, in ``task_id``
    order (excluded roots included: an excluded root has already failed the
    build, and rollover compares the whole request)."""
    plan = await get_plan(session, environment_id, plan_id)
    rows = (
        await session.execute(
            select(
                Task.task_id,
                PlanMember.task_pk,
                PlanMember.instance_id,
                TaskInstance.instance_hash,
                TaskInstance.body,
                Task.status,
            )
            .select_from(PlanMember)
            .join(TaskInstance, TaskInstance.id == PlanMember.instance_id)
            .join(Task, Task.id == PlanMember.task_pk)
            .where(PlanMember.plan_id == plan.id, PlanMember.is_root.is_(True))
            .order_by(Task.task_id)
        )
    ).all()
    return PlanRoots(
        plan_id=plan.id,
        build_id=plan.build_id,
        deployment_id=plan.deployment_id,
        settings_hash=plan.settings_hash,
        roots=[
            FrontierMember(
                task_id=r.task_id,
                task_pk=r.task_pk,
                instance_id=r.instance_id,
                instance_hash=r.instance_hash,
                status=r.status,
                is_root=True,
                body=r.body,
            )
            for r in rows
        ],
    )


@dataclass(frozen=True)
class GraphMember:
    task_id: str
    instance_id: UUID
    instance_hash: str
    task_namespace: str
    task_name: str
    status: TaskStatus
    is_root: bool
    admitted_by: AdmittedBy
    excluded_at: datetime | None
    excluded_reason: ExclusionReason | None
    #: Executions of the task under any of the build's plans, and those of
    #: them that ended interrupted or preempted (as on the frontier).
    attempts: int
    interruptions: int


@dataclass(frozen=True)
class GraphEdge:
    upstream_instance_id: UUID
    downstream_instance_id: UUID
    is_dynamic: bool


@dataclass(frozen=True)
class PlanGraph:
    plan_id: UUID
    build_id: UUID
    deployment_id: UUID
    settings_hash: str
    members: list[GraphMember]
    edges: list[GraphEdge]


async def plan_graph(
    session: AsyncSession, environment_id: UUID, plan_id: UUID
) -> PlanGraph:
    """The plan's members in ``task_id`` order, and the edges whose two ends
    are both member instances."""
    plan = await get_plan(session, environment_id, plan_id)
    rows = (
        await session.execute(
            select(
                Task.task_id,
                Task.task_namespace,
                Task.task_name,
                Task.status,
                PlanMember.task_pk,
                PlanMember.instance_id,
                TaskInstance.instance_hash,
                PlanMember.is_root,
                PlanMember.admitted_by,
                PlanMember.excluded_at,
                PlanMember.excluded_reason,
            )
            .select_from(PlanMember)
            .join(Task, Task.id == PlanMember.task_pk)
            .join(TaskInstance, TaskInstance.id == PlanMember.instance_id)
            .where(PlanMember.plan_id == plan.id)
            .order_by(Task.task_id)
        )
    ).all()
    counts = await attempt_counts(session, plan.build_id, [r.task_pk for r in rows])
    downstream = PlanMember.__table__.alias("downstream_member")
    upstream = PlanMember.__table__.alias("upstream_member")
    edges = (
        await session.execute(
            select(
                TaskInstanceDependency.upstream_instance_id,
                TaskInstanceDependency.downstream_instance_id,
                TaskInstanceDependency.is_dynamic,
            )
            .join(
                downstream,
                (
                    downstream.c.instance_id
                    == TaskInstanceDependency.downstream_instance_id
                )
                & (downstream.c.plan_id == plan.id),
            )
            .join(
                upstream,
                (upstream.c.instance_id == TaskInstanceDependency.upstream_instance_id)
                & (upstream.c.plan_id == plan.id),
            )
            .order_by(
                TaskInstanceDependency.downstream_instance_id,
                TaskInstanceDependency.upstream_instance_id,
            )
        )
    ).tuples()
    return PlanGraph(
        plan_id=plan.id,
        build_id=plan.build_id,
        deployment_id=plan.deployment_id,
        settings_hash=plan.settings_hash,
        members=[
            GraphMember(
                task_id=r.task_id,
                instance_id=r.instance_id,
                instance_hash=r.instance_hash,
                task_namespace=r.task_namespace,
                task_name=r.task_name,
                status=r.status,
                is_root=r.is_root,
                admitted_by=r.admitted_by,
                excluded_at=r.excluded_at,
                excluded_reason=r.excluded_reason,
                attempts=counts.get(r.task_pk, (0, 0))[0],
                interruptions=counts.get(r.task_pk, (0, 0))[1],
            )
            for r in rows
        ],
        edges=[GraphEdge(u, d, dyn) for u, d, dyn in edges],
    )


__all__ = [
    "GraphEdge",
    "GraphMember",
    "PlanDetail",
    "PlanGraph",
    "PlanRoots",
    "get_plan_detail",
    "list_build_plans",
    "plan_graph",
    "plan_roots",
]
