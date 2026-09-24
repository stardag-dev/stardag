"""Reads the SDK needs beyond the frontier: the watchdog's build listing,
a plan's roots for rollover, and a completion with its instances.

- ``GET /builds`` (:func:`list_builds`): builds of the caller's environment,
  most recently active first, filtered by status and reactive app — the
  watchdog's "RUNNING builds owned by app X".
- ``GET /plans/{id}/roots`` (:func:`plan_roots`): the plan's root members
  with their instance bodies, which a rolling-over tick rehydrates under its
  own code and re-hashes against ``build.root_task_ids`` (design.md,
  "Rollover", step 2).
- ``GET /tasks/{task_id}`` (:func:`get_task`): a ``task`` row holds no
  parameters, so a completion is returned with its **instances** — each a
  body under one scope — newest first. Which body to rehydrate is the
  caller's choice (they are different constructions of one promise).
- ``GET /tasks/{task_id}/events`` and ``GET /builds/{id}/events``
  (:func:`list_events`): the append-only log, oldest first. The status
  columns are one row per task, overwritten by whoever wrote last; the log
  is where "which build reset this task" and "which report was refused"
  are still visible.

Plain reads: no locks, no writes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.models import (
    Build,
    BuildStatus,
    Event,
    EventType,
    PlanMember,
    Task,
    TaskInstance,
    TaskStatus,
)
from stardag_api.services.builds import get_build
from stardag_api.services.errors import NotFound
from stardag_api.services.frontier import FrontierMember
from stardag_api.services.registration import get_plan

#: Bounds of one listing.
MAX_LIST_LIMIT = 500


async def list_builds(
    session: AsyncSession,
    environment_id: UUID,
    *,
    status: BuildStatus | None = None,
    reactive_app_name: str | None = None,
    limit: int = 100,
) -> list[Build]:
    """Builds, most recently active first (``ix_build_environment_status``
    serves a status filter with this order)."""
    stmt = select(Build).where(Build.environment_id == environment_id)
    if status is not None:
        stmt = stmt.where(Build.status == status)
    if reactive_app_name is not None:
        stmt = stmt.where(Build.reactive_app_name == reactive_app_name)
    stmt = stmt.order_by(Build.last_active_at.desc(), Build.id.desc()).limit(
        max(1, min(limit, MAX_LIST_LIMIT))
    )
    return list((await session.scalars(stmt)).all())


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
class InstanceView:
    id: UUID
    deployment_id: UUID
    settings_hash: str
    instance_hash: str
    body: dict[str, Any]
    expanded_at: datetime | None
    created_at: datetime


@dataclass(frozen=True)
class TaskView:
    task_id: str
    task_namespace: str
    task_name: str
    version: str | None
    output_uri: str | None
    status: TaskStatus
    status_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    error_message: str | None
    claim_expires_at: datetime | None
    execution_id: UUID | None
    instances: list[InstanceView] = field(default_factory=list)


async def get_task(
    session: AsyncSession, environment_id: UUID, task_id: str, *, limit: int = 50
) -> TaskView:
    """A completion in the caller's environment and its instances, newest
    first (at most ``limit``)."""
    task = await session.scalar(
        select(Task).where(
            Task.environment_id == environment_id, Task.task_id == task_id
        )
    )
    if task is None:
        raise NotFound("unknown_task", f"no task {task_id}", task_id=task_id)
    instances = (
        await session.scalars(
            select(TaskInstance)
            .where(
                TaskInstance.environment_id == environment_id,
                TaskInstance.task_pk == task.id,
            )
            .order_by(TaskInstance.created_at.desc(), TaskInstance.id.desc())
            .limit(max(1, min(limit, MAX_LIST_LIMIT)))
        )
    ).all()
    return TaskView(
        task_id=task.task_id,
        task_namespace=task.task_namespace,
        task_name=task.task_name,
        version=task.version,
        output_uri=task.output_uri,
        status=task.status,
        status_at=task.status_at,
        started_at=task.started_at,
        completed_at=task.completed_at,
        error_message=task.error_message,
        claim_expires_at=task.claim_expires_at,
        execution_id=task.execution_id,
        instances=[
            InstanceView(
                id=i.id,
                deployment_id=i.deployment_id,
                settings_hash=i.settings_hash,
                instance_hash=i.instance_hash,
                body=i.body,
                expanded_at=i.expanded_at,
                created_at=i.created_at,
            )
            for i in instances
        ],
    )


@dataclass(frozen=True)
class EventView:
    id: UUID
    event_type: EventType
    created_at: datetime
    build_id: UUID | None
    plan_id: UUID | None
    execution_id: UUID | None
    task_id: str | None
    report_applied: bool
    error_message: str | None
    event_metadata: dict[str, Any] | None


async def list_events(
    session: AsyncSession,
    environment_id: UUID,
    *,
    build_id: UUID | None = None,
    task_id: str | None = None,
    limit: int = MAX_LIST_LIMIT,
) -> list[EventView]:
    """Events of one build or one task (exactly one of the two), oldest
    first, at most ``limit``. 404 for an unknown build or task: an empty
    list must mean "nothing recorded", never "no such thing"."""
    if (build_id is None) == (task_id is None):
        raise ValueError("list_events takes exactly one of build_id, task_id")
    stmt = (
        select(Event, Task.task_id)
        .outerjoin(Task, Task.id == Event.task_pk)
        .where(Event.environment_id == environment_id)
    )
    if build_id is not None:
        build = await get_build(session, environment_id, build_id)
        stmt = stmt.where(Event.build_id == build.id)
    else:
        task_pk = await session.scalar(
            select(Task.id).where(
                Task.environment_id == environment_id, Task.task_id == task_id
            )
        )
        if task_pk is None:
            raise NotFound("unknown_task", f"no task {task_id}", task_id=task_id)
        stmt = stmt.where(Event.task_pk == task_pk)
    rows = (
        await session.execute(
            stmt.order_by(Event.created_at, Event.id).limit(
                max(1, min(limit, MAX_LIST_LIMIT))
            )
        )
    ).tuples()
    return [
        EventView(
            id=e.id,
            event_type=e.event_type,
            created_at=e.created_at,
            build_id=e.build_id,
            plan_id=e.plan_id,
            execution_id=e.execution_id,
            task_id=tid,
            report_applied=e.report_applied,
            error_message=e.error_message,
            event_metadata=e.event_metadata,
        )
        for e, tid in rows
    ]


__all__ = [
    "EventView",
    "InstanceView",
    "MAX_LIST_LIMIT",
    "PlanRoots",
    "TaskView",
    "get_task",
    "list_builds",
    "list_events",
    "plan_roots",
]
