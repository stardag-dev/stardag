"""Distributed lock routes for global concurrency control."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.auth import SdkAuth, require_sdk_auth
from stardag_api.db import get_db
from stardag_api.schemas import (
    LockAcquireRequest,
    LockAcquireResponse,
    LockListResponse,
    LockReleaseRequest,
    LockReleaseResponse,
    LockRenewRequest,
    LockRenewResponse,
    LockResponse,
    TaskCompletionStatusResponse,
)
from stardag_api.services import (
    LockAcquisitionStatus,
    acquire_lock,
    check_task_completed_in_registry,
    get_lock,
    list_locks,
    release_lock,
    release_lock_with_completion,
    renew_lock,
)

router = APIRouter(prefix="/locks", tags=["locks"])


@router.post("/{lock_name}/acquire", response_model=LockAcquireResponse)
async def acquire_lock_endpoint(
    lock_name: str,
    request: LockAcquireRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[SdkAuth, Depends(require_sdk_auth)],
):
    """Acquire a distributed lock for a task.

    The lock is scoped to the authenticated workspace and identified by lock_name
    (typically the task_id hash).

    The lock can be acquired if:
    - No lock exists with this name
    - The existing lock has expired
    - The existing lock is owned by the same owner (re-entrant)

    If check_task_completion is True and the task has a TASK_COMPLETED event in the
    registry, returns status="already_completed" instead of acquiring the lock.

    Args:
        lock_name: Unique lock identifier (typically task_id)
        request: Lock acquisition parameters

    Returns:
        LockAcquireResponse with status and lock details

    HTTP Status Codes:
        200: Lock acquired or already_completed
        423: Lock held by another owner
        429: Workspace concurrency limit reached
    """
    result = await acquire_lock(
        db=db,
        lock_name=lock_name,
        owner_id=request.owner_id,
        workspace_id=auth.workspace_id,
        ttl_seconds=request.ttl_seconds,
        check_task_completion=request.check_task_completion,
    )

    await db.commit()

    # Map status to HTTP response
    if result.status == LockAcquisitionStatus.HELD_BY_OTHER:
        raise HTTPException(
            status_code=423,
            detail={
                "status": result.status.value,
                "error_message": result.error_message,
            },
        )

    if result.status == LockAcquisitionStatus.CONCURRENCY_LIMIT_REACHED:
        raise HTTPException(
            status_code=429,
            detail={
                "status": result.status.value,
                "error_message": result.error_message,
            },
        )

    lock_response = None
    if result.lock:
        lock_response = LockResponse(
            name=result.lock.name,
            workspace_id=result.lock.workspace_id,
            owner_id=result.lock.owner_id,
            acquired_at=result.lock.acquired_at,
            expires_at=result.lock.expires_at,
            version=result.lock.version,
        )

    return LockAcquireResponse(
        status=result.status.value,
        acquired=result.acquired,
        lock=lock_response,
        error_message=result.error_message,
    )


@router.post("/{lock_name}/renew", response_model=LockRenewResponse)
async def renew_lock_endpoint(
    lock_name: str,
    request: LockRenewRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[SdkAuth, Depends(require_sdk_auth)],
):
    """Renew an existing lock's TTL.

    Only succeeds if the lock exists and is owned by the caller.

    Args:
        lock_name: The lock to renew
        request: Renewal parameters

    Returns:
        LockRenewResponse indicating success

    HTTP Status Codes:
        200: Lock renewed successfully or renewal failed
        409: Lock not owned by caller or does not exist
    """
    # First verify the lock exists and belongs to this workspace
    existing = await get_lock(db, lock_name)
    if existing and existing.workspace_id != auth.workspace_id:
        raise HTTPException(
            status_code=403,
            detail="Lock does not belong to this workspace",
        )

    renewed = await renew_lock(
        db=db,
        lock_name=lock_name,
        owner_id=request.owner_id,
        ttl_seconds=request.ttl_seconds,
    )

    await db.commit()

    if not renewed:
        raise HTTPException(
            status_code=409,
            detail="Lock not found or not owned by caller",
        )

    return LockRenewResponse(renewed=renewed)


@router.post("/{lock_name}/release", response_model=LockReleaseResponse)
async def release_lock_endpoint(
    lock_name: str,
    request: LockReleaseRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[SdkAuth, Depends(require_sdk_auth)],
):
    """Release a distributed lock.

    Only succeeds if the lock exists and is owned by the caller.

    If task_completed=True and build_id is provided, records a TASK_COMPLETED event
    in the same transaction before releasing the lock.

    Args:
        lock_name: The lock to release
        request: Release parameters

    Returns:
        LockReleaseResponse indicating success

    HTTP Status Codes:
        200: Lock released successfully
        409: Lock not owned by caller or does not exist
    """
    # First verify the lock exists and belongs to this workspace
    existing = await get_lock(db, lock_name)
    if existing and existing.workspace_id != auth.workspace_id:
        raise HTTPException(
            status_code=403,
            detail="Lock does not belong to this workspace",
        )

    if request.task_completed and request.build_id:
        # Release with completion recording
        released = await release_lock_with_completion(
            db=db,
            lock_name=lock_name,
            owner_id=request.owner_id,
            workspace_id=auth.workspace_id,
            build_id=request.build_id,
        )
    else:
        # Simple release
        released = await release_lock(
            db=db,
            lock_name=lock_name,
            owner_id=request.owner_id,
        )

    await db.commit()

    if not released:
        raise HTTPException(
            status_code=409,
            detail="Lock not found or not owned by caller",
        )

    return LockReleaseResponse(released=released)


@router.get("", response_model=LockListResponse)
async def list_locks_endpoint(
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[SdkAuth, Depends(require_sdk_auth)],
    include_expired: Annotated[bool, Query()] = False,
):
    """List active locks in the authenticated workspace.

    Useful for debugging and monitoring lock state.

    Args:
        include_expired: Whether to include expired locks

    Returns:
        LockListResponse with all locks in the workspace
    """
    locks = await list_locks(
        db=db,
        workspace_id=auth.workspace_id,
        include_expired=include_expired,
    )

    lock_responses = [
        LockResponse(
            name=lock.name,
            workspace_id=lock.workspace_id,
            owner_id=lock.owner_id,
            acquired_at=lock.acquired_at,
            expires_at=lock.expires_at,
            version=lock.version,
        )
        for lock in locks
    ]

    return LockListResponse(locks=lock_responses, count=len(lock_responses))


@router.get("/{lock_name}", response_model=LockResponse)
async def get_lock_endpoint(
    lock_name: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[SdkAuth, Depends(require_sdk_auth)],
):
    """Get details of a specific lock.

    Args:
        lock_name: The lock to retrieve

    Returns:
        LockResponse with lock details

    HTTP Status Codes:
        200: Lock found
        404: Lock not found
    """
    lock = await get_lock(db, lock_name)

    if not lock:
        raise HTTPException(status_code=404, detail="Lock not found")

    if lock.workspace_id != auth.workspace_id:
        raise HTTPException(
            status_code=403,
            detail="Lock does not belong to this workspace",
        )

    return LockResponse(
        name=lock.name,
        workspace_id=lock.workspace_id,
        owner_id=lock.owner_id,
        acquired_at=lock.acquired_at,
        expires_at=lock.expires_at,
        version=lock.version,
    )


# Task completion status endpoint (separate from locks but related)
@router.get(
    "/tasks/{task_id}/completion-status", response_model=TaskCompletionStatusResponse
)
async def check_task_completion_endpoint(
    task_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[SdkAuth, Depends(require_sdk_auth)],
):
    """Check if a task has been completed in the registry.

    Searches across all builds in the workspace for any TASK_COMPLETED event.

    Args:
        task_id: The task_id (hash) to check

    Returns:
        TaskCompletionStatusResponse indicating completion status
    """
    is_completed = await check_task_completed_in_registry(
        db=db,
        workspace_id=auth.workspace_id,
        task_id=task_id,
    )

    return TaskCompletionStatusResponse(task_id=task_id, is_completed=is_completed)
