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
    TaskRegistryAsset,
    TaskStatus,
    User,
)
from stardag_api.schemas import (
    BuildCreate,
    BuildListResponse,
    BuildResponse,
    EventResponse,
    StatusTriggeredByUser,
    TaskCreate,
    TaskEdge,
    TaskEventResponse,
    TaskGraphResponse,
    TaskNode,
    TaskRegistryAssetCreate,
    TaskRegistryAssetListResponse,
    TaskRegistryAssetResponse,
    TaskResponse,
    TaskWithStatusResponse,
)
from stardag_api.services import generate_build_slug, get_build_status
from stardag_api.services.status import (
    get_all_task_global_statuses,
    get_task_status_in_build,
)

router = APIRouter(prefix="/builds", tags=["builds"])


# --- Helpers ---


async def _get_triggered_by_user(
    db: AsyncSession, external_id: str | None
) -> StatusTriggeredByUser | None:
    """Look up user by external_id and return StatusTriggeredByUser or None."""
    if not external_id:
        return None

    result = await db.execute(select(User).where(User.external_id == external_id))
    user = result.scalar_one_or_none()
    if not user:
        return None

    return StatusTriggeredByUser(
        id=user.external_id,
        email=user.email or "",
        display_name=user.display_name,
    )


async def _get_build_and_task(
    build_id: str,
    task_id: str,
    db: AsyncSession,
    auth: SdkAuth,
) -> tuple[Build, Task]:
    """Get build and task, verifying ownership. Raises HTTPException on errors."""
    build = await db.get(Build, build_id)
    if not build:
        raise HTTPException(status_code=404, detail="Build not found")

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

    return build, db_task


async def _create_task_event(
    build_id: str,
    task_id: str,
    event_type: EventType,
    db: AsyncSession,
    auth: SdkAuth,
    error_message: str | None = None,
) -> TaskEventResponse:
    """Create a task event and return slim response."""
    _, db_task = await _get_build_and_task(build_id, task_id, db, auth)

    event = Event(
        build_id=build_id,
        task_id=db_task.id,
        event_type=event_type,
        error_message=error_message,
    )
    db.add(event)
    await db.commit()

    status, _, _, _ = await get_task_status_in_build(db, build_id, db_task.id)

    return TaskEventResponse(task_id=db_task.task_id, status=status)


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
    status, started_at, completed_at, triggered_by_id = await get_build_status(
        db, db_build.id
    )
    triggered_by_user = await _get_triggered_by_user(db, triggered_by_id)

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
        status_triggered_by_user=triggered_by_user,
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
        status, started_at, completed_at, triggered_by_id = await get_build_status(
            db, build.id
        )
        triggered_by_user = await _get_triggered_by_user(db, triggered_by_id)
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
                status_triggered_by_user=triggered_by_user,
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

    status, started_at, completed_at, triggered_by_id = await get_build_status(
        db, build.id
    )
    triggered_by_user = await _get_triggered_by_user(db, triggered_by_id)

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
        status_triggered_by_user=triggered_by_user,
    )


@router.post("/{build_id}/complete", response_model=BuildResponse)
async def complete_build(
    build_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[SdkAuth, Depends(require_sdk_auth)],
    triggered_by_user_id: str | None = None,
):
    """Mark a build as completed.

    Args:
        triggered_by_user_id: Optional user ID if this is a manual override from UI.
    """
    build = await db.get(Build, build_id)
    if not build:
        raise HTTPException(status_code=404, detail="Build not found")

    # Verify build belongs to authenticated workspace
    if build.workspace_id != auth.workspace_id:
        raise HTTPException(
            status_code=403, detail="Build does not belong to this workspace"
        )

    # Store user ID in metadata if this was user-triggered
    event_metadata = (
        {"triggered_by_user_id": triggered_by_user_id} if triggered_by_user_id else None
    )

    event = Event(
        build_id=build_id,
        task_id=None,
        event_type=EventType.BUILD_COMPLETED,
        event_metadata=event_metadata,
    )
    db.add(event)
    await db.commit()

    status, started_at, completed_at, triggered_by_id = await get_build_status(
        db, build.id
    )
    triggered_by_user = await _get_triggered_by_user(db, triggered_by_id)

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
        status_triggered_by_user=triggered_by_user,
    )


@router.post("/{build_id}/fail", response_model=BuildResponse)
async def fail_build(
    build_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[SdkAuth, Depends(require_sdk_auth)],
    error_message: str | None = None,
    triggered_by_user_id: str | None = None,
):
    """Mark a build as failed.

    Args:
        error_message: Optional error message.
        triggered_by_user_id: Optional user ID if this is a manual override from UI.
    """
    build = await db.get(Build, build_id)
    if not build:
        raise HTTPException(status_code=404, detail="Build not found")

    # Verify build belongs to authenticated workspace
    if build.workspace_id != auth.workspace_id:
        raise HTTPException(
            status_code=403, detail="Build does not belong to this workspace"
        )

    # Store user ID in metadata if this was user-triggered
    event_metadata = (
        {"triggered_by_user_id": triggered_by_user_id} if triggered_by_user_id else None
    )

    event = Event(
        build_id=build_id,
        task_id=None,
        event_type=EventType.BUILD_FAILED,
        error_message=error_message,
        event_metadata=event_metadata,
    )
    db.add(event)
    await db.commit()

    status, started_at, completed_at, triggered_by_id = await get_build_status(
        db, build.id
    )
    triggered_by_user = await _get_triggered_by_user(db, triggered_by_id)

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
        status_triggered_by_user=triggered_by_user,
    )


@router.post("/{build_id}/cancel", response_model=BuildResponse)
async def cancel_build(
    build_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[SdkAuth, Depends(require_sdk_auth)],
    triggered_by_user_id: str | None = None,
):
    """Cancel a build.

    Args:
        triggered_by_user_id: Optional user ID if this is a manual override from UI.
    """
    build = await db.get(Build, build_id)
    if not build:
        raise HTTPException(status_code=404, detail="Build not found")

    # Verify build belongs to authenticated workspace
    if build.workspace_id != auth.workspace_id:
        raise HTTPException(
            status_code=403, detail="Build does not belong to this workspace"
        )

    # Store user ID in metadata if this was user-triggered
    event_metadata = (
        {"triggered_by_user_id": triggered_by_user_id} if triggered_by_user_id else None
    )

    event = Event(
        build_id=build_id,
        task_id=None,
        event_type=EventType.BUILD_CANCELLED,
        event_metadata=event_metadata,
    )
    db.add(event)
    await db.commit()

    status, started_at, completed_at, triggered_by_id = await get_build_status(
        db, build.id
    )
    triggered_by_user = await _get_triggered_by_user(db, triggered_by_id)

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
        status_triggered_by_user=triggered_by_user,
    )


@router.post("/{build_id}/exit-early", response_model=BuildResponse)
async def exit_early(
    build_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[SdkAuth, Depends(require_sdk_auth)],
    reason: str | None = None,
):
    """Mark a build as exited early (all remaining tasks running in other builds)."""
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
        event_type=EventType.BUILD_EXIT_EARLY,
        error_message=reason,  # Reuse error_message field for the reason
    )
    db.add(event)
    await db.commit()

    status, started_at, completed_at, triggered_by_id = await get_build_status(
        db, build.id
    )
    triggered_by_user = await _get_triggered_by_user(db, triggered_by_id)

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
        status_triggered_by_user=triggered_by_user,
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
    task_already_existed = db_task is not None

    if not db_task:
        # Create new task
        db_task = Task(
            task_id=task.task_id,
            workspace_id=build.workspace_id,
            task_namespace=task.task_namespace,
            task_name=task.task_name,
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
    pending_event = Event(
        build_id=build_id,
        task_id=db_task.id,
        event_type=EventType.TASK_PENDING,
    )
    db.add(pending_event)

    # If task already existed, also create TASK_REFERENCED event
    # This allows distinguishing between builds that first registered the task
    # vs builds that are referencing an existing task
    if task_already_existed:
        referenced_event = Event(
            build_id=build_id,
            task_id=db_task.id,
            event_type=EventType.TASK_REFERENCED,
        )
        db.add(referenced_event)

    await db.commit()
    await db.refresh(db_task)

    return TaskResponse(
        id=db_task.id,
        task_id=db_task.task_id,
        workspace_id=db_task.workspace_id,
        task_namespace=db_task.task_namespace,
        task_name=db_task.task_name,
        task_data=db_task.task_data,
        version=db_task.version,
        created_at=db_task.created_at,
    )


@router.post("/{build_id}/tasks/{task_id}/start", response_model=TaskEventResponse)
async def start_task(
    build_id: str,
    task_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[SdkAuth, Depends(require_sdk_auth)],
):
    """Mark a task as started within a build."""
    return await _create_task_event(build_id, task_id, EventType.TASK_STARTED, db, auth)


@router.post("/{build_id}/tasks/{task_id}/complete", response_model=TaskEventResponse)
async def complete_task(
    build_id: str,
    task_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[SdkAuth, Depends(require_sdk_auth)],
):
    """Mark a task as completed within a build."""
    return await _create_task_event(
        build_id, task_id, EventType.TASK_COMPLETED, db, auth
    )


@router.post("/{build_id}/tasks/{task_id}/fail", response_model=TaskEventResponse)
async def fail_task(
    build_id: str,
    task_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[SdkAuth, Depends(require_sdk_auth)],
    error_message: str | None = None,
):
    """Mark a task as failed within a build."""
    return await _create_task_event(
        build_id, task_id, EventType.TASK_FAILED, db, auth, error_message
    )


@router.post("/{build_id}/tasks/{task_id}/suspend", response_model=TaskEventResponse)
async def suspend_task(
    build_id: str,
    task_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[SdkAuth, Depends(require_sdk_auth)],
):
    """Mark a task as suspended (waiting for dynamic dependencies)."""
    return await _create_task_event(
        build_id, task_id, EventType.TASK_SUSPENDED, db, auth
    )


@router.post("/{build_id}/tasks/{task_id}/resume", response_model=TaskEventResponse)
async def resume_task(
    build_id: str,
    task_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[SdkAuth, Depends(require_sdk_auth)],
):
    """Mark a task as resumed (dynamic dependencies completed)."""
    return await _create_task_event(build_id, task_id, EventType.TASK_RESUMED, db, auth)


@router.post("/{build_id}/tasks/{task_id}/cancel", response_model=TaskEventResponse)
async def cancel_task(
    build_id: str,
    task_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[SdkAuth, Depends(require_sdk_auth)],
):
    """Cancel a task within a build."""
    return await _create_task_event(
        build_id, task_id, EventType.TASK_CANCELLED, db, auth
    )


@router.post(
    "/{build_id}/tasks/{task_id}/waiting-for-lock", response_model=TaskEventResponse
)
async def task_waiting_for_lock(
    build_id: str,
    task_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[SdkAuth, Depends(require_sdk_auth)],
    lock_owner: str | None = None,
):
    """Record that a task is waiting for a global lock held by another build."""
    _, db_task = await _get_build_and_task(build_id, task_id, db, auth)

    # Store lock owner info in event_metadata if provided
    event_metadata = {"lock_owner": lock_owner} if lock_owner else None

    event = Event(
        build_id=build_id,
        task_id=db_task.id,
        event_type=EventType.TASK_WAITING_FOR_LOCK,
        event_metadata=event_metadata,
    )
    db.add(event)
    await db.commit()

    status, _, _, _ = await get_task_status_in_build(db, build_id, db_task.id)

    return TaskEventResponse(task_id=db_task.task_id, status=status)


@router.post(
    "/{build_id}/tasks/{task_id}/assets",
    response_model=TaskRegistryAssetListResponse,
    status_code=201,
)
async def upload_task_registry_assets(
    build_id: str,
    task_id: str,
    assets: list[TaskRegistryAssetCreate],
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[SdkAuth, Depends(require_sdk_auth)],
):
    """Upload registry assets for a completed task.

    Assets are rich outputs like markdown reports or JSON data that
    can be viewed in the UI.

    Body format:
    - For markdown: {"content": "<markdown string>"}
    - For json: the actual JSON data dict
    """
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

    created_assets = []
    for asset in assets:
        # Check if asset with same type and name already exists
        existing_result = await db.execute(
            select(TaskRegistryAsset)
            .where(TaskRegistryAsset.task_pk == db_task.id)
            .where(TaskRegistryAsset.asset_type == asset.type)
            .where(TaskRegistryAsset.name == asset.name)
        )
        existing_asset = existing_result.scalar_one_or_none()

        if existing_asset:
            # Update existing asset
            existing_asset.body_json = asset.body
            db_asset = existing_asset
        else:
            # Create new asset
            db_asset = TaskRegistryAsset(
                task_pk=db_task.id,
                workspace_id=build.workspace_id,
                asset_type=asset.type,
                name=asset.name,
                body_json=asset.body,
            )
            db.add(db_asset)

        await db.flush()
        created_assets.append(db_asset)

    await db.commit()

    # Build response
    asset_responses = [
        TaskRegistryAssetResponse(
            id=db_asset.id,
            task_id=db_task.task_id,
            asset_type=db_asset.asset_type,
            name=db_asset.name,
            body=db_asset.body_json,
            created_at=db_asset.created_at,
        )
        for db_asset in created_assets
    ]

    return TaskRegistryAssetListResponse(assets=asset_responses)


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
    task_ids = [t.id for t in tasks]

    # Get global statuses (considering events from ALL builds)
    statuses = await get_all_task_global_statuses(db, task_ids)

    # Get asset counts per task
    asset_counts: dict[int, int] = {}
    if task_ids:
        asset_count_result = await db.execute(
            select(TaskRegistryAsset.task_pk, func.count(TaskRegistryAsset.id))
            .where(TaskRegistryAsset.task_pk.in_(task_ids))
            .group_by(TaskRegistryAsset.task_pk)
        )
        asset_counts = {row[0]: row[1] for row in asset_count_result.all()}

    responses = []
    for task in tasks:
        (
            status,
            started_at,
            completed_at,
            error_message,
            status_build_id,
            waiting_for_lock,
        ) = statuses.get(task.id, (TaskStatus.PENDING, None, None, None, None, False))
        responses.append(
            TaskWithStatusResponse(
                id=task.id,
                task_id=task.task_id,
                workspace_id=task.workspace_id,
                task_namespace=task.task_namespace,
                task_name=task.task_name,
                task_data=task.task_data,
                version=task.version,
                created_at=task.created_at,
                status=status,
                started_at=started_at,
                completed_at=completed_at,
                error_message=error_message,
                asset_count=asset_counts.get(task.id, 0),
                waiting_for_lock=waiting_for_lock,
                status_build_id=status_build_id,
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
    task_ids_list = [t.id for t in tasks]
    task_ids = set(task_ids_list)

    # Get global statuses (considering events from ALL builds)
    statuses = await get_all_task_global_statuses(db, task_ids_list)

    # Get asset counts per task
    asset_counts: dict[int, int] = {}
    if task_ids:
        asset_count_result = await db.execute(
            select(TaskRegistryAsset.task_pk, func.count(TaskRegistryAsset.id))
            .where(TaskRegistryAsset.task_pk.in_(task_ids))
            .group_by(TaskRegistryAsset.task_pk)
        )
        asset_counts = {row[0]: row[1] for row in asset_count_result.all()}

    # Build nodes
    nodes = []
    for task in tasks:
        status, _, _, _, _, _ = statuses.get(
            task.id, (TaskStatus.PENDING, None, None, None, None, False)
        )
        nodes.append(
            TaskNode(
                id=task.id,
                task_id=task.task_id,
                task_name=task.task_name,
                task_namespace=task.task_namespace,
                status=status,
                asset_count=asset_counts.get(task.id, 0),
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
