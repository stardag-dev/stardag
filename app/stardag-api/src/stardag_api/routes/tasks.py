"""Task routes - workspace-scoped task queries."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.auth import SdkAuth, require_sdk_auth
from stardag_api.db import get_db
from stardag_api.models import Event, Task, TaskRegistryAsset
from stardag_api.schemas import (
    EventResponse,
    TaskListResponse,
    TaskRegistryAssetListResponse,
    TaskRegistryAssetResponse,
    TaskResponse,
)

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
                created_at=t.created_at,
            )
            for t in tasks
        ],
        total=total,
        page=page,
        page_size=page_size,
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
        created_at=task.created_at,
    )


@router.get("/{task_id}/assets", response_model=TaskRegistryAssetListResponse)
async def get_task_assets(
    task_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[SdkAuth, Depends(require_sdk_auth)],
):
    """Get assets for a task by its task_id (hash).

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

    # Get all assets for this task
    assets_result = await db.execute(
        select(TaskRegistryAsset)
        .where(TaskRegistryAsset.task_pk == task.id)
        .order_by(TaskRegistryAsset.created_at.asc())
    )
    assets = assets_result.scalars().all()

    return TaskRegistryAssetListResponse(
        assets=[
            TaskRegistryAssetResponse(
                id=asset.id,
                task_id=task.task_id,
                asset_type=asset.asset_type,
                name=asset.name,
                body=asset.body_json,
                created_at=asset.created_at,
            )
            for asset in assets
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
