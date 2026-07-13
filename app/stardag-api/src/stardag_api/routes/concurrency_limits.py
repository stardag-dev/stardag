"""CRUD + admin endpoints for named environment concurrency limits.

See ``models/concurrency_limit.py`` for the model and semantics; the
enforcement itself happens in the task-start endpoint
(``routes/builds.py::start_task`` with ``enforce_limits=true``).

Besides the limit CRUD, this module exposes the slot-admin surface:
listing a key's current holders (RUNNING tasks counted against it) and
evicting a holder — the recovery path for slots leaked by a crashed
resident build process (reactive builds self-heal via scheduler ticks;
resident builds have no automatic healer).
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.auth import SdkAuth, require_sdk_auth
from stardag_api.config import limits_settings
from stardag_api.db import get_db
from stardag_api.limits import (
    LimitExceededError,
    check_entity_creation_limit,
    check_rate_limit,
    record_entity_created,
)
from stardag_api.models import (
    EnvironmentConcurrencyLimit,
    Event,
    EventType,
    Task,
    TaskLimitKey,
    TaskStatus,
)
from stardag_api.schemas import (
    ConcurrencyLimitHolder,
    ConcurrencyLimitHoldersResponse,
    ConcurrencyLimitList,
    ConcurrencyLimitResponse,
    ConcurrencyLimitUpsert,
    TaskEventResponse,
)
from stardag_api.services.status import apply_event_to_task

router = APIRouter(prefix="/concurrency-limits", tags=["concurrency-limits"])


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


@router.get("", response_model=ConcurrencyLimitList)
async def list_concurrency_limits(
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[SdkAuth, Depends(require_sdk_auth)],
):
    """List the environment's named concurrency limits."""
    rows = (
        (
            await db.execute(
                select(EnvironmentConcurrencyLimit)
                .where(
                    EnvironmentConcurrencyLimit.environment_id == auth.environment_id
                )
                .order_by(EnvironmentConcurrencyLimit.key)
            )
        )
        .scalars()
        .all()
    )
    return ConcurrencyLimitList(
        limits=[
            ConcurrencyLimitResponse(key=row.key, max_concurrent=row.max_concurrent)
            for row in rows
        ]
    )


@router.put("/{key}", response_model=ConcurrencyLimitResponse)
async def upsert_concurrency_limit(
    key: str,
    payload: ConcurrencyLimitUpsert,
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[SdkAuth, Depends(require_sdk_auth)],
):
    """Create or update a named concurrency limit for the environment."""
    if payload.max_concurrent < 1:
        raise HTTPException(status_code=422, detail="max_concurrent must be at least 1")

    async def _select_row() -> EnvironmentConcurrencyLimit | None:
        return (
            await db.execute(
                select(EnvironmentConcurrencyLimit).where(
                    EnvironmentConcurrencyLimit.environment_id == auth.environment_id,
                    EnvironmentConcurrencyLimit.key == key,
                )
            )
        ).scalar_one_or_none()

    row = await _select_row()
    if row is None:
        row = EnvironmentConcurrencyLimit(
            environment_id=auth.environment_id,
            key=key,
            max_concurrent=payload.max_concurrent,
        )
        db.add(row)
        try:
            await db.commit()
        except IntegrityError:
            # Lost a create race against a concurrent PUT for the same
            # (environment, key): the unique constraint fired — retry as an
            # update of the row the winner inserted.
            await db.rollback()
            row = await _select_row()
            if row is None:  # pragma: no cover - winner deleted in between
                raise HTTPException(
                    status_code=409, detail="Concurrent limit modification"
                )
            row.max_concurrent = payload.max_concurrent
            await db.commit()
    else:
        row.max_concurrent = payload.max_concurrent
        await db.commit()
    return ConcurrencyLimitResponse(key=key, max_concurrent=payload.max_concurrent)


@router.delete("/{key}", status_code=204)
async def delete_concurrency_limit(
    key: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[SdkAuth, Depends(require_sdk_auth)],
):
    """Remove a named concurrency limit (the key becomes unlimited)."""
    row = (
        await db.execute(
            select(EnvironmentConcurrencyLimit).where(
                EnvironmentConcurrencyLimit.environment_id == auth.environment_id,
                EnvironmentConcurrencyLimit.key == key,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Concurrency limit not found")
    await db.delete(row)
    await db.commit()


def _holders_filter(key: str, environment_id):
    """WHERE clause fragments selecting a key's current slot holders.

    A holder is a task in the environment that is RUNNING and has the key
    recorded (see ``TaskLimitKey``) — the same definition the enforcement
    count in ``routes/builds.py::_check_concurrency_limits`` uses. Note
    that holders can exist for keys without a configured limit row (the
    key is then unlimited but slots are still tracked).
    """
    return (
        TaskLimitKey.key == key,
        Task.environment_id == environment_id,
        Task.latest_status == TaskStatus.RUNNING,
    )


@router.get("/{key}/holders", response_model=ConcurrencyLimitHoldersResponse)
async def list_concurrency_limit_holders(
    key: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[SdkAuth, Depends(require_sdk_auth)],
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
):
    """List the RUNNING tasks currently holding slots of a limit key.

    Ordered oldest-running first (eviction candidates on top); ``total``
    carries the full holder count when it exceeds ``limit``.
    """
    _raise_if_limit_exceeded(check_rate_limit(auth.workspace_id, limits_settings))

    total = (
        await db.execute(
            select(func.count(func.distinct(TaskLimitKey.task_pk)))
            .select_from(TaskLimitKey)
            .join(Task, TaskLimitKey.task_pk == Task.id)
            .where(*_holders_filter(key, auth.environment_id))
        )
    ).scalar_one()

    holders = (
        (
            await db.execute(
                select(Task)
                .join(TaskLimitKey, TaskLimitKey.task_pk == Task.id)
                .where(*_holders_filter(key, auth.environment_id))
                .order_by(Task.latest_status_at.asc(), Task.id.asc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )

    return ConcurrencyLimitHoldersResponse(
        key=key,
        holders=[
            ConcurrencyLimitHolder(
                task_id=t.task_id,
                task_namespace=t.task_namespace,
                task_name=t.task_name,
                latest_status_at=t.latest_status_at,
                latest_executor=t.latest_executor,
                latest_executor_ref=t.latest_executor_ref,
                latest_executor_metadata=t.latest_executor_metadata,
            )
            for t in holders
        ],
        total=total,
    )


@router.post(
    "/{key}/holders/{task_id}/evict",
    response_model=TaskEventResponse,
)
async def evict_concurrency_limit_holder(
    key: str,
    task_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[SdkAuth, Depends(require_sdk_auth)],
):
    """Evict a slot holder: record TASK_FAILED for a RUNNING holder of ``key``.

    Admin recovery for leaked slots (e.g. a resident build process that
    died while its tasks were RUNNING). The failure flows through the
    normal status transition, freeing ALL the task's slots — deliberately
    scoped to current holders of ``key`` (404 otherwise), NOT a generic
    kill-any-task endpoint.

    Auth: same pattern as other UI-facing mutations (build cancel etc.) —
    API key or JWT + environment_id. The evicting identity is recorded in
    the event metadata and error message.
    """
    _raise_if_limit_exceeded(check_rate_limit(auth.workspace_id, limits_settings))
    _raise_if_limit_exceeded(
        await check_entity_creation_limit(
            db, auth.workspace_id, "events", limits_settings
        )
    )

    # Lock the task row (same reasoning as _create_task_event in
    # routes/builds.py): apply_event_to_task below does a read-modify-write
    # on the denormalised latest_* columns.
    db_task = (
        await db.execute(
            select(Task)
            .join(TaskLimitKey, TaskLimitKey.task_pk == Task.id)
            .where(
                *_holders_filter(key, auth.environment_id),
                Task.task_id == task_id,
            )
            .with_for_update(of=Task)
        )
    ).scalar_one_or_none()
    if db_task is None:
        raise HTTPException(
            status_code=404,
            detail="Task is not a current holder of this concurrency limit key",
        )

    # Attach the failure event to the build the RUNNING status came from.
    # Always set for RUNNING tasks (TASK_STARTED records it); the guard
    # covers hypothetical legacy rows.
    if db_task.latest_status_build_id is None:
        raise HTTPException(
            status_code=409,
            detail="Task has no associated build to record the eviction in",
        )

    if auth.user is not None:
        evicted_by = auth.user.email or auth.user.display_name or "user"
        event_metadata: dict = {"evicted_by_user_id": auth.user.external_id}
    else:
        evicted_by = "API key"
        event_metadata = {}
    event_metadata["concurrency_limit_key"] = key

    event = Event(
        build_id=db_task.latest_status_build_id,
        task_id=db_task.id,
        event_type=EventType.TASK_FAILED,
        error_message=(f"Evicted by {evicted_by} from concurrency limit key {key!r}"),
        event_metadata=event_metadata,
    )
    db.add(event)
    await db.flush()
    apply_event_to_task(db_task, event)
    await db.commit()

    record_entity_created(auth.workspace_id, "events")

    return TaskEventResponse(task_id=db_task.task_id, status=db_task.latest_status)
