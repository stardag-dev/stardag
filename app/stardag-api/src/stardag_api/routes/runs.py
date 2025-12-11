"""Run management routes - primary interface for SDK."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.db import get_db
from stardag_api.models import (
    Event,
    EventType,
    Run,
    Task,
    TaskDependency,
    TaskStatus,
    User,
)
from stardag_api.schemas import (
    EventResponse,
    RunCreate,
    RunListResponse,
    RunResponse,
    TaskCreate,
    TaskGraphResponse,
    TaskEdge,
    TaskNode,
    TaskResponse,
    TaskWithStatusResponse,
)
from stardag_api.services import generate_run_slug, get_run_status
from stardag_api.services.status import get_all_task_statuses_in_run

router = APIRouter(prefix="/runs", tags=["runs"])


# --- Run CRUD ---


@router.post("", response_model=RunResponse, status_code=201)
async def create_run(run: RunCreate, db: AsyncSession = Depends(get_db)):
    """Create a new run.

    This is the entry point for SDK - creates a new run and returns its ID.
    """
    # Resolve user by username
    user_result = await db.execute(select(User).where(User.username == run.user))
    user = user_result.scalar_one_or_none()

    # Generate memorable slug
    name = generate_run_slug()

    db_run = Run(
        workspace_id=run.workspace_id,
        user_id=user.id if user else None,
        name=name,
        description=run.description,
        commit_hash=run.commit_hash,
        root_task_ids=run.root_task_ids,
    )
    db.add(db_run)
    await db.flush()  # Get the run ID

    # Create RUN_STARTED event
    start_event = Event(
        run_id=db_run.id,
        task_id=None,
        event_type=EventType.RUN_STARTED,
    )
    db.add(start_event)

    await db.commit()
    await db.refresh(db_run)

    # Build response with derived status
    status, started_at, completed_at = await get_run_status(db, db_run.id)

    return RunResponse(
        id=db_run.id,
        workspace_id=db_run.workspace_id,
        user_id=db_run.user_id,
        name=db_run.name,
        description=db_run.description,
        commit_hash=db_run.commit_hash,
        root_task_ids=db_run.root_task_ids,
        created_at=db_run.created_at,
        status=status,
        started_at=started_at,
        completed_at=completed_at,
    )


@router.get("", response_model=RunListResponse)
async def list_runs(
    db: AsyncSession = Depends(get_db),
    workspace_id: str = "default",
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    """List runs in a workspace."""
    query = select(Run).where(Run.workspace_id == workspace_id)
    count_query = (
        select(func.count()).select_from(Run).where(Run.workspace_id == workspace_id)
    )

    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0

    result = await db.execute(
        query.order_by(Run.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    runs = result.scalars().all()

    # Build responses with derived status
    run_responses = []
    for run in runs:
        status, started_at, completed_at = await get_run_status(db, run.id)
        run_responses.append(
            RunResponse(
                id=run.id,
                workspace_id=run.workspace_id,
                user_id=run.user_id,
                name=run.name,
                description=run.description,
                commit_hash=run.commit_hash,
                root_task_ids=run.root_task_ids,
                created_at=run.created_at,
                status=status,
                started_at=started_at,
                completed_at=completed_at,
            )
        )

    return RunListResponse(
        runs=run_responses,
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/{run_id}", response_model=RunResponse)
async def get_run(run_id: str, db: AsyncSession = Depends(get_db)):
    """Get a run by ID with derived status."""
    run = await db.get(Run, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    status, started_at, completed_at = await get_run_status(db, run.id)

    return RunResponse(
        id=run.id,
        workspace_id=run.workspace_id,
        user_id=run.user_id,
        name=run.name,
        description=run.description,
        commit_hash=run.commit_hash,
        root_task_ids=run.root_task_ids,
        created_at=run.created_at,
        status=status,
        started_at=started_at,
        completed_at=completed_at,
    )


@router.post("/{run_id}/complete", response_model=RunResponse)
async def complete_run(run_id: str, db: AsyncSession = Depends(get_db)):
    """Mark a run as completed."""
    run = await db.get(Run, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    event = Event(
        run_id=run_id,
        task_id=None,
        event_type=EventType.RUN_COMPLETED,
    )
    db.add(event)
    await db.commit()

    status, started_at, completed_at = await get_run_status(db, run.id)

    return RunResponse(
        id=run.id,
        workspace_id=run.workspace_id,
        user_id=run.user_id,
        name=run.name,
        description=run.description,
        commit_hash=run.commit_hash,
        root_task_ids=run.root_task_ids,
        created_at=run.created_at,
        status=status,
        started_at=started_at,
        completed_at=completed_at,
    )


@router.post("/{run_id}/fail", response_model=RunResponse)
async def fail_run(
    run_id: str,
    error_message: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Mark a run as failed."""
    run = await db.get(Run, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    event = Event(
        run_id=run_id,
        task_id=None,
        event_type=EventType.RUN_FAILED,
        error_message=error_message,
    )
    db.add(event)
    await db.commit()

    status, started_at, completed_at = await get_run_status(db, run.id)

    return RunResponse(
        id=run.id,
        workspace_id=run.workspace_id,
        user_id=run.user_id,
        name=run.name,
        description=run.description,
        commit_hash=run.commit_hash,
        root_task_ids=run.root_task_ids,
        created_at=run.created_at,
        status=status,
        started_at=started_at,
        completed_at=completed_at,
    )


# --- Tasks within Runs ---


@router.post("/{run_id}/tasks", response_model=TaskResponse, status_code=201)
async def register_task(
    run_id: str, task: TaskCreate, db: AsyncSession = Depends(get_db)
):
    """Register a task to a run.

    If the task already exists in the workspace, it will be reused.
    Creates a TASK_PENDING event for this run.
    """
    run = await db.get(Run, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    # Check if task already exists in workspace
    result = await db.execute(
        select(Task)
        .where(Task.workspace_id == run.workspace_id)
        .where(Task.task_id == task.task_id)
    )
    db_task = result.scalar_one_or_none()

    if not db_task:
        # Create new task
        db_task = Task(
            task_id=task.task_id,
            workspace_id=run.workspace_id,
            task_namespace=task.task_namespace,
            task_family=task.task_family,
            task_data=task.task_data,
            version=task.version,
        )
        db.add(db_task)
        await db.flush()  # Get the id

        # Create dependencies
        for dep_task_id in task.dependency_task_ids:
            # Find the upstream task
            dep_result = await db.execute(
                select(Task)
                .where(Task.workspace_id == run.workspace_id)
                .where(Task.task_id == dep_task_id)
            )
            dep_task = dep_result.scalar_one_or_none()
            if dep_task:
                # Check if dependency edge already exists
                edge_result = await db.execute(
                    select(TaskDependency)
                    .where(TaskDependency.upstream_task_id == dep_task.id)
                    .where(TaskDependency.downstream_task_id == db_task.id)
                )
                if not edge_result.scalar_one_or_none():
                    dep_edge = TaskDependency(
                        upstream_task_id=dep_task.id,
                        downstream_task_id=db_task.id,
                    )
                    db.add(dep_edge)

    # Create TASK_PENDING event for this run
    event = Event(
        run_id=run_id,
        task_id=db_task.id,
        event_type=EventType.TASK_PENDING,
    )
    db.add(event)

    await db.commit()
    await db.refresh(db_task)

    return TaskResponse(
        id=db_task.id,
        task_id=db_task.task_id,
        workspace_id=db_task.workspace_id,
        task_namespace=db_task.task_namespace,
        task_family=db_task.task_family,
        task_data=db_task.task_data,
        version=db_task.version,
        created_at=db_task.created_at,
    )


@router.post("/{run_id}/tasks/{task_id}/start", response_model=TaskWithStatusResponse)
async def start_task(run_id: str, task_id: str, db: AsyncSession = Depends(get_db)):
    """Mark a task as started within a run."""
    run = await db.get(Run, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    # Find task by task_id (hash) in workspace
    result = await db.execute(
        select(Task)
        .where(Task.workspace_id == run.workspace_id)
        .where(Task.task_id == task_id)
    )
    db_task = result.scalar_one_or_none()
    if not db_task:
        raise HTTPException(status_code=404, detail="Task not found")

    event = Event(
        run_id=run_id,
        task_id=db_task.id,
        event_type=EventType.TASK_STARTED,
    )
    db.add(event)
    await db.commit()

    from stardag_api.services.status import get_task_status_in_run

    status, started_at, completed_at, error_message = await get_task_status_in_run(
        db, run_id, db_task.id
    )

    return TaskWithStatusResponse(
        id=db_task.id,
        task_id=db_task.task_id,
        workspace_id=db_task.workspace_id,
        task_namespace=db_task.task_namespace,
        task_family=db_task.task_family,
        task_data=db_task.task_data,
        version=db_task.version,
        created_at=db_task.created_at,
        status=status,
        started_at=started_at,
        completed_at=completed_at,
        error_message=error_message,
    )


@router.post(
    "/{run_id}/tasks/{task_id}/complete", response_model=TaskWithStatusResponse
)
async def complete_task(run_id: str, task_id: str, db: AsyncSession = Depends(get_db)):
    """Mark a task as completed within a run."""
    run = await db.get(Run, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    result = await db.execute(
        select(Task)
        .where(Task.workspace_id == run.workspace_id)
        .where(Task.task_id == task_id)
    )
    db_task = result.scalar_one_or_none()
    if not db_task:
        raise HTTPException(status_code=404, detail="Task not found")

    event = Event(
        run_id=run_id,
        task_id=db_task.id,
        event_type=EventType.TASK_COMPLETED,
    )
    db.add(event)
    await db.commit()

    from stardag_api.services.status import get_task_status_in_run

    status, started_at, completed_at, error_message = await get_task_status_in_run(
        db, run_id, db_task.id
    )

    return TaskWithStatusResponse(
        id=db_task.id,
        task_id=db_task.task_id,
        workspace_id=db_task.workspace_id,
        task_namespace=db_task.task_namespace,
        task_family=db_task.task_family,
        task_data=db_task.task_data,
        version=db_task.version,
        created_at=db_task.created_at,
        status=status,
        started_at=started_at,
        completed_at=completed_at,
        error_message=error_message,
    )


@router.post("/{run_id}/tasks/{task_id}/fail", response_model=TaskWithStatusResponse)
async def fail_task(
    run_id: str,
    task_id: str,
    error_message: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Mark a task as failed within a run."""
    run = await db.get(Run, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    result = await db.execute(
        select(Task)
        .where(Task.workspace_id == run.workspace_id)
        .where(Task.task_id == task_id)
    )
    db_task = result.scalar_one_or_none()
    if not db_task:
        raise HTTPException(status_code=404, detail="Task not found")

    event = Event(
        run_id=run_id,
        task_id=db_task.id,
        event_type=EventType.TASK_FAILED,
        error_message=error_message,
    )
    db.add(event)
    await db.commit()

    from stardag_api.services.status import get_task_status_in_run

    status, started_at, completed_at, err_msg = await get_task_status_in_run(
        db, run_id, db_task.id
    )

    return TaskWithStatusResponse(
        id=db_task.id,
        task_id=db_task.task_id,
        workspace_id=db_task.workspace_id,
        task_namespace=db_task.task_namespace,
        task_family=db_task.task_family,
        task_data=db_task.task_data,
        version=db_task.version,
        created_at=db_task.created_at,
        status=status,
        started_at=started_at,
        completed_at=completed_at,
        error_message=err_msg,
    )


@router.get("/{run_id}/tasks", response_model=list[TaskWithStatusResponse])
async def list_tasks_in_run(run_id: str, db: AsyncSession = Depends(get_db)):
    """List all tasks in a run with their status."""
    run = await db.get(Run, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    # Get all tasks that have events in this run
    result = await db.execute(
        select(Task)
        .join(Event, Event.task_id == Task.id)
        .where(Event.run_id == run_id)
        .distinct()
    )
    tasks = result.scalars().all()

    # Get all statuses
    statuses = await get_all_task_statuses_in_run(db, run_id)

    responses = []
    for task in tasks:
        status, started_at, completed_at, error_message = statuses.get(
            task.id, (TaskStatus.PENDING, None, None, None)
        )
        responses.append(
            TaskWithStatusResponse(
                id=task.id,
                task_id=task.task_id,
                workspace_id=task.workspace_id,
                task_namespace=task.task_namespace,
                task_family=task.task_family,
                task_data=task.task_data,
                version=task.version,
                created_at=task.created_at,
                status=status,
                started_at=started_at,
                completed_at=completed_at,
                error_message=error_message,
            )
        )

    return responses


@router.get("/{run_id}/events", response_model=list[EventResponse])
async def list_run_events(run_id: str, db: AsyncSession = Depends(get_db)):
    """List all events for a run."""
    run = await db.get(Run, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    result = await db.execute(
        select(Event).where(Event.run_id == run_id).order_by(Event.created_at.asc())
    )
    events = result.scalars().all()

    return [
        EventResponse(
            id=e.id,
            run_id=e.run_id,
            task_id=e.task_id,
            event_type=e.event_type,
            created_at=e.created_at,
            error_message=e.error_message,
            event_metadata=e.event_metadata,
        )
        for e in events
    ]


@router.get("/{run_id}/graph", response_model=TaskGraphResponse)
async def get_run_graph(run_id: str, db: AsyncSession = Depends(get_db)):
    """Get the task graph for a run (for visualization)."""
    run = await db.get(Run, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    # Get all tasks in this run
    result = await db.execute(
        select(Task)
        .join(Event, Event.task_id == Task.id)
        .where(Event.run_id == run_id)
        .distinct()
    )
    tasks = result.scalars().all()
    task_ids = {t.id for t in tasks}

    # Get statuses
    statuses = await get_all_task_statuses_in_run(db, run_id)

    # Build nodes
    nodes = []
    for task in tasks:
        status, _, _, _ = statuses.get(task.id, (TaskStatus.PENDING, None, None, None))
        nodes.append(
            TaskNode(
                id=task.id,
                task_id=task.task_id,
                task_family=task.task_family,
                task_namespace=task.task_namespace,
                status=status,
            )
        )

    # Get edges (only between tasks in this run)
    edge_result = await db.execute(
        select(TaskDependency).where(
            TaskDependency.upstream_task_id.in_(task_ids),
            TaskDependency.downstream_task_id.in_(task_ids),
        )
    )
    deps = edge_result.scalars().all()

    edges = [
        TaskEdge(source=d.upstream_task_id, target=d.downstream_task_id) for d in deps
    ]

    return TaskGraphResponse(nodes=nodes, edges=edges)
