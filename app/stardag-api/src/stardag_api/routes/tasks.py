"""Task routes - workspace-scoped task queries."""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.auth import SdkAuth, require_sdk_auth
from stardag_api.db import get_db
from stardag_api.models import Event, Task, TaskArtifact, TaskStatus
from stardag_api.schemas import (
    EventResponse,
    TaskGraphExtendedResponse,
    TaskGraphRequest,
    TaskListResponse,
    TaskMetadataResponse,
    TaskArtifactListResponse,
    TaskArtifactResponse,
    TaskResponse,
)
from stardag_api.services.graph import traverse_upstream
from stardag_api.services.status import get_task_global_status

router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.get("", response_model=TaskListResponse)
async def list_tasks(
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[SdkAuth, Depends(require_sdk_auth)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    task_name: str | None = None,
    task_namespace: str | None = None,
    status: Annotated[
        list[TaskStatus] | None,
        Query(
            description=(
                "Filter by the task's *global* (environment-wide) status. "
                "Repeatable — `?status=running&status=suspended` matches "
                "either."
            ),
        ),
    ] = None,
    status_older_than: Annotated[
        datetime | None,
        Query(
            description=(
                "Only tasks whose current status was recorded strictly "
                "before this ISO-8601 timestamp (`latest_status_at < "
                "status_older_than`). Tasks with no recorded "
                "`latest_status_at` never match."
            ),
        ),
    ] = None,
):
    """List tasks in an environment.

    Requires authentication via API key or JWT token with environment_id.
    The workspace is determined from the authentication context.

    The ``status`` filter answers the operational question "which tasks in
    this environment are holding an execution claim?" — i.e.
    ``?status=running``. ``latest_status`` is environment-global (task rows
    are unique per ``(environment_id, task_id)``), so a task left RUNNING by
    a build whose orchestrator died denies the claim to *every* future build
    that needs it, indefinitely. ``suspended`` matters for the same reason:
    it is an abandoned execution that gates everything downstream of it.

    ``status_older_than`` is the staleness cut for triage. It takes an
    **absolute ISO-8601 timestamp**, not a duration, for two reasons:

    - It is reproducible. Paging through matches with a duration would move
      the cutoff on every request (the server re-evaluates ``now() -
      duration`` each time), so rows can drift in and out between pages.
    - It is exact. "What did I ask for?" is answerable from the request
      alone, and the same cutoff can be replayed later against the log.

    The counter-argument is client/server clock skew, since the client
    computes the timestamp — but skew is seconds under NTP while realistic
    staleness thresholds are hours to days. A CLI wanting ``--older-than
    24h`` converts locally. (Contrast the build reaper, whose threshold is a
    recurring *policy* and therefore a duration: ``idle_for_seconds``.)

    Ordering: newest-first by registration time, as always — **except**
    when a status/staleness filter is applied, where it becomes oldest
    claim first (``latest_status_at`` ascending). That is the triage order:
    the task that has been RUNNING longest is the most likely to be
    abandoned and the most expensive to leave holding a claim.
    """
    environment_id = auth.environment_id
    query = select(Task).where(Task.environment_id == environment_id)
    count_query = (
        select(func.count())
        .select_from(Task)
        .where(Task.environment_id == environment_id)
    )

    if task_name:
        query = query.where(Task.task_name == task_name)
        count_query = count_query.where(Task.task_name == task_name)
    if task_namespace:
        query = query.where(Task.task_namespace == task_namespace)
        count_query = count_query.where(Task.task_namespace == task_namespace)
    if status:
        # Dedup so a repeated value doesn't widen the IN list pointlessly.
        status_filter = Task.latest_status.in_(list(dict.fromkeys(status)))
        query = query.where(status_filter)
        count_query = count_query.where(status_filter)
    if status_older_than is not None:
        # Strictly older. A NULL latest_status_at (a row predating status
        # denormalisation, or a phantom that never had an event) is excluded
        # by SQL's NULL comparison semantics — correct here: we cannot claim
        # such a row is stale, and a cleanup workflow must not act on rows
        # whose age is unknown.
        staleness_filter = Task.latest_status_at < status_older_than
        query = query.where(staleness_filter)
        count_query = count_query.where(staleness_filter)

    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0
    if status or status_older_than is not None:
        # Oldest claim first (see docstring). NULLS LAST explicitly rather
        # than by dialect default — Postgres and SQLite disagree on where
        # NULLs sort in ASC, and a paginated list must not depend on that.
        # ``Task.id`` (UUID7) breaks timestamp ties deterministically so
        # pagination can't duplicate or skip rows.
        ordering = (Task.latest_status_at.asc().nulls_last(), Task.id.asc())
    else:
        ordering = (Task.created_at.desc(),)
    result = await db.execute(
        query.order_by(*ordering).offset((page - 1) * page_size).limit(page_size)
    )
    tasks = result.scalars().all()

    return TaskListResponse(
        tasks=[
            TaskResponse(
                id=t.id,
                task_id=t.task_id,
                environment_id=t.environment_id,
                task_namespace=t.task_namespace,
                task_name=t.task_name,
                task_data=t.task_data,
                version=t.version,
                output_uri=t.output_uri,
                created_at=t.created_at,
                is_phantom=t.is_phantom,
                latest_executor=t.latest_executor,
                latest_executor_ref=t.latest_executor_ref,
                latest_executor_metadata=t.latest_executor_metadata,
                latest_status=t.latest_status,
                latest_status_at=t.latest_status_at,
                latest_status_build_id=t.latest_status_build_id,
            )
            for t in tasks
        ],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.post("/graph", response_model=TaskGraphExtendedResponse)
async def get_task_graph(
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[SdkAuth, Depends(require_sdk_auth)],
    body: TaskGraphRequest,
):
    """Get the task graph for a set of tasks with optional upstream traversal.

    Resolves task_id hashes to internal PKs and traverses upstream dependencies.
    Used by the Task Explorer for cross-build DAG visualization.
    """
    if not body.task_ids:
        return TaskGraphExtendedResponse(
            nodes=[],
            edges=[],
            groups=[],
            truncated=False,
            total_upstream_count=0,
            total_downstream_count=0,
        )

    # Resolve task_id hashes to internal PKs
    result = await db.execute(
        select(Task)
        .where(Task.environment_id == auth.environment_id)
        .where(Task.task_id.in_(body.task_ids))
    )
    tasks = result.scalars().all()
    task_pks = [t.id for t in tasks]

    if not task_pks:
        return TaskGraphExtendedResponse(
            nodes=[],
            edges=[],
            groups=[],
            truncated=False,
            total_upstream_count=0,
            total_downstream_count=0,
        )

    return await traverse_upstream(
        db=db,
        environment_id=auth.environment_id,
        primary_task_pks=task_pks,
        upstream_depth=body.upstream_depth,
        downstream_depth=body.downstream_depth,
        max_per_type_per_level=body.max_per_type_per_level,
        max_total_nodes=body.max_total_nodes,
    )


@router.get("/{task_id}", response_model=TaskResponse)
async def get_task(
    task_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[SdkAuth, Depends(require_sdk_auth)],
):
    """Get a task by its task_id (hash) in an environment.

    Requires authentication via API key or JWT token with environment_id.
    The workspace is determined from the authentication context.
    """
    result = await db.execute(
        select(Task)
        .where(Task.environment_id == auth.environment_id)
        .where(Task.task_id == task_id)
    )
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    return TaskResponse(
        id=task.id,
        task_id=task.task_id,
        environment_id=task.environment_id,
        task_namespace=task.task_namespace,
        task_name=task.task_name,
        task_data=task.task_data,
        version=task.version,
        output_uri=task.output_uri,
        created_at=task.created_at,
        is_phantom=task.is_phantom,
        latest_executor=task.latest_executor,
        latest_executor_ref=task.latest_executor_ref,
        latest_executor_metadata=task.latest_executor_metadata,
        latest_status=task.latest_status,
        latest_status_at=task.latest_status_at,
        latest_status_build_id=task.latest_status_build_id,
    )


@router.get("/{task_id}/artifacts", response_model=TaskArtifactListResponse)
async def get_task_artifacts(
    task_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[SdkAuth, Depends(require_sdk_auth)],
):
    """Get artifacts for a task by its task_id (hash).

    Requires authentication via API key or JWT token with environment_id.
    The workspace is determined from the authentication context.
    """
    # Find task by task_id (hash) in workspace
    result = await db.execute(
        select(Task)
        .where(Task.environment_id == auth.environment_id)
        .where(Task.task_id == task_id)
    )
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    # Get all artifacts for this task
    artifacts_result = await db.execute(
        select(TaskArtifact)
        .where(TaskArtifact.task_pk == task.id)
        .order_by(TaskArtifact.created_at.asc())
    )
    artifacts = artifacts_result.scalars().all()

    return TaskArtifactListResponse(
        artifacts=[
            TaskArtifactResponse(
                id=artifact.id,
                task_id=task.task_id,
                artifact_type=artifact.artifact_type,
                name=artifact.name,
                body=artifact.body_json,
                created_at=artifact.created_at,
            )
            for artifact in artifacts
        ]
    )


@router.get("/{task_id}/events", response_model=list[EventResponse])
async def get_task_events(
    task_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[SdkAuth, Depends(require_sdk_auth)],
):
    """Get all events for a task across all builds.

    Returns events sorted by creation time (newest first).
    Requires authentication via API key or JWT token with environment_id.
    """
    # Find task by task_id (hash) in workspace
    result = await db.execute(
        select(Task)
        .where(Task.environment_id == auth.environment_id)
        .where(Task.task_id == task_id)
    )
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    # Get all events for this task across all builds
    events_result = await db.execute(
        select(Event).where(Event.task_id == task.id).order_by(Event.created_at.desc())
    )
    events = events_result.scalars().all()

    return [
        EventResponse(
            id=event.id,
            build_id=event.build_id,
            task_id=event.task_id,
            event_type=event.event_type,
            created_at=event.created_at,
            error_message=event.error_message,
            event_metadata=event.event_metadata,
        )
        for event in events
    ]


@router.get("/{task_id}/metadata", response_model=TaskMetadataResponse)
async def get_task_metadata(
    task_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[SdkAuth, Depends(require_sdk_auth)],
):
    """Get task metadata for SDK task_get_metadata.

    Returns task metadata in the format expected by the SDK's TaskMetadata class,
    which is used by AliasTask.from_registry to create alias tasks.

    Requires authentication via API key or JWT token with environment_id.
    """
    # Find task by task_id (hash) in workspace
    result = await db.execute(
        select(Task)
        .where(Task.environment_id == auth.environment_id)
        .where(Task.task_id == task_id)
    )
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    # Get task status from events
    status, started_at, completed_at, error_message, _ = await get_task_global_status(
        db, task.id
    )

    return TaskMetadataResponse(
        id=task.task_id,
        body=task.task_data,
        name=task.task_name,
        namespace=task.task_namespace,
        version=task.version or "",
        output_uri=task.output_uri,
        status=status,
        registered_at=task.created_at,
        started_at=started_at,
        completed_at=completed_at,
        error_message=error_message,
    )
