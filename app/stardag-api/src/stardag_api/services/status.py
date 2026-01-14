"""Status derivation from events."""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.models import BuildStatus, Event, EventType, TaskStatus


async def get_build_status(
    db: AsyncSession, build_id: str
) -> tuple[BuildStatus, datetime | None, datetime | None]:
    """Get derived build status from events.

    Returns:
        Tuple of (status, started_at, completed_at)
    """
    # Get build-level events (task_id is NULL)
    result = await db.execute(
        select(Event)
        .where(Event.build_id == build_id)
        .where(Event.task_id.is_(None))
        .order_by(Event.created_at.desc())
    )
    events = result.scalars().all()

    status = BuildStatus.PENDING
    started_at: datetime | None = None
    completed_at: datetime | None = None

    # Process events from oldest to newest to build final state
    for event in reversed(events):
        if event.event_type == EventType.BUILD_STARTED:
            status = BuildStatus.RUNNING
            started_at = event.created_at
        elif event.event_type == EventType.BUILD_COMPLETED:
            status = BuildStatus.COMPLETED
            completed_at = event.created_at
        elif event.event_type == EventType.BUILD_FAILED:
            status = BuildStatus.FAILED
            completed_at = event.created_at
        elif event.event_type == EventType.BUILD_CANCELLED:
            status = BuildStatus.CANCELLED
            completed_at = event.created_at
        elif event.event_type == EventType.BUILD_EXIT_EARLY:
            status = BuildStatus.EXIT_EARLY
            completed_at = event.created_at

    return status, started_at, completed_at


async def get_task_status_in_build(
    db: AsyncSession, build_id: str, task_db_id: int
) -> tuple[TaskStatus, datetime | None, datetime | None, str | None]:
    """Get derived task status from events for a specific build.

    Returns:
        Tuple of (status, started_at, completed_at, error_message)
    """
    result = await db.execute(
        select(Event)
        .where(Event.build_id == build_id)
        .where(Event.task_id == task_db_id)
        .order_by(Event.created_at.desc())
    )
    events = result.scalars().all()

    status = TaskStatus.PENDING
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error_message: str | None = None

    # Process events from oldest to newest to build final state
    for event in reversed(events):
        if event.event_type == EventType.TASK_PENDING:
            status = TaskStatus.PENDING
        elif event.event_type == EventType.TASK_REFERENCED:
            # Informational: task already existed, stays PENDING
            pass
        elif event.event_type == EventType.TASK_STARTED:
            status = TaskStatus.RUNNING
            started_at = event.created_at
        elif event.event_type == EventType.TASK_SUSPENDED:
            status = TaskStatus.SUSPENDED
        elif event.event_type == EventType.TASK_RESUMED:
            status = TaskStatus.RUNNING
        elif event.event_type == EventType.TASK_WAITING_FOR_LOCK:
            # Informational: blocked by global lock, stays PENDING
            pass
        elif event.event_type == EventType.TASK_COMPLETED:
            status = TaskStatus.COMPLETED
            completed_at = event.created_at
        elif event.event_type == EventType.TASK_FAILED:
            status = TaskStatus.FAILED
            completed_at = event.created_at
            error_message = event.error_message
        elif event.event_type == EventType.TASK_SKIPPED:
            status = TaskStatus.SKIPPED
            completed_at = event.created_at
        elif event.event_type == EventType.TASK_CANCELLED:
            status = TaskStatus.CANCELLED
            completed_at = event.created_at

    return status, started_at, completed_at, error_message


async def get_all_task_statuses_in_build(
    db: AsyncSession, build_id: str
) -> dict[int, tuple[TaskStatus, datetime | None, datetime | None, str | None]]:
    """Get derived status for all tasks in a build.

    Returns:
        Dict mapping task_db_id to (status, started_at, completed_at, error_message)
    """
    result = await db.execute(
        select(Event)
        .where(Event.build_id == build_id)
        .where(Event.task_id.isnot(None))
        .order_by(Event.created_at.asc())
    )
    events = result.scalars().all()

    # Build status for each task
    statuses: dict[
        int, tuple[TaskStatus, datetime | None, datetime | None, str | None]
    ] = {}

    for event in events:
        if event.task_id is None:
            continue

        task_id = event.task_id
        current = statuses.get(task_id, (TaskStatus.PENDING, None, None, None))
        status, started_at, completed_at, error_message = current

        if event.event_type == EventType.TASK_PENDING:
            status = TaskStatus.PENDING
        elif event.event_type == EventType.TASK_REFERENCED:
            # Informational: task already existed, stays PENDING
            pass
        elif event.event_type == EventType.TASK_STARTED:
            status = TaskStatus.RUNNING
            started_at = event.created_at
        elif event.event_type == EventType.TASK_SUSPENDED:
            status = TaskStatus.SUSPENDED
        elif event.event_type == EventType.TASK_RESUMED:
            status = TaskStatus.RUNNING
        elif event.event_type == EventType.TASK_WAITING_FOR_LOCK:
            # Informational: blocked by global lock, stays PENDING
            pass
        elif event.event_type == EventType.TASK_COMPLETED:
            status = TaskStatus.COMPLETED
            completed_at = event.created_at
        elif event.event_type == EventType.TASK_FAILED:
            status = TaskStatus.FAILED
            completed_at = event.created_at
            error_message = event.error_message
        elif event.event_type == EventType.TASK_SKIPPED:
            status = TaskStatus.SKIPPED
            completed_at = event.created_at
        elif event.event_type == EventType.TASK_CANCELLED:
            status = TaskStatus.CANCELLED
            completed_at = event.created_at

        statuses[task_id] = (status, started_at, completed_at, error_message)

    return statuses


async def get_task_global_status(
    db: AsyncSession, task_db_id: int
) -> tuple[TaskStatus, datetime | None, datetime | None, str | None, str | None]:
    """Get task status considering events from ALL builds.

    This provides a "global" view of task status across all builds in the workspace.
    Useful for understanding the true state when multiple builds share tasks.

    Priority order:
    1. TASK_COMPLETED in any build -> completed (with build_id)
    2. TASK_STARTED/RUNNING in any build -> running
    3. TASK_PENDING -> pending

    Returns:
        Tuple of (status, started_at, completed_at, error_message, completed_in_build_id)
    """
    # Get all events for this task across all builds
    result = await db.execute(
        select(Event)
        .where(Event.task_id == task_db_id)
        .order_by(Event.created_at.asc())
    )
    events = result.scalars().all()

    # Track the "best" status we've seen
    # Priority: COMPLETED > RUNNING > PENDING
    best_status = TaskStatus.PENDING
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error_message: str | None = None
    completed_in_build_id: str | None = None

    for event in events:
        if event.event_type == EventType.TASK_COMPLETED:
            # Completed takes precedence over everything
            best_status = TaskStatus.COMPLETED
            completed_at = event.created_at
            completed_in_build_id = event.build_id
        elif event.event_type == EventType.TASK_STARTED:
            # Running takes precedence over pending (but not completed)
            if best_status != TaskStatus.COMPLETED:
                best_status = TaskStatus.RUNNING
                if started_at is None:
                    started_at = event.created_at
        elif event.event_type == EventType.TASK_RESUMED:
            # Resumed also means running
            if best_status != TaskStatus.COMPLETED:
                best_status = TaskStatus.RUNNING
        elif event.event_type == EventType.TASK_FAILED:
            # Only set failed if not completed elsewhere
            if best_status != TaskStatus.COMPLETED:
                best_status = TaskStatus.FAILED
                completed_at = event.created_at
                error_message = event.error_message

    return best_status, started_at, completed_at, error_message, completed_in_build_id
