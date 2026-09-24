"""Reads of builds, tasks and the event log (plans: ``plan_reads.py``).

- ``GET /builds`` (:func:`list_builds`): builds of the caller's environment,
  most recently active first, filtered by status and reactive app — the
  watchdog's "RUNNING builds owned by app X" — and by idleness
  (``idle_for_seconds``), a page at a time, with the total the filters
  match.
- ``GET /tasks/{task_id}`` (:func:`get_task`): a ``task`` row holds no
  parameters, so a completion is returned with its **instances** — each a
  body under one scope — newest first. Which body to rehydrate is the
  caller's choice (they are different constructions of one promise). The
  claim's holder is named by plan and build.
- ``GET /tasks`` (:func:`list_tasks`): completions by status, most recent
  status change first, a page at a time (triage).
- ``GET /tasks/{task_id}/events`` and ``GET /builds/{id}/events``
  (:func:`list_events`): the append-only log, oldest first. The status
  columns are one row per task, overwritten by whoever wrote last; the log
  is where "which build reset this task" and "which report was refused"
  are still visible.

Plain reads: no locks, no writes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import Select, func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.models import (
    Build,
    BuildStatus,
    Event,
    EventType,
    Plan,
    Task,
    TaskInstance,
    TaskStatus,
)
from stardag_api.services.builds import get_build
from stardag_api.models.base import utc_now
from stardag_api.services.errors import BadRequest, NotFound
from stardag_api.services.paging import decode_cursor, encode_cursor

#: Bounds of one listing.
MAX_LIST_LIMIT = 500


@dataclass(frozen=True)
class BuildPage:
    builds: list[Build]
    #: Builds matching the filters, over every page.
    total: int
    #: Pass back as ``cursor`` for the next page; None on the last one.
    next_cursor: str | None


async def list_builds(
    session: AsyncSession,
    environment_id: UUID,
    *,
    status: BuildStatus | None = None,
    reactive_app_name: str | None = None,
    idle_for_seconds: int | None = None,
    limit: int = 100,
    cursor: str | None = None,
) -> BuildPage:
    """Builds, most recently active first, a page at a time
    (``ix_build_environment_status`` serves a status filter with this
    order). ``total`` counts every build the filters match.

    ``idle_for_seconds`` keeps builds that are **still running** and whose
    ``last_active_at`` is at least that old. It implies RUNNING, as in v1:
    a finished build is not idle, and without the predicate the filter
    would list every build that ended long enough ago. So it combines with
    no status or ``running`` only; any other status is a contradiction,
    refused 400 ``idle_requires_running`` rather than served empty.
    ``last_active_at`` moves on build lifecycle changes (create, resume,
    terminal status) and on task activity (a status change of a task the
    build's active plan holds), so "idle" here means "no task or lifecycle
    activity for that long" — a build only sitting on stalled tasks matches,
    a busy one does not. The order stays most recently active first, so the
    keyset cursor is the same one."""
    limit = max(1, min(limit, MAX_LIST_LIMIT))
    if idle_for_seconds is not None and status not in (None, BuildStatus.RUNNING):
        raise BadRequest(
            "idle_requires_running",
            f"status={status.value!r} cannot be combined with idle_for_seconds: "
            "an idle filter already means 'still running'",
        )
    filters = [Build.environment_id == environment_id]
    if status is not None:
        filters.append(Build.status == status)
    if reactive_app_name is not None:
        filters.append(Build.reactive_app_name == reactive_app_name)
    if idle_for_seconds is not None:
        filters.append(Build.status == BuildStatus.RUNNING)
        filters.append(
            Build.last_active_at <= utc_now() - timedelta(seconds=idle_for_seconds)
        )
    total = await session.scalar(
        select(func.count()).select_from(Build).where(*filters)
    )
    stmt = select(Build).where(*filters)
    if cursor is not None:
        at, after_id = decode_cursor(cursor)
        stmt = stmt.where(tuple_(Build.last_active_at, Build.id) < (at, after_id))
    rows = list(
        (
            await session.scalars(
                stmt.order_by(Build.last_active_at.desc(), Build.id.desc()).limit(
                    limit + 1
                )
            )
        ).all()
    )
    more = len(rows) > limit
    rows = rows[:limit]
    return BuildPage(
        builds=rows,
        total=total or 0,
        next_cursor=(
            encode_cursor(rows[-1].last_active_at, rows[-1].id) if more else None
        ),
    )


@dataclass(frozen=True)
class InstanceView:
    id: UUID
    deployment_id: UUID
    settings_hash: UUID
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
    #: The claim's holder, while RUNNING (live or lapsed): the plan it was
    #: granted through, and that plan's build.
    claim_plan_id: UUID | None
    claim_build_id: UUID | None
    #: The current execution (the claim's, while RUNNING).
    execution_id: UUID | None
    instances: list[InstanceView] = field(default_factory=list)

    @classmethod
    def of(
        cls,
        task: Task,
        claim_build_id: UUID | None,
        instances: list[InstanceView] | None = None,
    ) -> TaskView:
        return cls(
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
            claim_plan_id=task.claim_plan_id,
            claim_build_id=claim_build_id,
            execution_id=task.execution_id,
            instances=instances or [],
        )


async def get_task(
    session: AsyncSession, environment_id: UUID, task_id: str, *, limit: int = 50
) -> TaskView:
    """A completion in the caller's environment and its instances, newest
    first (at most ``limit``)."""
    row = (
        await session.execute(
            _with_claim_build(select(Task)).where(
                Task.environment_id == environment_id, Task.task_id == task_id
            )
        )
    ).one_or_none()
    if row is None:
        raise NotFound("unknown_task", f"no task {task_id}", task_id=task_id)
    task, claim_build_id = row._tuple()
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
    return TaskView.of(
        task,
        claim_build_id,
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


def _with_claim_build(stmt: Select[tuple[Task]]) -> Select[tuple[Task, UUID | None]]:
    """``stmt`` over ``task`` with the claim-holding plan's build alongside."""
    return stmt.add_columns(Plan.build_id).outerjoin(
        Plan, Plan.id == Task.claim_plan_id
    )


@dataclass(frozen=True)
class TaskPage:
    tasks: list[TaskView]
    next_cursor: str | None


async def list_tasks(
    session: AsyncSession,
    environment_id: UUID,
    *,
    status: TaskStatus | None = None,
    limit: int = 100,
    cursor: str | None = None,
) -> TaskPage:
    """Completions, most recent status change first, a page at a time,
    optionally of one status — triage's "what is FAILED / RUNNING here"
    (``ix_task_environment_status``). Without instances: ``GET
    /tasks/{task_id}`` carries those. ``status_at`` is written at
    registration and by every transition, so every task has one."""
    limit = max(1, min(limit, MAX_LIST_LIMIT))
    stmt = _with_claim_build(select(Task)).where(
        Task.environment_id == environment_id, Task.status_at.is_not(None)
    )
    if status is not None:
        stmt = stmt.where(Task.status == status)
    if cursor is not None:
        at, after_id = decode_cursor(cursor)
        stmt = stmt.where(tuple_(Task.status_at, Task.id) < (at, after_id))
    rows = (
        (
            await session.execute(
                stmt.order_by(Task.status_at.desc(), Task.id.desc()).limit(limit + 1)
            )
        )
        .tuples()
        .all()
    )
    more = len(rows) > limit
    page = [TaskView.of(t, build_id) for t, build_id in rows[:limit]]
    last = rows[limit - 1][0] if more else None
    return TaskPage(
        tasks=page,
        next_cursor=(
            encode_cursor(last.status_at, last.id)
            if last is not None and last.status_at is not None
            else None
        ),
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
    "BuildPage",
    "EventView",
    "InstanceView",
    "MAX_LIST_LIMIT",
    "TaskPage",
    "TaskView",
    "get_task",
    "list_builds",
    "list_events",
    "list_tasks",
]
