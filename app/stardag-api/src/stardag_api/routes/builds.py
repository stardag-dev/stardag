"""Build management routes - primary interface for SDK."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.auth import (
    SdkAuth,
    require_sdk_auth,
)
from stardag_api.db import get_db
from stardag_api.models import (
    Build,
    Event,
    EventType,
    Task,
    TaskDependency,
    TaskStatus,
)
from stardag_api.schemas import (
    BuildCreate,
    BuildListResponse,
    BuildResponse,
    EventResponse,
    TaskCreate,
    TaskEdge,
    TaskGraphResponse,
    TaskNode,
    TaskResponse,
    TaskWithStatusResponse,
)
from stardag_api.services import generate_build_slug, get_build_status
from stardag_api.services.status import get_all_task_statuses_in_build

router = APIRouter(prefix="/builds", tags=["builds"])


# --- Build CRUD ---


@router.post("", response_model=BuildResponse, status_code=201)
async def create_build(
    build: BuildCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[SdkAuth, Depends(require_sdk_auth)],
):
    """Create a new build.

    This is the entry point for SDK - creates a new build and returns its ID.
    Requires API key authentication (recommended) or JWT token with workspace_id.
    The workspace is determined from the authentication context.
    """
    # Generate memorable slug
    name = generate_build_slug()

    # Use workspace from auth context (API key determines workspace)
    db_build = Build(
        workspace_id=auth.workspace_id,
        user_id=auth.user.id if auth.user else None,
        name=name,
        description=build.description,
        commit_hash=build.commit_hash,
        root_task_ids=build.root_task_ids,
    )
    db.add(db_build)
    await db.flush()  # Get the build ID

    # Create BUILD_STARTED event
    start_event = Event(
        build_id=db_build.id,
        task_id=None,
        event_type=EventType.BUILD_STARTED,
    )
    db.add(start_event)

    await db.commit()
    await db.refresh(db_build)

    # Build response with derived status
    status, started_at, completed_at = await get_build_status(db, db_build.id)

    return BuildResponse(
        id=db_build.id,
        workspace_id=db_build.workspace_id,
        user_id=db_build.user_id,
        name=db_build.name,
        description=db_build.description,
        commit_hash=db_build.commit_hash,
        root_task_ids=db_build.root_task_ids,
        created_at=db_build.created_at,
        status=status,
        started_at=started_at,
        completed_at=completed_at,
    )


@router.get("", response_model=BuildListResponse)
async def list_builds(
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[SdkAuth, Depends(require_sdk_auth)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    """List builds in a workspace.

    Requires authentication via API key or JWT token with workspace_id.
    The workspace is determined from the authentication context.
    """
    workspace_id = auth.workspace_id
    query = select(Build).where(Build.workspace_id == workspace_id)
    count_query = (
        select(func.count())
        .select_from(Build)
        .where(Build.workspace_id == workspace_id)
    )

    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0

    result = await db.execute(
        query.order_by(Build.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    builds = result.scalars().all()

    # Build responses with derived status
    build_responses = []
    for build in builds:
        status, started_at, completed_at = await get_build_status(db, build.id)
        build_responses.append(
            BuildResponse(
                id=build.id,
                workspace_id=build.workspace_id,
                user_id=build.user_id,
                name=build.name,
                description=build.description,
                commit_hash=build.commit_hash,
                root_task_ids=build.root_task_ids,
                created_at=build.created_at,
                status=status,
                started_at=started_at,
                completed_at=completed_at,
            )
        )

    return BuildListResponse(
        builds=build_responses,
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/{build_id}", response_model=BuildResponse)
async def get_build(
    build_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[SdkAuth, Depends(require_sdk_auth)],
):
    """Get a build by ID with derived status.

    Requires authentication via API key or JWT token with workspace_id.
    """
    build = await db.get(Build, build_id)
    if not build:
        raise HTTPException(status_code=404, detail="Build not found")

    # Verify build belongs to authenticated workspace
    if build.workspace_id != auth.workspace_id:
        raise HTTPException(
            status_code=403, detail="Build does not belong to this workspace"
        )

    status, started_at, completed_at = await get_build_status(db, build.id)

    return BuildResponse(
        id=build.id,
        workspace_id=build.workspace_id,
        user_id=build.user_id,
        name=build.name,
        description=build.description,
        commit_hash=build.commit_hash,
        root_task_ids=build.root_task_ids,
        created_at=build.created_at,
        status=status,
        started_at=started_at,
        completed_at=completed_at,
    )


@router.post("/{build_id}/complete", response_model=BuildResponse)
async def complete_build(
    build_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[SdkAuth, Depends(require_sdk_auth)],
):
    """Mark a build as completed."""
    build = await db.get(Build, build_id)
    if not build:
        raise HTTPException(status_code=404, detail="Build not found")

    # Verify build belongs to authenticated workspace
    if build.workspace_id != auth.workspace_id:
        raise HTTPException(
            status_code=403, detail="Build does not belong to this workspace"
        )

    event = Event(
        build_id=build_id,
        task_id=None,
        event_type=EventType.BUILD_COMPLETED,
    )
    db.add(event)
    await db.commit()

    status, started_at, completed_at = await get_build_status(db, build.id)

    return BuildResponse(
        id=build.id,
        workspace_id=build.workspace_id,
        user_id=build.user_id,
        name=build.name,
        description=build.description,
        commit_hash=build.commit_hash,
        root_task_ids=build.root_task_ids,
        created_at=build.created_at,
        status=status,
        started_at=started_at,
        completed_at=completed_at,
    )


@router.post("/{build_id}/fail", response_model=BuildResponse)
async def fail_build(
    build_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[SdkAuth, Depends(require_sdk_auth)],
    error_message: str | None = None,
):
    """Mark a build as failed."""
    build = await db.get(Build, build_id)
    if not build:
        raise HTTPException(status_code=404, detail="Build not found")

    # Verify build belongs to authenticated workspace
    if build.workspace_id != auth.workspace_id:
        raise HTTPException(
            status_code=403, detail="Build does not belong to this workspace"
        )

    event = Event(
        build_id=build_id,
        task_id=None,
        event_type=EventType.BUILD_FAILED,
        error_message=error_message,
    )
    db.add(event)
    await db.commit()

    status, started_at, completed_at = await get_build_status(db, build.id)

    return BuildResponse(
        id=build.id,
        workspace_id=build.workspace_id,
        user_id=build.user_id,
        name=build.name,
        description=build.description,
        commit_hash=build.commit_hash,
        root_task_ids=build.root_task_ids,
        created_at=build.created_at,
        status=status,
        started_at=started_at,
        completed_at=completed_at,
    )


# --- Tasks within Builds ---


@router.post("/{build_id}/tasks", response_model=TaskResponse, status_code=201)
async def register_task(
    build_id: str,
    task: TaskCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[SdkAuth, Depends(require_sdk_auth)],
):
    """Register a task to a build.

    If the task already exists in the workspace, it will be reused.
    Creates a TASK_PENDING event for this build.
    """
    build = await db.get(Build, build_id)
    if not build:
        raise HTTPException(status_code=404, detail="Build not found")

    # Verify build belongs to authenticated workspace
    if build.workspace_id != auth.workspace_id:
        raise HTTPException(
            status_code=403, detail="Build does not belong to this workspace"
        )

    # Check if task already exists in workspace
    result = await db.execute(
        select(Task)
        .where(Task.workspace_id == build.workspace_id)
        .where(Task.task_id == task.task_id)
    )
    db_task = result.scalar_one_or_none()

    if not db_task:
        # Create new task
        db_task = Task(
            task_id=task.task_id,
            workspace_id=build.workspace_id,
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
                .where(Task.workspace_id == build.workspace_id)
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

    # Create TASK_PENDING event for this build
    event = Event(
        build_id=build_id,
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


@router.post("/{build_id}/tasks/{task_id}/start", response_model=TaskWithStatusResponse)
async def start_task(
    build_id: str,
    task_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[SdkAuth, Depends(require_sdk_auth)],
):
    """Mark a task as started within a build."""
    build = await db.get(Build, build_id)
    if not build:
        raise HTTPException(status_code=404, detail="Build not found")

    # Verify build belongs to authenticated workspace
    if build.workspace_id != auth.workspace_id:
        raise HTTPException(
            status_code=403, detail="Build does not belong to this workspace"
        )

    # Find task by task_id (hash) in workspace
    result = await db.execute(
        select(Task)
        .where(Task.workspace_id == build.workspace_id)
        .where(Task.task_id == task_id)
    )
    db_task = result.scalar_one_or_none()
    if not db_task:
        raise HTTPException(status_code=404, detail="Task not found")

    event = Event(
        build_id=build_id,
        task_id=db_task.id,
        event_type=EventType.TASK_STARTED,
    )
    db.add(event)
    await db.commit()

    from stardag_api.services.status import get_task_status_in_build

    status, started_at, completed_at, error_message = await get_task_status_in_build(
        db, build_id, db_task.id
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
    "/{build_id}/tasks/{task_id}/complete", response_model=TaskWithStatusResponse
)
async def complete_task(
    build_id: str,
    task_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[SdkAuth, Depends(require_sdk_auth)],
):
    """Mark a task as completed within a build."""
    build = await db.get(Build, build_id)
    if not build:
        raise HTTPException(status_code=404, detail="Build not found")

    # Verify build belongs to authenticated workspace
    if build.workspace_id != auth.workspace_id:
        raise HTTPException(
            status_code=403, detail="Build does not belong to this workspace"
        )

    result = await db.execute(
        select(Task)
        .where(Task.workspace_id == build.workspace_id)
        .where(Task.task_id == task_id)
    )
    db_task = result.scalar_one_or_none()
    if not db_task:
        raise HTTPException(status_code=404, detail="Task not found")

    event = Event(
        build_id=build_id,
        task_id=db_task.id,
        event_type=EventType.TASK_COMPLETED,
    )
    db.add(event)
    await db.commit()

    from stardag_api.services.status import get_task_status_in_build

    status, started_at, completed_at, error_message = await get_task_status_in_build(
        db, build_id, db_task.id
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


@router.post("/{build_id}/tasks/{task_id}/fail", response_model=TaskWithStatusResponse)
async def fail_task(
    build_id: str,
    task_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[SdkAuth, Depends(require_sdk_auth)],
    error_message: str | None = None,
):
    """Mark a task as failed within a build."""
    build = await db.get(Build, build_id)
    if not build:
        raise HTTPException(status_code=404, detail="Build not found")

    # Verify build belongs to authenticated workspace
    if build.workspace_id != auth.workspace_id:
        raise HTTPException(
            status_code=403, detail="Build does not belong to this workspace"
        )

    result = await db.execute(
        select(Task)
        .where(Task.workspace_id == build.workspace_id)
        .where(Task.task_id == task_id)
    )
    db_task = result.scalar_one_or_none()
    if not db_task:
        raise HTTPException(status_code=404, detail="Task not found")

    event = Event(
        build_id=build_id,
        task_id=db_task.id,
        event_type=EventType.TASK_FAILED,
        error_message=error_message,
    )
    db.add(event)
    await db.commit()

    from stardag_api.services.status import get_task_status_in_build

    status, started_at, completed_at, err_msg = await get_task_status_in_build(
        db, build_id, db_task.id
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


@router.get("/{build_id}/tasks", response_model=list[TaskWithStatusResponse])
async def list_tasks_in_build(
    build_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[SdkAuth, Depends(require_sdk_auth)],
):
    """List all tasks in a build with their status.

    Requires authentication via API key or JWT token with workspace_id.
    """
    build = await db.get(Build, build_id)
    if not build:
        raise HTTPException(status_code=404, detail="Build not found")

    # Verify build belongs to authenticated workspace
    if build.workspace_id != auth.workspace_id:
        raise HTTPException(
            status_code=403, detail="Build does not belong to this workspace"
        )

    # Get distinct task IDs that have events in this build
    task_ids_subquery = (
        select(Event.task_id)
        .where(Event.build_id == build_id)
        .where(Event.task_id.isnot(None))
        .distinct()
        .scalar_subquery()
    )

    # Get all tasks by those IDs
    result = await db.execute(select(Task).where(Task.id.in_(task_ids_subquery)))
    tasks = result.scalars().all()

    # Get all statuses
    statuses = await get_all_task_statuses_in_build(db, build_id)

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


@router.get("/{build_id}/events", response_model=list[EventResponse])
async def list_build_events(
    build_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[SdkAuth, Depends(require_sdk_auth)],
):
    """List all events for a build.

    Requires authentication via API key or JWT token with workspace_id.
    """
    build = await db.get(Build, build_id)
    if not build:
        raise HTTPException(status_code=404, detail="Build not found")

    # Verify build belongs to authenticated workspace
    if build.workspace_id != auth.workspace_id:
        raise HTTPException(
            status_code=403, detail="Build does not belong to this workspace"
        )

    result = await db.execute(
        select(Event).where(Event.build_id == build_id).order_by(Event.created_at.asc())
    )
    events = result.scalars().all()

    return [
        EventResponse(
            id=e.id,
            build_id=e.build_id,
            task_id=e.task_id,
            event_type=e.event_type,
            created_at=e.created_at,
            error_message=e.error_message,
            event_metadata=e.event_metadata,
        )
        for e in events
    ]


@router.get("/{build_id}/graph", response_model=TaskGraphResponse)
async def get_build_graph(
    build_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[SdkAuth, Depends(require_sdk_auth)],
):
    """Get the task graph for a build.

    Requires authentication via API key or JWT token with workspace_id.
    """
    build = await db.get(Build, build_id)
    if not build:
        raise HTTPException(status_code=404, detail="Build not found")

    # Verify build belongs to authenticated workspace
    if build.workspace_id != auth.workspace_id:
        raise HTTPException(
            status_code=403, detail="Build does not belong to this workspace"
        )

    # Get distinct task IDs that have events in this build
    task_ids_subquery = (
        select(Event.task_id)
        .where(Event.build_id == build_id)
        .where(Event.task_id.isnot(None))
        .distinct()
        .scalar_subquery()
    )

    # Get all tasks by those IDs
    result = await db.execute(select(Task).where(Task.id.in_(task_ids_subquery)))
    tasks = result.scalars().all()
    task_ids = {t.id for t in tasks}

    # Get statuses
    statuses = await get_all_task_statuses_in_build(db, build_id)

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

    # Get edges (only between tasks in this build)
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
