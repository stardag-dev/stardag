"""Task routes - workspace-scoped task queries."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.auth import SdkAuth, require_sdk_auth
from stardag_api.db import get_db
from stardag_api.models import Event, Task, TaskArtifact
from stardag_api.schemas import (
    EventResponse,
    TaskGraphExtendedResponse,
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
):
    """List tasks in an environment.

    Requires authentication via API key or JWT token with environment_id.
    The workspace is determined from the authentication context.
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

    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0
    result = await db.execute(
        query.order_by(Task.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
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
            )
            for t in tasks
        ],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/graph", response_model=TaskGraphExtendedResponse)
async def get_task_graph(
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[SdkAuth, Depends(require_sdk_auth)],
    task_ids: Annotated[str, Query(description="Comma-separated task_id hashes")],
    upstream_depth: Annotated[int, Query(ge=0, le=10)] = 0,
    max_per_type_per_level: Annotated[int, Query(ge=1, le=200)] = 50,
    max_total_nodes: Annotated[int, Query(ge=1, le=2000)] = 500,
):
    """Get the task graph for a set of tasks with optional upstream traversal.

    Resolves task_id hashes to internal PKs and traverses upstream dependencies.
    Used by the Task Explorer for cross-build DAG visualization.
    """
    task_id_list = [tid.strip() for tid in task_ids.split(",") if tid.strip()]
    if not task_id_list:
        return TaskGraphExtendedResponse(
            nodes=[], edges=[], groups=[], truncated=False, total_upstream_count=0
        )

    # Resolve task_id hashes to internal PKs
    result = await db.execute(
        select(Task)
        .where(Task.environment_id == auth.environment_id)
        .where(Task.task_id.in_(task_id_list))
    )
    tasks = result.scalars().all()
    task_pks = [t.id for t in tasks]

    if not task_pks:
        return TaskGraphExtendedResponse(
            nodes=[], edges=[], groups=[], truncated=False, total_upstream_count=0
        )

    return await traverse_upstream(
        db=db,
        environment_id=auth.environment_id,
        primary_task_pks=task_pks,
        upstream_depth=upstream_depth,
        max_per_type_per_level=max_per_type_per_level,
        max_total_nodes=max_total_nodes,
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
