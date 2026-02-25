"""Build management routes - primary interface for SDK."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.auth import (
    SdkAuth,
    require_sdk_auth,
)
from stardag_api.config import limits_settings
from stardag_api.db import get_db
from stardag_api.limits import (
    ErrorCode,
    LimitExceededError,
    check_entity_creation_limit,
    check_payload_size,
    check_rate_limit,
    check_structural_limit,
    record_entity_created,
)
from stardag_api.models import (
    Build,
    Event,
    EventType,
    Task,
    TaskDependency,
    TaskArtifact,
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
    TaskArtifactCreate,
    TaskArtifactListResponse,
    TaskArtifactResponse,
    TaskResponse,
    TaskWithStatusResponse,
)
from stardag_api.services import generate_build_slug, get_build_status
from stardag_api.services.status import (
    get_all_task_global_statuses,
    get_task_status_in_build,
)

router = APIRouter(prefix="/builds", tags=["builds"])


def _raise_if_limit_exceeded(error: LimitExceededError | None) -> None:
    """Raise HTTP 429 if a limit check returned an error."""
    if error is None:
        return
    headers = {}
    if error.retry_after is not None:
        headers["Retry-After"] = str(error.retry_after)
    raise HTTPException(
        status_code=429,
        detail=error.model_dump(exclude_none=True),
        headers=headers or None,
    )


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
    build_id: UUID,
    task_id: str,
    db: AsyncSession,
    auth: SdkAuth,
) -> tuple[Build, Task]:
    """Get build and task, verifying ownership. Raises HTTPException on errors."""
    build = await db.get(Build, build_id)
    if not build:
        raise HTTPException(status_code=404, detail="Build not found")

    if build.environment_id != auth.environment_id:
        raise HTTPException(
            status_code=403, detail="Build does not belong to this environment"
        )

    result = await db.execute(
        select(Task)
        .where(Task.environment_id == build.environment_id)
        .where(Task.task_id == task_id)
    )
    db_task = result.scalar_one_or_none()
    if not db_task:
        raise HTTPException(status_code=404, detail="Task not found")

    return build, db_task


async def _create_task_event(
    build_id: UUID,
    task_id: str,
    event_type: EventType,
    db: AsyncSession,
    auth: SdkAuth,
    error_message: str | None = None,
) -> TaskEventResponse:
    """Create a task event and return slim response."""
    # Limit checks
    _raise_if_limit_exceeded(check_rate_limit(auth.workspace_id, limits_settings))
    _raise_if_limit_exceeded(
        await check_entity_creation_limit(
            db, auth.workspace_id, "events", limits_settings
        )
    )

    _, db_task = await _get_build_and_task(build_id, task_id, db, auth)

    event = Event(
        build_id=build_id,
        task_id=db_task.id,
        event_type=event_type,
        error_message=error_message,
    )
    db.add(event)
    await db.commit()

    record_entity_created(auth.workspace_id, "events")

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
    Requires API key authentication (recommended) or JWT token with environment_id.
    The environment is determined from the authentication context.
    """
    # Limit checks
    _raise_if_limit_exceeded(check_rate_limit(auth.workspace_id, limits_settings))
    _raise_if_limit_exceeded(
        await check_entity_creation_limit(
            db, auth.workspace_id, "builds", limits_settings
        )
    )
    _raise_if_limit_exceeded(
        await check_entity_creation_limit(
            db, auth.workspace_id, "events", limits_settings
        )
    )

    # Generate memorable slug
    name = generate_build_slug()

    # Use environment from auth context (API key determines environment)
    db_build = Build(
        environment_id=auth.environment_id,
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

    record_entity_created(auth.workspace_id, "builds")
    record_entity_created(auth.workspace_id, "events")

    # Build response with derived status
    status, started_at, completed_at, triggered_by_id = await get_build_status(
        db, db_build.id
    )
    triggered_by_user = await _get_triggered_by_user(db, triggered_by_id)

    return BuildResponse(
        id=db_build.id,
        environment_id=db_build.environment_id,
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
    """List builds in an environment.

    Requires authentication via API key or JWT token with environment_id.
    The environment is determined from the authentication context.
    """
    environment_id = auth.environment_id
    query = select(Build).where(Build.environment_id == environment_id)
    count_query = (
        select(func.count())
        .select_from(Build)
        .where(Build.environment_id == environment_id)
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
                environment_id=build.environment_id,
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
    build_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[SdkAuth, Depends(require_sdk_auth)],
):
    """Get a build by ID with derived status.

    Requires authentication via API key or JWT token with environment_id.
    """
    build = await db.get(Build, build_id)
    if not build:
        raise HTTPException(status_code=404, detail="Build not found")

    # Verify build belongs to authenticated environment
    if build.environment_id != auth.environment_id:
        raise HTTPException(
            status_code=403, detail="Build does not belong to this environment"
        )

    status, started_at, completed_at, triggered_by_id = await get_build_status(
        db, build.id
    )
    triggered_by_user = await _get_triggered_by_user(db, triggered_by_id)

    return BuildResponse(
        id=build.id,
        environment_id=build.environment_id,
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
    build_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[SdkAuth, Depends(require_sdk_auth)],
    triggered_by_user_id: str | None = None,
):
    """Mark a build as completed.

    Args:
        triggered_by_user_id: Optional user ID if this is a manual override from UI.
    """
    # Limit checks
    _raise_if_limit_exceeded(check_rate_limit(auth.workspace_id, limits_settings))
    _raise_if_limit_exceeded(
        await check_entity_creation_limit(
            db, auth.workspace_id, "events", limits_settings
        )
    )

    build = await db.get(Build, build_id)
    if not build:
        raise HTTPException(status_code=404, detail="Build not found")

    # Verify build belongs to authenticated environment
    if build.environment_id != auth.environment_id:
        raise HTTPException(
            status_code=403, detail="Build does not belong to this environment"
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

    record_entity_created(auth.workspace_id, "events")

    status, started_at, completed_at, triggered_by_id = await get_build_status(
        db, build.id
    )
    triggered_by_user = await _get_triggered_by_user(db, triggered_by_id)

    return BuildResponse(
        id=build.id,
        environment_id=build.environment_id,
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
    build_id: UUID,
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
    # Limit checks
    _raise_if_limit_exceeded(check_rate_limit(auth.workspace_id, limits_settings))
    _raise_if_limit_exceeded(
        await check_entity_creation_limit(
            db, auth.workspace_id, "events", limits_settings
        )
    )

    build = await db.get(Build, build_id)
    if not build:
        raise HTTPException(status_code=404, detail="Build not found")

    # Verify build belongs to authenticated environment
    if build.environment_id != auth.environment_id:
        raise HTTPException(
            status_code=403, detail="Build does not belong to this environment"
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

    record_entity_created(auth.workspace_id, "events")

    status, started_at, completed_at, triggered_by_id = await get_build_status(
        db, build.id
    )
    triggered_by_user = await _get_triggered_by_user(db, triggered_by_id)

    return BuildResponse(
        id=build.id,
        environment_id=build.environment_id,
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
    build_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[SdkAuth, Depends(require_sdk_auth)],
    triggered_by_user_id: str | None = None,
):
    """Cancel a build.

    Args:
        triggered_by_user_id: Optional user ID if this is a manual override from UI.
    """
    # Limit checks
    _raise_if_limit_exceeded(check_rate_limit(auth.workspace_id, limits_settings))
    _raise_if_limit_exceeded(
        await check_entity_creation_limit(
            db, auth.workspace_id, "events", limits_settings
        )
    )

    build = await db.get(Build, build_id)
    if not build:
        raise HTTPException(status_code=404, detail="Build not found")

    # Verify build belongs to authenticated environment
    if build.environment_id != auth.environment_id:
        raise HTTPException(
            status_code=403, detail="Build does not belong to this environment"
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

    record_entity_created(auth.workspace_id, "events")

    status, started_at, completed_at, triggered_by_id = await get_build_status(
        db, build.id
    )
    triggered_by_user = await _get_triggered_by_user(db, triggered_by_id)

    return BuildResponse(
        id=build.id,
        environment_id=build.environment_id,
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
    build_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[SdkAuth, Depends(require_sdk_auth)],
    reason: str | None = None,
):
    """Mark a build as exited early (all remaining tasks running in other builds)."""
    # Limit checks
    _raise_if_limit_exceeded(check_rate_limit(auth.workspace_id, limits_settings))
    _raise_if_limit_exceeded(
        await check_entity_creation_limit(
            db, auth.workspace_id, "events", limits_settings
        )
    )

    build = await db.get(Build, build_id)
    if not build:
        raise HTTPException(status_code=404, detail="Build not found")

    # Verify build belongs to authenticated environment
    if build.environment_id != auth.environment_id:
        raise HTTPException(
            status_code=403, detail="Build does not belong to this environment"
        )

    event = Event(
        build_id=build_id,
        task_id=None,
        event_type=EventType.BUILD_EXIT_EARLY,
        error_message=reason,  # Reuse error_message field for the reason
    )
    db.add(event)
    await db.commit()

    record_entity_created(auth.workspace_id, "events")

    status, started_at, completed_at, triggered_by_id = await get_build_status(
        db, build.id
    )
    triggered_by_user = await _get_triggered_by_user(db, triggered_by_id)

    return BuildResponse(
        id=build.id,
        environment_id=build.environment_id,
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
    build_id: UUID,
    task: TaskCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[SdkAuth, Depends(require_sdk_auth)],
):
    """Register a task to a build.

    If the task already exists in the environment, it will be reused and a
    TASK_REFERENCED event is created. Otherwise creates the task and a
    TASK_PENDING event.
    """
    # Limit checks
    _raise_if_limit_exceeded(check_rate_limit(auth.workspace_id, limits_settings))
    _raise_if_limit_exceeded(
        check_payload_size(
            task.task_data,
            limits_settings.max_task_data_bytes,
            ErrorCode.TASK_DATA_SIZE_LIMIT,
            "task_data",
        )
    )
    _raise_if_limit_exceeded(
        check_structural_limit(
            len(task.dependency_task_ids),
            limits_settings.max_dependency_ids_per_task,
            ErrorCode.DEPENDENCY_COUNT_LIMIT,
            "dependency_task_ids",
        )
    )
    _raise_if_limit_exceeded(
        await check_entity_creation_limit(
            db, auth.workspace_id, "events", limits_settings
        )
    )

    build = await db.get(Build, build_id)
    if not build:
        raise HTTPException(status_code=404, detail="Build not found")

    # Verify build belongs to authenticated environment
    if build.environment_id != auth.environment_id:
        raise HTTPException(
            status_code=403, detail="Build does not belong to this environment"
        )

    # Check if task already exists in environment
    result = await db.execute(
        select(Task)
        .where(Task.environment_id == build.environment_id)
        .where(Task.task_id == task.task_id)
    )
    db_task = result.scalar_one_or_none()
    task_already_existed = db_task is not None

    if not db_task:
        # Check task creation limit only for new tasks
        _raise_if_limit_exceeded(
            await check_entity_creation_limit(
                db, auth.workspace_id, "tasks", limits_settings
            )
        )
        # Create new task
        db_task = Task(
            task_id=task.task_id,
            environment_id=build.environment_id,
            task_namespace=task.task_namespace,
            task_name=task.task_name,
            task_data=task.task_data,
            version=task.version,
            output_uri=task.output_uri,
        )
        db.add(db_task)
        await db.flush()  # Get the id

        # Create dependencies
        for dep_task_id in task.dependency_task_ids:
            # Find the upstream task
            dep_result = await db.execute(
                select(Task)
                .where(Task.environment_id == build.environment_id)
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

    # Create appropriate event for this build:
    # - TASK_PENDING if this build first registered the task
    # - TASK_REFERENCED if the task already existed from another build
    event = Event(
        build_id=build_id,
        task_id=db_task.id,
        event_type=EventType.TASK_REFERENCED
        if task_already_existed
        else EventType.TASK_PENDING,
    )
    db.add(event)

    await db.commit()
    await db.refresh(db_task)

    record_entity_created(auth.workspace_id, "events")
    if not task_already_existed:
        record_entity_created(auth.workspace_id, "tasks")

    return TaskResponse(
        id=db_task.id,
        task_id=db_task.task_id,
        environment_id=db_task.environment_id,
        task_namespace=db_task.task_namespace,
        task_name=db_task.task_name,
        task_data=db_task.task_data,
        version=db_task.version,
        output_uri=db_task.output_uri,
        created_at=db_task.created_at,
    )


@router.post("/{build_id}/tasks/{task_id}/start", response_model=TaskEventResponse)
async def start_task(
    build_id: UUID,
    task_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[SdkAuth, Depends(require_sdk_auth)],
):
    """Mark a task as started within a build."""
    return await _create_task_event(build_id, task_id, EventType.TASK_STARTED, db, auth)


@router.post("/{build_id}/tasks/{task_id}/complete", response_model=TaskEventResponse)
async def complete_task(
    build_id: UUID,
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
    build_id: UUID,
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
    build_id: UUID,
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
    build_id: UUID,
    task_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[SdkAuth, Depends(require_sdk_auth)],
):
    """Mark a task as resumed (dynamic dependencies completed)."""
    return await _create_task_event(build_id, task_id, EventType.TASK_RESUMED, db, auth)


@router.post("/{build_id}/tasks/{task_id}/cancel", response_model=TaskEventResponse)
async def cancel_task(
    build_id: UUID,
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
    build_id: UUID,
    task_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[SdkAuth, Depends(require_sdk_auth)],
    lock_owner: str | None = None,
):
    """Record that a task is waiting for a global lock held by another build."""
    # Limit checks
    _raise_if_limit_exceeded(check_rate_limit(auth.workspace_id, limits_settings))
    _raise_if_limit_exceeded(
        await check_entity_creation_limit(
            db, auth.workspace_id, "events", limits_settings
        )
    )

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

    record_entity_created(auth.workspace_id, "events")

    status, _, _, _ = await get_task_status_in_build(db, build_id, db_task.id)

    return TaskEventResponse(task_id=db_task.task_id, status=status)


@router.post(
    "/{build_id}/tasks/{task_id}/artifacts",
    response_model=TaskArtifactListResponse,
    status_code=201,
)
async def upload_task_artifacts(
    build_id: UUID,
    task_id: str,
    artifacts: list[TaskArtifactCreate],
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[SdkAuth, Depends(require_sdk_auth)],
):
    """Upload artifacts for a completed task.

    Artifacts are rich outputs like markdown reports or JSON data that
    can be viewed in the UI.

    Body format:
    - For markdown: {"content": "<markdown string>"}
    - For json: the actual JSON data dict
    """
    # Limit checks
    _raise_if_limit_exceeded(check_rate_limit(auth.workspace_id, limits_settings))
    for artifact in artifacts:
        _raise_if_limit_exceeded(
            check_payload_size(
                artifact.body,
                limits_settings.max_artifact_body_bytes,
                ErrorCode.ARTIFACT_BODY_SIZE_LIMIT,
                "artifact body",
            )
        )
    _raise_if_limit_exceeded(
        await check_entity_creation_limit(
            db, auth.workspace_id, "artifacts", limits_settings, amount=len(artifacts)
        )
    )

    build = await db.get(Build, build_id)
    if not build:
        raise HTTPException(status_code=404, detail="Build not found")

    # Verify build belongs to authenticated environment
    if build.environment_id != auth.environment_id:
        raise HTTPException(
            status_code=403, detail="Build does not belong to this environment"
        )

    # Find task by task_id (hash) in environment
    result = await db.execute(
        select(Task)
        .where(Task.environment_id == build.environment_id)
        .where(Task.task_id == task_id)
    )
    db_task = result.scalar_one_or_none()
    if not db_task:
        raise HTTPException(status_code=404, detail="Task not found")

    # Check artifacts-per-task limit (conservative: counts all submitted artifacts as new,
    # even if some may update existing artifacts - acceptable for guardrails)
    if limits_settings.max_artifacts_per_task is not None:
        existing_count_result = await db.execute(
            select(func.count())
            .select_from(TaskArtifact)
            .where(TaskArtifact.task_pk == db_task.id)
        )
        existing_count = existing_count_result.scalar() or 0
        _raise_if_limit_exceeded(
            check_structural_limit(
                existing_count + len(artifacts),
                limits_settings.max_artifacts_per_task,
                ErrorCode.ARTIFACTS_PER_TASK_LIMIT,
                "artifacts per task",
            )
        )

    created_artifacts = []
    new_artifact_count = 0
    for artifact in artifacts:
        # Check if artifact with same type and name already exists
        existing_result = await db.execute(
            select(TaskArtifact)
            .where(TaskArtifact.task_pk == db_task.id)
            .where(TaskArtifact.artifact_type == artifact.type)
            .where(TaskArtifact.name == artifact.name)
        )
        existing_artifact = existing_result.scalar_one_or_none()

        if existing_artifact:
            # Update existing artifact
            existing_artifact.body_json = artifact.body
            db_artifact = existing_artifact
        else:
            # Create new artifact
            db_artifact = TaskArtifact(
                task_pk=db_task.id,
                environment_id=build.environment_id,
                artifact_type=artifact.type,
                name=artifact.name,
                body_json=artifact.body,
            )
            db.add(db_artifact)
            new_artifact_count += 1

        await db.flush()
        created_artifacts.append(db_artifact)

    await db.commit()

    for _ in range(new_artifact_count):
        record_entity_created(auth.workspace_id, "artifacts")

    # Build response
    artifact_responses = [
        TaskArtifactResponse(
            id=db_artifact.id,
            task_id=db_task.task_id,
            artifact_type=db_artifact.artifact_type,
            name=db_artifact.name,
            body=db_artifact.body_json,
            created_at=db_artifact.created_at,
        )
        for db_artifact in created_artifacts
    ]

    return TaskArtifactListResponse(artifacts=artifact_responses)


@router.get("/{build_id}/tasks", response_model=list[TaskWithStatusResponse])
async def list_tasks_in_build(
    build_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[SdkAuth, Depends(require_sdk_auth)],
):
    """List all tasks in a build with their status.

    Requires authentication via API key or JWT token with environment_id.
    """
    build = await db.get(Build, build_id)
    if not build:
        raise HTTPException(status_code=404, detail="Build not found")

    # Verify build belongs to authenticated environment
    if build.environment_id != auth.environment_id:
        raise HTTPException(
            status_code=403, detail="Build does not belong to this environment"
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

    # Get artifact counts per task
    artifact_counts: dict[UUID, int] = {}
    if task_ids:
        artifact_count_result = await db.execute(
            select(TaskArtifact.task_pk, func.count(TaskArtifact.id))
            .where(TaskArtifact.task_pk.in_(task_ids))
            .group_by(TaskArtifact.task_pk)
        )
        artifact_counts = {row[0]: row[1] for row in artifact_count_result.all()}

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
                environment_id=task.environment_id,
                task_namespace=task.task_namespace,
                task_name=task.task_name,
                task_data=task.task_data,
                version=task.version,
                output_uri=task.output_uri,
                created_at=task.created_at,
                status=status,
                started_at=started_at,
                completed_at=completed_at,
                error_message=error_message,
                artifact_count=artifact_counts.get(task.id, 0),
                waiting_for_lock=waiting_for_lock,
                status_build_id=status_build_id,
            )
        )

    return responses


@router.get("/{build_id}/events", response_model=list[EventResponse])
async def list_build_events(
    build_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[SdkAuth, Depends(require_sdk_auth)],
):
    """List all events for a build.

    Requires authentication via API key or JWT token with environment_id.
    """
    build = await db.get(Build, build_id)
    if not build:
        raise HTTPException(status_code=404, detail="Build not found")

    # Verify build belongs to authenticated environment
    if build.environment_id != auth.environment_id:
        raise HTTPException(
            status_code=403, detail="Build does not belong to this environment"
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
    build_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[SdkAuth, Depends(require_sdk_auth)],
):
    """Get the task graph for a build.

    Requires authentication via API key or JWT token with environment_id.
    """
    build = await db.get(Build, build_id)
    if not build:
        raise HTTPException(status_code=404, detail="Build not found")

    # Verify build belongs to authenticated environment
    if build.environment_id != auth.environment_id:
        raise HTTPException(
            status_code=403, detail="Build does not belong to this environment"
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

    # Get artifact counts per task
    artifact_counts: dict[UUID, int] = {}
    if task_ids:
        artifact_count_result = await db.execute(
            select(TaskArtifact.task_pk, func.count(TaskArtifact.id))
            .where(TaskArtifact.task_pk.in_(task_ids))
            .group_by(TaskArtifact.task_pk)
        )
        artifact_counts = {row[0]: row[1] for row in artifact_count_result.all()}

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
                artifact_count=artifact_counts.get(task.id, 0),
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
