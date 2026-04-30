"""Build management routes - primary interface for SDK."""

from datetime import timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
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
from stardag_api.models.base import generate_uuid7, utc_now
from stardag_api.schemas import (
    AddDependenciesRequest,
    AddDependenciesResponse,
    BuildCreate,
    BuildListResponse,
    BuildResponse,
    EventResponse,
    StatusTriggeredByUser,
    TaskBulkCreate,
    TaskBulkResponse,
    TaskCreate,
    TaskEventResponse,
    TaskGraphExtendedResponse,
    TaskArtifactCreate,
    TaskArtifactListResponse,
    TaskArtifactResponse,
    TaskResponse,
    TaskWithStatusResponse,
)
from stardag_api.services import generate_build_slug, get_build_status
from stardag_api.services.status import (
    apply_event_to_task,
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
    *,
    for_update: bool = False,
) -> tuple[Build, Task]:
    """Get build and task, verifying ownership. Raises HTTPException on errors.

    When ``for_update=True`` issues ``SELECT ... FOR UPDATE`` on the task row
    so concurrent event-creating handlers serialise on the same task. Required
    by anything that does a read-modify-write on the denormalised
    ``Task.latest_*`` columns — without it two concurrent writers can each
    read PENDING, apply different events, and the last-committer wins
    regardless of the priority logic in ``apply_event_to_task`` (e.g. a
    concurrent TASK_STARTED could clobber a TASK_COMPLETED). The lock is
    released on transaction commit, so request duration governs hold time.
    On SQLite the FOR UPDATE clause is silently dropped — fine, since the
    test suite runs single-connection.
    """
    build = await db.get(Build, build_id)
    if not build:
        raise HTTPException(status_code=404, detail="Build not found")

    if build.environment_id != auth.environment_id:
        raise HTTPException(
            status_code=403, detail="Build does not belong to this environment"
        )

    stmt = (
        select(Task)
        .where(Task.environment_id == build.environment_id)
        .where(Task.task_id == task_id)
    )
    if for_update:
        stmt = stmt.with_for_update()
    result = await db.execute(stmt)
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
    commit_hash: str | None = None,
    extra_metadata: dict | None = None,
) -> TaskEventResponse:
    """Create a task event and return slim response."""
    # Limit checks
    _raise_if_limit_exceeded(check_rate_limit(auth.workspace_id, limits_settings))
    _raise_if_limit_exceeded(
        await check_entity_creation_limit(
            db, auth.workspace_id, "events", limits_settings
        )
    )

    # Lock the task row so apply_event_to_task can safely do a
    # read-modify-write on the denormalised latest_* columns. Without the
    # lock, two concurrent event-creators racing on the same task could
    # both observe PENDING, apply different events (e.g. STARTED in one,
    # COMPLETED in the other), and the last committer wins regardless of
    # COMPLETED-stickiness.
    _, db_task = await _get_build_and_task(build_id, task_id, db, auth, for_update=True)

    # Build event_metadata from commit_hash and any extra metadata
    event_metadata: dict | None = None
    if commit_hash or extra_metadata:
        event_metadata = {}
        if commit_hash:
            event_metadata["commit_hash"] = commit_hash
        if extra_metadata:
            event_metadata.update(extra_metadata)

    event = Event(
        build_id=build_id,
        task_id=db_task.id,
        event_type=event_type,
        error_message=error_message,
        event_metadata=event_metadata,
    )
    db.add(event)
    # Flush so event.id and event.created_at are populated before we feed the
    # event into apply_event_to_task. The whole bundle commits atomically.
    await db.flush()
    apply_event_to_task(db_task, event)
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


def _build_event_metadata(
    commit_hash: str | None = None,
    triggered_by_user_id: str | None = None,
) -> dict | None:
    """Build event_metadata dict from optional fields."""
    metadata: dict = {}
    if commit_hash:
        metadata["commit_hash"] = commit_hash
    if triggered_by_user_id:
        metadata["triggered_by_user_id"] = triggered_by_user_id
    return metadata or None


@router.post("/{build_id}/complete", response_model=BuildResponse)
async def complete_build(
    build_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[SdkAuth, Depends(require_sdk_auth)],
    triggered_by_user_id: str | None = None,
    commit_hash: str | None = None,
):
    """Mark a build as completed.

    Args:
        triggered_by_user_id: Optional user ID if this is a manual override from UI.
        commit_hash: Optional git commit hash of the code that ran this build.
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

    event = Event(
        build_id=build_id,
        task_id=None,
        event_type=EventType.BUILD_COMPLETED,
        event_metadata=_build_event_metadata(commit_hash, triggered_by_user_id),
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
    commit_hash: str | None = None,
):
    """Mark a build as failed.

    Args:
        error_message: Optional error message.
        triggered_by_user_id: Optional user ID if this is a manual override from UI.
        commit_hash: Optional git commit hash of the code that ran this build.
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

    event = Event(
        build_id=build_id,
        task_id=None,
        event_type=EventType.BUILD_FAILED,
        error_message=error_message,
        event_metadata=_build_event_metadata(commit_hash, triggered_by_user_id),
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
    commit_hash: str | None = None,
):
    """Cancel a build.

    Args:
        triggered_by_user_id: Optional user ID if this is a manual override from UI.
        commit_hash: Optional git commit hash of the code that ran this build.
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

    event = Event(
        build_id=build_id,
        task_id=None,
        event_type=EventType.BUILD_CANCELLED,
        event_metadata=_build_event_metadata(commit_hash, triggered_by_user_id),
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
    commit_hash: str | None = None,
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
        event_metadata=_build_event_metadata(commit_hash),
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


async def _reconcile_dependency_edges(
    *,
    db: AsyncSession,
    environment_id: UUID,
    downstream_task_pk: UUID,
    upstream_task_ids: list[str],
    is_dynamic: bool,
) -> int:
    """Create any missing upstream tasks (as phantoms) and dependency edges.

    **Phantom-creation is a safety hatch, not the happy path.** With the
    SDK's post-order discover walk (every dep registers before its
    parent) and within-batch ordering of the bulk-register endpoint, the
    upstream ``task_id`` lookup at step 1 always finds existing rows in
    normal operation, so steps 2 + 3 are skipped. Phantoms only appear
    when:
      - A build crashes between registering a parent and registering
        its deps (orphan rows from the failed build).
      - An out-of-band caller registers edges via the
        ``/dependencies`` endpoint pointing at not-yet-registered task
        ids.
      - A future caller registers in pre-order again.
    The rest of the system (UI list, DAG view) treats phantoms as
    placeholder rows and the next register-with-real-data call upgrades
    them in place.

    Issues two statements when every upstream task already exists, or four
    when missing upstream ids must be created as phantoms — independent of
    N otherwise:
      1. SELECT existing tasks WHERE task_id IN (...).
      2. (only if any upstream ids were missing) INSERT ... VALUES (...)
         ON CONFLICT DO NOTHING — bulk phantom insert.
      3. (only if any upstream ids were missing) SELECT to re-fetch PKs.
         Required because ON CONFLICT DO NOTHING + RETURNING only returns
         our own inserted rows; a concurrent caller may have created the
         row first and we still need its PK.
      4. INSERT ... VALUES (...) ON CONFLICT DO NOTHING — bulk edge insert.

    Idempotent: ``ON CONFLICT DO NOTHING`` handles concurrent registrations.
    An edge's ``is_dynamic`` value is set from the *first* successful insert;
    a later call with a different ``is_dynamic`` value does not overwrite the
    existing row. That's intentional — if a dep is both static and yielded
    dynamically (unusual) we record the first observation as authoritative.

    Returns the number of edges inserted by this call. On Postgres the
    asyncpg cursor reports an accurate rowcount; on dialects that don't
    expose rowcount we conservatively report 0 so callers like
    ``AddDependenciesResponse.added`` don't over-claim.
    """
    if not upstream_task_ids:
        return 0

    # Deduplicate upstream ids so we don't propose the same row twice.
    requested_ids = list(dict.fromkeys(upstream_task_ids))
    # Single timestamp for every row this call writes — phantoms and edges
    # share it, which keeps the audit log self-consistent. UUID7 PKs encode
    # time, so per-row order is still preserved within the batch.
    now = utc_now()

    # 1. Find existing tasks for these task_ids in one round-trip.
    existing_result = await db.execute(
        select(Task.id, Task.task_id)
        .where(Task.environment_id == environment_id)
        .where(Task.task_id.in_(requested_ids))
    )
    task_pk_by_task_id: dict[str, UUID] = {
        task_id: pk for pk, task_id in existing_result.all()
    }

    # 2. For any missing ids, batch-insert phantoms. ON CONFLICT DO NOTHING
    # keeps us idempotent under concurrent registrations of the same id.
    missing_ids = [tid for tid in requested_ids if tid not in task_pk_by_task_id]
    if missing_ids:
        phantom_rows = [
            {
                "id": generate_uuid7(),
                "task_id": tid,
                "environment_id": environment_id,
                "task_namespace": "",
                "task_name": tid[:12],
                "task_data": {},
                "is_phantom": True,
                "created_at": now,
                # Match the historical "task with no events shows as PENDING"
                # semantic so phantoms appear consistently in the UI; the
                # is_phantom column distinguishes them for consumers that
                # care.
                "latest_status": TaskStatus.PENDING,
                "latest_waiting_for_lock": False,
            }
            for tid in missing_ids
        ]
        await db.execute(
            pg_insert(Task)
            .values(phantom_rows)
            .on_conflict_do_nothing(constraint="uq_task_environment_taskid")
        )
        # Re-fetch all missing rows in one go (catches both rows we inserted
        # and rows a concurrent writer beat us to). This is required because
        # ``RETURNING`` would only give us our own inserted rows, not the
        # ones we lost the race on.
        refetch_result = await db.execute(
            select(Task.id, Task.task_id)
            .where(Task.environment_id == environment_id)
            .where(Task.task_id.in_(missing_ids))
        )
        for pk, task_id in refetch_result.all():
            task_pk_by_task_id[task_id] = pk

    # Every requested id must now resolve to a PK. The KeyError below is
    # essentially unreachable in practice — under READ COMMITTED the
    # ON CONFLICT DO NOTHING + re-fetch resolves to a row in every
    # realistic ordering. The realistic concurrent-delete failure mode is
    # an FK violation on the edge insert below, not this lookup. The lookup
    # exists as a defence-in-depth tripwire so a future regression would
    # surface a clear error rather than silently dropping a dep.
    edge_rows = []
    for tid in requested_ids:
        upstream_pk = task_pk_by_task_id[tid]
        edge_rows.append(
            {
                "id": generate_uuid7(),
                "upstream_task_id": upstream_pk,
                "downstream_task_id": downstream_task_pk,
                "is_dynamic": is_dynamic,
                "created_at": now,
            }
        )

    # 3. Bulk insert the edge rows.
    edge_stmt = (
        pg_insert(TaskDependency)
        .values(edge_rows)
        .on_conflict_do_nothing(constraint="uq_task_dependency_edge")
    )
    result = await db.execute(edge_stmt)
    # CursorResult.rowcount totals across all VALUES rows on Postgres
    # (with asyncpg, this is reliable even for ON CONFLICT DO NOTHING —
    # only actually-inserted rows are counted). On dialects that don't
    # expose rowcount we report 0 rather than len(edge_rows), since
    # len(edge_rows) would over-claim whenever any conflict occurred.
    inserted = getattr(result, "rowcount", None)
    if inserted is None or inserted < 0:
        inserted = 0
    return inserted


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

    # Check if task already exists in environment. Lock for update so the
    # phantom-upgrade path (mutating an existing row) and the apply_event_to_task
    # call below don't race with a concurrent event-creator on the same task.
    # New rows go through pg_insert ON CONFLICT in the dependency reconcile
    # loop and are race-safe via the unique constraint, so the lock is only
    # needed when an existing row is found.
    result = await db.execute(
        select(Task)
        .where(Task.environment_id == build.environment_id)
        .where(Task.task_id == task.task_id)
        .with_for_update()
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

    elif db_task.is_phantom:
        # Upgrade phantom to real task
        db_task.task_namespace = task.task_namespace
        db_task.task_name = task.task_name
        db_task.task_data = task.task_data
        db_task.version = task.version
        db_task.output_uri = task.output_uri
        db_task.is_phantom = False

    # Reconcile static dependency edges (is_dynamic=False).
    await _reconcile_dependency_edges(
        db=db,
        environment_id=build.environment_id,
        downstream_task_pk=db_task.id,
        upstream_task_ids=task.dependency_task_ids,
        is_dynamic=False,
    )

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
    await db.flush()
    apply_event_to_task(db_task, event)

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


# Cap on the number of tasks per bulk-register call. Bounds memory/transaction
# size on the API side; the SDK's build engine should chunk if it ever exceeds.
_MAX_BULK_REGISTER_TASKS = 1000


@router.post(
    "/{build_id}/tasks/bulk",
    response_model=TaskBulkResponse,
    status_code=201,
)
async def register_tasks_bulk(
    build_id: UUID,
    payload: TaskBulkCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[SdkAuth, Depends(require_sdk_auth)],
):
    """Register multiple tasks to a build in a single transaction.

    Tasks are processed in array order. With the SDK's post-order discover
    walk, deps appear earlier in the array than their parents — so when a
    parent's ``dependency_task_ids`` resolves the API finds existing rows
    (no phantom-creation in ``_reconcile_dependency_edges``). Within the
    same transaction ``db.flush()`` makes earlier tasks visible to later
    SELECTs, so the in-batch ordering carries through correctly.

    Sibling-of single-task registration: same TASK_PENDING /
    TASK_REFERENCED event semantics, same phantom-upgrade behaviour for
    rows left over from prior failed builds.
    """
    raw_tasks = payload.tasks

    if not raw_tasks:
        return TaskBulkResponse(tasks=[])

    if len(raw_tasks) > _MAX_BULK_REGISTER_TASKS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Bulk register limited to {_MAX_BULK_REGISTER_TASKS} tasks per "
                f"call (got {len(raw_tasks)})"
            ),
        )

    # Deduplicate by task_id, keeping the first occurrence. The schema
    # contract documents this so callers can rely on "first wins" semantics
    # for accidental duplicates in a single batch (rather than getting
    # multiple events / repeated dep reconciliation per task).
    seen_ids: set[str] = set()
    tasks_in: list[TaskCreate] = []
    for t in raw_tasks:
        if t.task_id in seen_ids:
            continue
        seen_ids.add(t.task_id)
        tasks_in.append(t)

    # Rate limit (1 per call). The bulk endpoint deliberately doesn't count
    # as N requests — entity-creation limits below cap actual writes.
    _raise_if_limit_exceeded(check_rate_limit(auth.workspace_id, limits_settings))

    # Per-task structural limits.
    for t in tasks_in:
        _raise_if_limit_exceeded(
            check_payload_size(
                t.task_data,
                limits_settings.max_task_data_bytes,
                ErrorCode.TASK_DATA_SIZE_LIMIT,
                "task_data",
            )
        )
        _raise_if_limit_exceeded(
            check_structural_limit(
                len(t.dependency_task_ids),
                limits_settings.max_dependency_ids_per_task,
                ErrorCode.DEPENDENCY_COUNT_LIMIT,
                "dependency_task_ids",
            )
        )

    # Auth/build check first — a probe with a bogus build_id mustn't
    # consume rate-limit budget or fingerprint the workspace's 24h
    # creation limits via timing or error type.
    build = await db.get(Build, build_id)
    if not build:
        raise HTTPException(status_code=404, detail="Build not found")
    if build.environment_id != auth.environment_id:
        raise HTTPException(
            status_code=403, detail="Build does not belong to this environment"
        )

    # Each (post-dedup) task produces exactly one event in this build.
    _raise_if_limit_exceeded(
        await check_entity_creation_limit(
            db,
            auth.workspace_id,
            "events",
            limits_settings,
            amount=len(tasks_in),
        )
    )

    # Pre-query which task_ids already exist in this environment so the
    # 24h tasks limit only counts brand-new task creations.
    #
    # Note: this estimate is computed without ``FOR UPDATE`` and may
    # diverge from the actual new-task count if a concurrent transaction
    # inserts the same ``task_id`` between this SELECT and the bulk
    # INSERT below. In that race we'd skip the limit check for what
    # turns out to be 0 new rows — strictly an under-counting of the
    # actual writes, never an over-counting, so the guard rail stays
    # safe.
    existing_tasks_result = await db.execute(
        select(Task)
        .where(Task.environment_id == build.environment_id)
        .where(Task.task_id.in_([t.task_id for t in tasks_in]))
    )
    existing_tasks: dict[str, Task] = {
        t.task_id: t for t in existing_tasks_result.scalars().all()
    }
    new_task_count_estimate = sum(
        1 for t in tasks_in if t.task_id not in existing_tasks
    )
    if new_task_count_estimate:
        _raise_if_limit_exceeded(
            await check_entity_creation_limit(
                db,
                auth.workspace_id,
                "tasks",
                limits_settings,
                amount=new_task_count_estimate,
            )
        )

    # Lock existing rows in **sorted task_id order** so that two
    # concurrent bulk calls hitting overlapping cached tasks acquire
    # locks in the same order and can't deadlock on each other. We only
    # need FOR UPDATE on rows we'll mutate (phantom upgrades) or on rows
    # whose denormalised ``latest_*`` columns ``apply_event_to_task``
    # touches — i.e., existing rows. Brand-new rows are inserted
    # without a competing writer, no lock needed.
    if existing_tasks:
        sorted_existing_ids = sorted(existing_tasks.keys())
        await db.execute(
            select(Task)
            .where(Task.environment_id == build.environment_id)
            .where(Task.task_id.in_(sorted_existing_ids))
            .order_by(Task.task_id.asc())
            .with_for_update()
        )

    # Phase 1: insert new tasks + upgrade phantoms in-memory.
    # Maintain ``pk_by_task_id`` so we can resolve dependency_task_ids
    # in Python without per-task SELECTs, and keep ORM instances in
    # ``db_task_by_task_id`` so we can apply events to them after
    # flushing.
    now = utc_now()
    pk_by_task_id: dict[str, UUID] = {t_id: t.id for t_id, t in existing_tasks.items()}
    db_task_by_task_id: dict[str, Task] = dict(existing_tasks)

    new_task_count = 0
    for t in tasks_in:
        if t.task_id in existing_tasks:
            db_task = existing_tasks[t.task_id]
            if db_task.is_phantom:
                # Phantom upgrade: real task data overrides the
                # ``tid[:12]`` placeholder. Note that we deliberately do
                # *not* reset latest_status_at / latest_status_event_id
                # here — the apply_event_to_task(TASK_PENDING) call
                # below refreshes them via the
                # ``latest_status == PENDING`` branch in
                # services/status.py:101. If that branch ever gains a
                # phantom-upgrade short-circuit, this needs an explicit
                # reset.
                db_task.task_namespace = t.task_namespace
                db_task.task_name = t.task_name
                db_task.task_data = t.task_data
                db_task.version = t.version
                db_task.output_uri = t.output_uri
                db_task.is_phantom = False
        else:
            # Pre-generate the UUID7 PK so we can resolve dep edges
            # below without an extra round-trip after flush.
            new_pk = generate_uuid7()
            db_task = Task(
                id=new_pk,
                task_id=t.task_id,
                environment_id=build.environment_id,
                task_namespace=t.task_namespace,
                task_name=t.task_name,
                task_data=t.task_data,
                version=t.version,
                output_uri=t.output_uri,
            )
            db.add(db_task)
            pk_by_task_id[t.task_id] = new_pk
            db_task_by_task_id[t.task_id] = db_task
            new_task_count += 1

    # Single flush: SQLAlchemy with asyncpg batches INSERTs of the same
    # entity type into one round-trip when their primary keys are
    # client-generated (which our UUID7 PKs are). For a 1000-task batch
    # this is the difference between 1000 sequential round-trips and
    # one ``executemany``.
    if new_task_count:
        await db.flush()

    # Phase 2: bulk-reconcile dependency edges.
    # Collect all upstream task_ids referenced anywhere in the batch.
    all_upstream_ids: set[str] = set()
    for t in tasks_in:
        all_upstream_ids.update(t.dependency_task_ids)
    # Subtract task_ids already in our map (in-batch deps + pre-existing).
    unknown_upstream_ids = all_upstream_ids - set(pk_by_task_id.keys())
    if unknown_upstream_ids:
        # Look up unknown upstreams in DB — they may be tasks that
        # exist in the env but weren't part of this batch.
        unknown_lookup_result = await db.execute(
            select(Task.id, Task.task_id)
            .where(Task.environment_id == build.environment_id)
            .where(Task.task_id.in_(unknown_upstream_ids))
        )
        for pk, t_id in unknown_lookup_result.all():
            pk_by_task_id[t_id] = pk
        still_unknown = unknown_upstream_ids - set(pk_by_task_id.keys())
        if still_unknown:
            # Safety hatch: edges referencing a task not in the batch
            # *and* not in the DB. Phantom-create. With the SDK's
            # post-order discover walk this should not happen in normal
            # operation; documented in ``_reconcile_dependency_edges``.
            phantom_rows = [
                {
                    "id": generate_uuid7(),
                    "task_id": tid,
                    "environment_id": build.environment_id,
                    "task_namespace": "",
                    "task_name": tid[:12],
                    "task_data": {},
                    "is_phantom": True,
                    "created_at": now,
                    "latest_status": TaskStatus.PENDING,
                    "latest_waiting_for_lock": False,
                }
                for tid in still_unknown
            ]
            await db.execute(
                pg_insert(Task)
                .values(phantom_rows)
                .on_conflict_do_nothing(constraint="uq_task_environment_taskid")
            )
            # Re-fetch (ON CONFLICT DO NOTHING + RETURNING only returns
            # rows we inserted; a concurrent writer may have created
            # the row first and we still need its PK).
            refetch_result = await db.execute(
                select(Task.id, Task.task_id)
                .where(Task.environment_id == build.environment_id)
                .where(Task.task_id.in_(still_unknown))
            )
            for pk, t_id in refetch_result.all():
                pk_by_task_id[t_id] = pk

    # Build edge rows for the whole batch and bulk-insert in one shot.
    edge_rows: list[dict[str, object]] = []
    for t in tasks_in:
        if not t.dependency_task_ids:
            continue
        downstream_pk = pk_by_task_id[t.task_id]
        # Deduplicate within a single task's dep list (the schema
        # constraint allows it, but emitting duplicates is wasteful).
        seen_in_task: set[str] = set()
        for upstream_id in t.dependency_task_ids:
            if upstream_id in seen_in_task:
                continue
            seen_in_task.add(upstream_id)
            edge_rows.append(
                {
                    "id": generate_uuid7(),
                    "upstream_task_id": pk_by_task_id[upstream_id],
                    "downstream_task_id": downstream_pk,
                    "is_dynamic": False,
                    "created_at": now,
                }
            )
    if edge_rows:
        await db.execute(
            pg_insert(TaskDependency)
            .values(edge_rows)
            .on_conflict_do_nothing(constraint="uq_task_dependency_edge")
        )

    # Phase 3: bulk-insert events with explicit per-event timestamps so
    # that ``list_tasks_in_build`` can order tasks by per-build first
    # event in array order. (The endpoint joins against
    # ``min(events.created_at)`` — if every event in this batch shared
    # one timestamp, ordering would fall to ``Task.id``, which is
    # unstable for re-referenced cached tasks whose UUID7 came from an
    # earlier build.)
    events: list[Event] = []
    for i, t in enumerate(tasks_in):
        already_existed = t.task_id in existing_tasks
        events.append(
            Event(
                id=generate_uuid7(),
                build_id=build_id,
                task_id=pk_by_task_id[t.task_id],
                event_type=EventType.TASK_REFERENCED
                if already_existed
                else EventType.TASK_PENDING,
                created_at=now + timedelta(microseconds=i),
            )
        )
    if events:
        db.add_all(events)
        # Apply event semantics in Python on the in-memory ORM rows —
        # ``apply_event_to_task`` reads only fields we set above
        # (event_type, created_at, id, build_id, error_message,
        # event_metadata) and mutates the Task ORM in-place. No DB
        # round-trip needed.
        for t, ev in zip(tasks_in, events):
            apply_event_to_task(db_task_by_task_id[t.task_id], ev)

    # One final flush + commit at the end. Earlier flushes (just the
    # task INSERT batch) populated the rows we need to FK against.
    await db.commit()

    # Build responses in array order from the (now-flushed) ORM
    # instances. Refresh ensures ``created_at`` is populated for newly
    # inserted rows (server-default would have applied at flush time).
    responses: list[TaskResponse] = []
    for t in tasks_in:
        db_task = db_task_by_task_id[t.task_id]
        responses.append(
            TaskResponse(
                id=db_task.id,
                task_id=db_task.task_id,
                environment_id=db_task.environment_id,
                task_namespace=db_task.task_namespace,
                task_name=db_task.task_name,
                task_data=db_task.task_data,
                version=db_task.version,
                output_uri=db_task.output_uri,
                created_at=db_task.created_at,
                is_phantom=db_task.is_phantom,
            )
        )

    # Update entity-count cache for in-process limit tracking.
    for _ in range(len(tasks_in)):
        record_entity_created(auth.workspace_id, "events")
    for _ in range(new_task_count):
        record_entity_created(auth.workspace_id, "tasks")

    return TaskBulkResponse(tasks=responses)


@router.post("/{build_id}/tasks/{task_id}/start", response_model=TaskEventResponse)
async def start_task(
    build_id: UUID,
    task_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[SdkAuth, Depends(require_sdk_auth)],
    commit_hash: str | None = None,
):
    """Mark a task as started within a build."""
    return await _create_task_event(
        build_id, task_id, EventType.TASK_STARTED, db, auth, commit_hash=commit_hash
    )


@router.post("/{build_id}/tasks/{task_id}/complete", response_model=TaskEventResponse)
async def complete_task(
    build_id: UUID,
    task_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[SdkAuth, Depends(require_sdk_auth)],
    commit_hash: str | None = None,
):
    """Mark a task as completed within a build."""
    return await _create_task_event(
        build_id, task_id, EventType.TASK_COMPLETED, db, auth, commit_hash=commit_hash
    )


@router.post("/{build_id}/tasks/{task_id}/fail", response_model=TaskEventResponse)
async def fail_task(
    build_id: UUID,
    task_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[SdkAuth, Depends(require_sdk_auth)],
    error_message: str | None = None,
    commit_hash: str | None = None,
):
    """Mark a task as failed within a build."""
    return await _create_task_event(
        build_id,
        task_id,
        EventType.TASK_FAILED,
        db,
        auth,
        error_message,
        commit_hash=commit_hash,
    )


@router.post("/{build_id}/tasks/{task_id}/suspend", response_model=TaskEventResponse)
async def suspend_task(
    build_id: UUID,
    task_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[SdkAuth, Depends(require_sdk_auth)],
    commit_hash: str | None = None,
):
    """Mark a task as suspended (waiting for dynamic dependencies)."""
    return await _create_task_event(
        build_id, task_id, EventType.TASK_SUSPENDED, db, auth, commit_hash=commit_hash
    )


@router.post(
    "/{build_id}/tasks/{task_id}/dependencies",
    response_model=AddDependenciesResponse,
)
async def add_task_dependencies(
    build_id: UUID,
    task_id: str,
    request: AddDependenciesRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[SdkAuth, Depends(require_sdk_auth)],
):
    """Register dependency edges for an existing task.

    Used by the SDK to record dynamically-yielded dependencies at runtime —
    deps that weren't known at ``task_register`` time because they come from
    a ``yield`` inside ``run()`` / ``run_aio()``. Static deps declared via
    ``requires()`` are registered in :func:`register_task` and don't use
    this endpoint.

    Creates phantom upstream tasks for unknown ``upstream_task_ids`` and
    inserts edges idempotently (``ON CONFLICT DO NOTHING``). The first write
    of a given edge sets ``is_dynamic``; subsequent writes do not overwrite.

    Returns:
        ``{"added": <new edges>, "total": <upstream_task_ids length>}``.
    """
    _raise_if_limit_exceeded(check_rate_limit(auth.workspace_id, limits_settings))
    _raise_if_limit_exceeded(
        check_structural_limit(
            len(request.upstream_task_ids),
            limits_settings.max_dependency_ids_per_task,
            ErrorCode.DEPENDENCY_COUNT_LIMIT,
            "upstream_task_ids",
        )
    )

    build = await db.get(Build, build_id)
    if not build:
        raise HTTPException(status_code=404, detail="Build not found")
    if build.environment_id != auth.environment_id:
        raise HTTPException(
            status_code=403, detail="Build does not belong to this environment"
        )

    # Locate the downstream task. Scoped by environment, not by build —
    # a task may pre-exist from an earlier build in the same environment
    # and still be a valid target for new dynamic-edge records. Fails with
    # 404 if the task_id is unknown in this environment.
    result = await db.execute(
        select(Task)
        .where(Task.environment_id == build.environment_id)
        .where(Task.task_id == task_id)
    )
    db_task = result.scalar_one_or_none()
    if not db_task:
        raise HTTPException(
            status_code=404,
            detail=f"Task {task_id} not registered in this environment",
        )

    added = await _reconcile_dependency_edges(
        db=db,
        environment_id=build.environment_id,
        downstream_task_pk=db_task.id,
        upstream_task_ids=request.upstream_task_ids,
        is_dynamic=request.is_dynamic,
    )
    await db.commit()

    return AddDependenciesResponse(added=added, total=len(request.upstream_task_ids))


@router.post("/{build_id}/tasks/{task_id}/resume", response_model=TaskEventResponse)
async def resume_task(
    build_id: UUID,
    task_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[SdkAuth, Depends(require_sdk_auth)],
    commit_hash: str | None = None,
):
    """Mark a task as resumed (dynamic dependencies completed)."""
    return await _create_task_event(
        build_id, task_id, EventType.TASK_RESUMED, db, auth, commit_hash=commit_hash
    )


@router.post("/{build_id}/tasks/{task_id}/cancel", response_model=TaskEventResponse)
async def cancel_task(
    build_id: UUID,
    task_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[SdkAuth, Depends(require_sdk_auth)],
    commit_hash: str | None = None,
):
    """Cancel a task within a build."""
    return await _create_task_event(
        build_id, task_id, EventType.TASK_CANCELLED, db, auth, commit_hash=commit_hash
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
    commit_hash: str | None = None,
):
    """Record that a task is waiting for a global lock held by another build."""
    extra_metadata = {"lock_owner": lock_owner} if lock_owner else None
    return await _create_task_event(
        build_id,
        task_id,
        EventType.TASK_WAITING_FOR_LOCK,
        db,
        auth,
        commit_hash=commit_hash,
        extra_metadata=extra_metadata,
    )


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

    # Order tasks by their first-event-in-this-build timestamp so the list
    # reflects per-build registration/discovery order — not Task.created_at,
    # which is the global "first ever seen in this environment" timestamp
    # and would surface previously-cached tasks at the top of every later
    # build. ``Task.id`` (UUID7, time-encoded) breaks the timestamp tie
    # deterministically. ``min(events.id)`` would be a more precise
    # tiebreaker but Postgres has no ``min(uuid)`` aggregate (unlike SQLite),
    # and the practical risk of two tasks having identical
    # ``min(events.created_at)`` is negligible.
    first_event_subquery = (
        select(
            Event.task_id.label("task_id"),
            func.min(Event.created_at).label("first_event_at"),
        )
        .where(Event.build_id == build_id)
        .where(Event.task_id.isnot(None))
        .group_by(Event.task_id)
        .subquery()
    )

    result = await db.execute(
        select(Task)
        .join(first_event_subquery, Task.id == first_event_subquery.c.task_id)
        .order_by(
            first_event_subquery.c.first_event_at.asc(),
            Task.id.asc(),
        )
    )
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
            commit_hash,
        ) = statuses.get(
            task.id, (TaskStatus.PENDING, None, None, None, None, False, None)
        )
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
                is_phantom=task.is_phantom,
                status=status,
                started_at=started_at,
                completed_at=completed_at,
                error_message=error_message,
                artifact_count=artifact_counts.get(task.id, 0),
                waiting_for_lock=waiting_for_lock,
                status_build_id=status_build_id,
                commit_hash=commit_hash,
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


@router.get("/{build_id}/graph", response_model=TaskGraphExtendedResponse)
async def get_build_graph(
    build_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[SdkAuth, Depends(require_sdk_auth)],
    upstream_depth: Annotated[int, Query(ge=0, le=100)] = 0,
    downstream_depth: Annotated[int, Query(ge=0, le=100)] = 0,
    max_per_type_per_level: Annotated[int, Query(ge=1, le=200)] = 5,
    max_total_nodes: Annotated[int, Query(ge=1, le=5000)] = 500,
) -> TaskGraphExtendedResponse:
    """Get the task graph for a build.

    Recursively traverses dependencies starting from tasks in the build.
    ``upstream_depth`` / ``downstream_depth`` control how far to traverse
    *beyond* the build boundary (both default 0 — just the build's own
    tasks are returned). ``max_per_type_per_level`` controls grouping:
    same-type tasks at the same traversal depth & status get collapsed
    into a single batch node when their count exceeds the threshold.
    Grouping applies regardless of traversal depth, including depth 0
    (so a build with many structurally-identical tasks renders tidily).

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

    # Get all tasks by those IDs (IDs only — traverse_upstream re-fetches)
    result = await db.execute(select(Task.id).where(Task.id.in_(task_ids_subquery)))
    task_ids_list = [row[0] for row in result.all()]

    from stardag_api.services.graph import traverse_upstream

    return await traverse_upstream(
        db=db,
        environment_id=auth.environment_id,
        primary_task_pks=task_ids_list,
        upstream_depth=upstream_depth,
        downstream_depth=downstream_depth,
        max_per_type_per_level=max_per_type_per_level,
        max_total_nodes=max_total_nodes,
    )
