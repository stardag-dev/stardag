"""Status derivation from events."""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.models import BuildStatus, Event, EventType, TaskStatus


async def get_build_status(
    db: AsyncSession, build_id: str
) -> tuple[BuildStatus, datetime | None, datetime | None, str | None]:
    """Get derived build status from events.

    Returns:
        Tuple of (status, started_at, completed_at, status_triggered_by_user_id)
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
    status_triggered_by_user_id: str | None = None

    # Process events from oldest to newest to build final state
    for event in reversed(events):
        if event.event_type == EventType.BUILD_STARTED:
            status = BuildStatus.RUNNING
            started_at = event.created_at
            status_triggered_by_user_id = None  # Not user-triggered
        elif event.event_type == EventType.BUILD_COMPLETED:
            status = BuildStatus.COMPLETED
            completed_at = event.created_at
            # Check if this was user-triggered (manual override)
            status_triggered_by_user_id = (
                event.event_metadata.get("triggered_by_user_id")
                if event.event_metadata
                else None
            )
        elif event.event_type == EventType.BUILD_FAILED:
            status = BuildStatus.FAILED
            completed_at = event.created_at
            status_triggered_by_user_id = (
                event.event_metadata.get("triggered_by_user_id")
                if event.event_metadata
                else None
            )
        elif event.event_type == EventType.BUILD_CANCELLED:
            status = BuildStatus.CANCELLED
            completed_at = event.created_at
            status_triggered_by_user_id = (
                event.event_metadata.get("triggered_by_user_id")
                if event.event_metadata
                else None
            )
        elif event.event_type == EventType.BUILD_EXIT_EARLY:
            status = BuildStatus.EXIT_EARLY
            completed_at = event.created_at
            status_triggered_by_user_id = None  # Not user-triggered

    return status, started_at, completed_at, status_triggered_by_user_id


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


async def get_all_task_global_statuses(
    db: AsyncSession, task_db_ids: list[int]
) -> dict[
    int,
    tuple[TaskStatus, datetime | None, datetime | None, str | None, str | None, bool],
]:
    """Get global status for multiple tasks considering events from ALL builds.

    This is a batched version of get_task_global_status for efficiency.

    Returns:
        Dict mapping task_db_id to:
        (status, started_at, completed_at, error_message, status_build_id, waiting_for_lock)

        status_build_id is the build where the status-determining event occurred.
        This allows the UI to show when a task's status came from a different build.
    """
    if not task_db_ids:
        return {}

    # Get all events for these tasks across all builds
    result = await db.execute(
        select(Event)
        .where(Event.task_id.in_(task_db_ids))
        .order_by(Event.created_at.asc())
    )
    events = result.scalars().all()

    # Initialize statuses for all requested tasks
    statuses: dict[
        int,
        tuple[
            TaskStatus, datetime | None, datetime | None, str | None, str | None, bool
        ],
    ] = {
        task_id: (TaskStatus.PENDING, None, None, None, None, False)
        for task_id in task_db_ids
    }

    # Track per-task state as we process events
    task_states: dict[int, dict] = {
        task_id: {
            "status": TaskStatus.PENDING,
            "started_at": None,
            "completed_at": None,
            "error_message": None,
            "status_build_id": None,
            "waiting_for_lock": False,
        }
        for task_id in task_db_ids
    }

    for event in events:
        if event.task_id is None or event.task_id not in task_states:
            continue

        state = task_states[event.task_id]

        if event.event_type == EventType.TASK_COMPLETED:
            # Completed takes precedence over everything
            state["status"] = TaskStatus.COMPLETED
            state["completed_at"] = event.created_at
            state["status_build_id"] = event.build_id
            state["waiting_for_lock"] = False  # No longer waiting
        elif event.event_type == EventType.TASK_STARTED:
            # Running takes precedence over pending (but not completed)
            if state["status"] != TaskStatus.COMPLETED:
                state["status"] = TaskStatus.RUNNING
                state["status_build_id"] = event.build_id
                if state["started_at"] is None:
                    state["started_at"] = event.created_at
                state["waiting_for_lock"] = False
        elif event.event_type == EventType.TASK_RESUMED:
            if state["status"] != TaskStatus.COMPLETED:
                state["status"] = TaskStatus.RUNNING
                state["status_build_id"] = event.build_id
                state["waiting_for_lock"] = False
        elif event.event_type == EventType.TASK_FAILED:
            # Only set failed if not completed elsewhere
            if state["status"] != TaskStatus.COMPLETED:
                state["status"] = TaskStatus.FAILED
                state["status_build_id"] = event.build_id
                state["completed_at"] = event.created_at
                state["error_message"] = event.error_message
        elif event.event_type == EventType.TASK_CANCELLED:
            if state["status"] != TaskStatus.COMPLETED:
                state["status"] = TaskStatus.CANCELLED
                state["status_build_id"] = event.build_id
                state["completed_at"] = event.created_at
        elif event.event_type == EventType.TASK_WAITING_FOR_LOCK:
            # Only mark as waiting if not yet completed/running
            if state["status"] == TaskStatus.PENDING:
                state["waiting_for_lock"] = True

    # Convert to return format
    for task_id, state in task_states.items():
        statuses[task_id] = (
            state["status"],
            state["started_at"],
            state["completed_at"],
            state["error_message"],
            state["status_build_id"],
            state["waiting_for_lock"],
        )

    return statuses
