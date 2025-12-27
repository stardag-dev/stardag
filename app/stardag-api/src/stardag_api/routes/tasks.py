"""Task routes - workspace-scoped task queries."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.auth import SdkAuth, require_sdk_auth
from stardag_api.db import get_db
from stardag_api.models import Task
from stardag_api.schemas import TaskListResponse, TaskResponse

router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.get("", response_model=TaskListResponse)
async def list_tasks(
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[SdkAuth, Depends(require_sdk_auth)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    task_family: str | None = None,
    task_namespace: str | None = None,
):
    """List tasks in a workspace.

    Requires authentication via API key or JWT token with workspace_id.
    The workspace is determined from the authentication context.
    """
    workspace_id = auth.workspace_id
    query = select(Task).where(Task.workspace_id == workspace_id)
    count_query = (
        select(func.count()).select_from(Task).where(Task.workspace_id == workspace_id)
    )

    if task_family:
        query = query.where(Task.task_family == task_family)
        count_query = count_query.where(Task.task_family == task_family)
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
                workspace_id=t.workspace_id,
                task_namespace=t.task_namespace,
                task_family=t.task_family,
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
    """Get a task by its task_id (hash) in a workspace.

    Requires authentication via API key or JWT token with workspace_id.
    The workspace is determined from the authentication context.
    """
    result = await db.execute(
        select(Task)
        .where(Task.workspace_id == auth.workspace_id)
        .where(Task.task_id == task_id)
    )
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    return TaskResponse(
        id=task.id,
        task_id=task.task_id,
        workspace_id=task.workspace_id,
        task_namespace=task.task_namespace,
        task_family=task.task_family,
        task_data=task.task_data,
        version=task.version,
        created_at=task.created_at,
    )
