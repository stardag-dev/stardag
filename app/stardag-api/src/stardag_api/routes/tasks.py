from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.db import get_db
from stardag_api.models.task import TaskRecord, TaskStatus
from stardag_api.schemas import (
    TaskCreate,
    TaskListResponse,
    TaskResponse,
    TaskUpdate,
)

router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.post("", response_model=TaskResponse, status_code=201)
async def create_task(task: TaskCreate, db: AsyncSession = Depends(get_db)):
    existing = await db.get(TaskRecord, task.task_id)
    if existing:
        raise HTTPException(status_code=409, detail="Task already exists")

    db_task = TaskRecord(
        task_id=task.task_id,
        task_family=task.task_family,
        task_data=task.task_data,
        user=task.user,
        commit_hash=task.commit_hash,
        dependency_ids=task.dependency_ids,
        status=TaskStatus.PENDING,
    )
    db.add(db_task)
    await db.commit()
    await db.refresh(db_task)
    return db_task


@router.get("", response_model=TaskListResponse)
async def list_tasks(
    db: AsyncSession = Depends(get_db),
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    task_family: str | None = None,
    status: TaskStatus | None = None,
    user: str | None = None,
):
    query = select(TaskRecord)
    count_query = select(func.count()).select_from(TaskRecord)

    if task_family:
        query = query.where(TaskRecord.task_family == task_family)
        count_query = count_query.where(TaskRecord.task_family == task_family)
    if status:
        query = query.where(TaskRecord.status == status)
        count_query = count_query.where(TaskRecord.status == status)
    if user:
        query = query.where(TaskRecord.user == user)
        count_query = count_query.where(TaskRecord.user == user)

    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0
    result = await db.execute(
        query.order_by(TaskRecord.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    tasks = result.scalars().all()

    return TaskListResponse(
        tasks=tasks,  # type: ignore[arg-type]
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/{task_id}", response_model=TaskResponse)
async def get_task(task_id: str, db: AsyncSession = Depends(get_db)):
    task = await db.get(TaskRecord, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@router.patch("/{task_id}", response_model=TaskResponse)
async def update_task(
    task_id: str, update: TaskUpdate, db: AsyncSession = Depends(get_db)
):
    task = await db.get(TaskRecord, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    if update.status is not None:
        task.status = update.status
        if update.status == TaskStatus.RUNNING:
            task.started_at = datetime.now(timezone.utc)
        elif update.status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
            task.completed_at = datetime.now(timezone.utc)

    if update.error_message is not None:
        task.error_message = update.error_message

    await db.commit()
    await db.refresh(task)
    return task


@router.post("/{task_id}/start", response_model=TaskResponse)
async def start_task(task_id: str, db: AsyncSession = Depends(get_db)):
    task = await db.get(TaskRecord, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    task.status = TaskStatus.RUNNING
    task.started_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(task)
    return task


@router.post("/{task_id}/complete", response_model=TaskResponse)
async def complete_task(task_id: str, db: AsyncSession = Depends(get_db)):
    task = await db.get(TaskRecord, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    task.status = TaskStatus.COMPLETED
    task.completed_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(task)
    return task


@router.post("/{task_id}/fail", response_model=TaskResponse)
async def fail_task(
    task_id: str, error_message: str | None = None, db: AsyncSession = Depends(get_db)
):
    task = await db.get(TaskRecord, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    task.status = TaskStatus.FAILED
    task.completed_at = datetime.now(timezone.utc)
    if error_message:
        task.error_message = error_message
    await db.commit()
    await db.refresh(task)
    return task
