"""Distributed lock service for global task concurrency control.

Implements lease-based distributed locks using PostgreSQL.
Locks are scoped to environment and identified by lock name (typically task_id).
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.models import DistributedLock, Environment, Event, EventType, Task


class LockAcquisitionStatus(StrEnum):
    """Status of a lock acquisition attempt."""

    ACQUIRED = "acquired"
    ALREADY_COMPLETED = "already_completed"
    HELD_BY_OTHER = "held_by_other"
    CONCURRENCY_LIMIT_REACHED = "concurrency_limit_reached"


@dataclass
class LockAcquisitionResult:
    """Result of a lock acquisition attempt."""

    status: LockAcquisitionStatus
    acquired: bool
    lock: DistributedLock | None = None
    error_message: str | None = None


def _utc_now() -> datetime:
    """Get current UTC timestamp."""
    return datetime.now(timezone.utc)


async def check_task_completed_in_registry(
    db: AsyncSession,
    environment_id: str,
    task_id: str,
) -> bool:
    """Check if a task has a TASK_COMPLETED event in the registry.

    Searches across all builds in the environment for any completion event.

    Args:
        db: Database session
        environment_id: The environment to search in
        task_id: The task_id (hash) to check

    Returns:
        True if the task has at least one TASK_COMPLETED event
    """
    # First find the task by task_id in the environment
    task_result = await db.execute(
        select(Task.id)
        .where(Task.environment_id == environment_id)
        .where(Task.task_id == task_id)
    )
    task_db_id = task_result.scalar_one_or_none()

    if task_db_id is None:
        # Task not registered in environment, so not completed
        return False

    # Check for any TASK_COMPLETED event for this task
    event_result = await db.execute(
        select(func.count())
        .select_from(Event)
        .where(Event.task_id == task_db_id)
        .where(Event.event_type == EventType.TASK_COMPLETED)
    )
    count = event_result.scalar() or 0
    return count > 0


async def get_environment_lock_count(
    db: AsyncSession,
    environment_id: str,
) -> int:
    """Get the count of active (non-expired) locks for an environment.

    Args:
        db: Database session
        environment_id: The environment to count locks for

    Returns:
        Number of active locks
    """
    now = _utc_now()
    result = await db.execute(
        select(func.count())
        .select_from(DistributedLock)
        .where(DistributedLock.environment_id == environment_id)
        .where(DistributedLock.expires_at > now)
    )
    return result.scalar() or 0


async def get_environment_max_concurrent_locks(
    db: AsyncSession,
    environment_id: str,
) -> int | None:
    """Get the max concurrent locks limit for an environment.

    Args:
        db: Database session
        environment_id: The environment to check

    Returns:
        Max concurrent locks limit, or None if unlimited
    """
    result = await db.execute(
        select(Environment.max_concurrent_locks).where(Environment.id == environment_id)
    )
    return result.scalar_one_or_none()


async def acquire_lock(
    db: AsyncSession,
    lock_name: str,
    owner_id: str,
    environment_id: str,
    ttl_seconds: int,
    check_task_completion: bool = True,
) -> LockAcquisitionResult:
    """Attempt to acquire a distributed lock.

    Uses atomic INSERT or UPDATE to handle concurrent acquisition attempts.
    The lock can be acquired if:
    - No lock exists with this name
    - The existing lock has expired (expires_at <= now)
    - The existing lock is owned by the same owner (re-entrant)

    Args:
        db: Database session
        lock_name: Unique lock identifier (typically task_id)
        owner_id: UUID identifying the lock owner (stable across retries)
        environment_id: The environment scope for the lock
        ttl_seconds: Time-to-live in seconds
        check_task_completion: Whether to check if task is already completed

    Returns:
        LockAcquisitionResult with status and lock details
    """
    now = _utc_now()
    expires_at = now + timedelta(seconds=ttl_seconds)

    # First, check if task is already completed in registry
    if check_task_completion:
        is_completed = await check_task_completed_in_registry(
            db, environment_id, lock_name
        )
        if is_completed:
            return LockAcquisitionResult(
                status=LockAcquisitionStatus.ALREADY_COMPLETED,
                acquired=False,
            )

    # Check environment concurrency limit
    max_locks = await get_environment_max_concurrent_locks(db, environment_id)
    if max_locks is not None:
        current_count = await get_environment_lock_count(db, environment_id)
        # Only check limit if we don't already hold a lock for this name
        existing_lock_result = await db.execute(
            select(DistributedLock).where(DistributedLock.name == lock_name)
        )
        existing_lock = existing_lock_result.scalar_one_or_none()

        # If we don't have an existing lock (or it's not ours), check the limit
        if existing_lock is None or existing_lock.owner_id != owner_id:
            if current_count >= max_locks:
                return LockAcquisitionResult(
                    status=LockAcquisitionStatus.CONCURRENCY_LIMIT_REACHED,
                    acquired=False,
                    error_message=f"Environment concurrency limit reached ({max_locks})",
                )

    # Attempt atomic upsert
    # PostgreSQL INSERT ... ON CONFLICT with conditional UPDATE
    stmt = pg_insert(DistributedLock).values(
        name=lock_name,
        environment_id=environment_id,
        owner_id=owner_id,
        acquired_at=now,
        expires_at=expires_at,
        version=0,
    )

    # On conflict (lock exists), update only if:
    # - Lock has expired (expires_at <= now), OR
    # - Same owner (re-entrant)
    stmt = stmt.on_conflict_do_update(
        index_elements=["name"],
        set_={
            "owner_id": owner_id,
            "acquired_at": now,
            "expires_at": expires_at,
            "version": DistributedLock.version + 1,
        },
        where=(
            (DistributedLock.expires_at <= now) | (DistributedLock.owner_id == owner_id)
        ),
    ).returning(DistributedLock)

    result = await db.execute(stmt)
    lock = result.scalar_one_or_none()

    if lock is not None and lock.owner_id == owner_id:
        # Successfully acquired or re-acquired
        return LockAcquisitionResult(
            status=LockAcquisitionStatus.ACQUIRED,
            acquired=True,
            lock=lock,
        )

    # Lock is held by another owner
    return LockAcquisitionResult(
        status=LockAcquisitionStatus.HELD_BY_OTHER,
        acquired=False,
        error_message="Lock is held by another owner",
    )


async def renew_lock(
    db: AsyncSession,
    lock_name: str,
    owner_id: str,
    ttl_seconds: int,
) -> bool:
    """Renew an existing lock's TTL.

    Only succeeds if the lock exists and is owned by the caller.

    Args:
        db: Database session
        lock_name: The lock to renew
        owner_id: The expected owner
        ttl_seconds: New TTL in seconds

    Returns:
        True if successfully renewed, False otherwise
    """
    now = _utc_now()
    expires_at = now + timedelta(seconds=ttl_seconds)

    result = await db.execute(
        update(DistributedLock)
        .where(DistributedLock.name == lock_name)
        .where(DistributedLock.owner_id == owner_id)
        .values(expires_at=expires_at, version=DistributedLock.version + 1)
        .returning(DistributedLock.name)
    )

    renewed = result.scalar_one_or_none()
    return renewed is not None


async def release_lock(
    db: AsyncSession,
    lock_name: str,
    owner_id: str,
) -> bool:
    """Release a lock.

    Only succeeds if the lock exists and is owned by the caller.

    Args:
        db: Database session
        lock_name: The lock to release
        owner_id: The expected owner

    Returns:
        True if successfully released, False otherwise
    """
    result = await db.execute(
        delete(DistributedLock)
        .where(DistributedLock.name == lock_name)
        .where(DistributedLock.owner_id == owner_id)
        .returning(DistributedLock.name)
    )

    deleted = result.scalar_one_or_none()
    return deleted is not None


async def release_lock_with_completion(
    db: AsyncSession,
    lock_name: str,
    owner_id: str,
    environment_id: str,
    build_id: str,
) -> bool:
    """Release a lock and record task completion in the same transaction.

    This ensures the completion event is recorded before the lock is released,
    preventing race conditions where another instance might see an incomplete
    task after the lock is released.

    Args:
        db: Database session
        lock_name: The lock to release (should be task_id)
        owner_id: The expected owner
        environment_id: The environment scope
        build_id: The build to record completion for

    Returns:
        True if successfully released, False otherwise
    """
    # Find the task by task_id
    task_result = await db.execute(
        select(Task.id)
        .where(Task.environment_id == environment_id)
        .where(Task.task_id == lock_name)
    )
    task_db_id = task_result.scalar_one_or_none()

    if task_db_id is not None:
        # Record completion event
        completion_event = Event(
            build_id=build_id,
            task_id=task_db_id,
            event_type=EventType.TASK_COMPLETED,
        )
        db.add(completion_event)

    # Release the lock
    result = await db.execute(
        delete(DistributedLock)
        .where(DistributedLock.name == lock_name)
        .where(DistributedLock.owner_id == owner_id)
        .returning(DistributedLock.name)
    )

    deleted = result.scalar_one_or_none()
    return deleted is not None


async def get_lock(
    db: AsyncSession,
    lock_name: str,
) -> DistributedLock | None:
    """Get a lock by name.

    Args:
        db: Database session
        lock_name: The lock name to look up

    Returns:
        The lock if it exists, None otherwise
    """
    result = await db.execute(
        select(DistributedLock).where(DistributedLock.name == lock_name)
    )
    return result.scalar_one_or_none()


async def list_locks(
    db: AsyncSession,
    environment_id: str,
    include_expired: bool = False,
) -> list[DistributedLock]:
    """List locks for an environment.

    Args:
        db: Database session
        environment_id: The environment to list locks for
        include_expired: Whether to include expired locks

    Returns:
        List of locks
    """
    query = select(DistributedLock).where(
        DistributedLock.environment_id == environment_id
    )

    if not include_expired:
        now = _utc_now()
        query = query.where(DistributedLock.expires_at > now)

    query = query.order_by(DistributedLock.acquired_at.desc())

    result = await db.execute(query)
    return list(result.scalars().all())


async def cleanup_expired_locks(
    db: AsyncSession,
    environment_id: str | None = None,
) -> int:
    """Clean up expired locks.

    This is optional - locks auto-expire and can be taken over.
    But periodic cleanup keeps the table tidy.

    Args:
        db: Database session
        environment_id: Optional environment to limit cleanup to

    Returns:
        Number of locks deleted
    """
    now = _utc_now()
    stmt = delete(DistributedLock).where(DistributedLock.expires_at <= now)

    if environment_id is not None:
        stmt = stmt.where(DistributedLock.environment_id == environment_id)

    stmt = stmt.returning(DistributedLock.name)

    result = await db.execute(stmt)
    deleted = result.scalars().all()
    return len(deleted)
