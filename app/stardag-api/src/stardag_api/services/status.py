"""Status derivation from events."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.models import BuildStatus, Event, EventType, Task, TaskStatus

# Statuses TASK_RETRIED resets to PENDING. Shared by the denormalised path
# (apply_event_to_task) and the two per-build event replays below, which
# must agree — they answer the same question for different readers.
#
# SUSPENDED is in the set because it is a dead end otherwise: a task
# suspended for dynamic dependencies whose orchestrator then died stays
# suspended forever, and no supported operation makes it schedulable again
# (the only escape was to cancel it first, purely to reach a status that
# *was* retryable). Nothing is running at that point — the suspension
# itself means the execution yielded and returned — so resetting it cannot
# orphan an execution.
#
# RUNNING is deliberately NOT retryable: it holds a live execution claim,
# and releasing that is cancellation, not retry. Flipping it to PENDING
# would let a scheduler spawn a second execution of a task that is still
# running. COMPLETED is excluded by stickiness.
_RETRYABLE_STATUSES = (
    TaskStatus.FAILED,
    TaskStatus.CANCELLED,
    TaskStatus.SKIPPED,
    TaskStatus.SUSPENDED,
)


def apply_event_to_task(task: Task, event: Event) -> None:
    """Mutate a Task's denormalised latest_* columns to reflect a new event.

    This implements the same priority logic as the historical
    get_all_task_global_statuses event scan, applied incrementally:

      - TASK_COMPLETED is sticky — once completed, no other event downgrades.
      - Otherwise TASK_RUNNING / TASK_FAILED / TASK_CANCELLED overwrite
        PENDING (and each other, in event-arrival order).
      - TASK_RESUMED is treated like TASK_STARTED (re-asserts RUNNING).
      - TASK_WAITING_FOR_LOCK only sets the flag if the task is still PENDING.
      - TASK_REFERENCED and TASK_PENDING are status-neutral on existing rows
        (they don't downgrade COMPLETED → PENDING).

    The caller is responsible for persisting the Task — this function only
    mutates the in-memory ORM instance.
    """
    event_commit = (
        event.event_metadata.get("commit_hash") if event.event_metadata else None
    )
    et = event.event_type

    # Generic bookkeeping that always reflects the most-recently-applied event.
    # latest_status_event_id moves only when status itself moves; see below.

    if et == EventType.TASK_COMPLETED:
        task.latest_status = TaskStatus.COMPLETED
        task.latest_status_at = event.created_at
        task.latest_status_event_id = event.id
        task.latest_status_build_id = event.build_id
        task.latest_completed_at = event.created_at
        task.latest_waiting_for_lock = False
        if event_commit is not None:
            task.latest_commit_hash = event_commit
        return

    # All branches below are no-ops once the task is COMPLETED.
    if task.latest_status == TaskStatus.COMPLETED:
        return

    if et == EventType.TASK_STARTED:
        task.latest_status = TaskStatus.RUNNING
        task.latest_status_at = event.created_at
        task.latest_status_event_id = event.id
        task.latest_status_build_id = event.build_id
        if task.latest_started_at is None:
            task.latest_started_at = event.created_at
        task.latest_waiting_for_lock = False
        if event_commit is not None:
            task.latest_commit_hash = event_commit
        # Executor ref of this start (detached execution re-attach). Set or
        # *cleared* on every start so a non-detached run can't leave a stale
        # ref from an earlier detached one behind. The descriptive
        # executor_metadata follows the exact same set/clear semantics.
        metadata = event.event_metadata or {}
        task.latest_executor = metadata.get("executor")
        task.latest_executor_ref = metadata.get("executor_ref")
        task.latest_executor_metadata = metadata.get("executor_metadata")
    elif et == EventType.TASK_RETRIED:
        # Reset a retryable status to PENDING (see _RETRYABLE_STATUSES);
        # sticky-COMPLETED is already handled by the early return, and
        # RUNNING is never downgraded by a retry.
        if task.latest_status in _RETRYABLE_STATUSES:
            task.latest_status = TaskStatus.PENDING
            task.latest_status_at = event.created_at
            task.latest_status_event_id = event.id
            task.latest_status_build_id = event.build_id
            task.latest_completed_at = None
            task.latest_error_message = None
            # The executor fields describe the run that reached the
            # retryable status — including a suspended one, which keeps the
            # ref of the execution that yielded. A retry re-runs from
            # scratch, so clearing them is what stops a scheduler from
            # re-attaching to an execution that will never resume.
            task.latest_executor = None
            task.latest_executor_ref = None
            task.latest_executor_metadata = None
    elif et == EventType.TASK_RESUMED:
        task.latest_status = TaskStatus.RUNNING
        task.latest_status_at = event.created_at
        task.latest_status_event_id = event.id
        task.latest_status_build_id = event.build_id
        task.latest_waiting_for_lock = False
        if event_commit is not None:
            task.latest_commit_hash = event_commit
    elif et == EventType.TASK_FAILED:
        task.latest_status = TaskStatus.FAILED
        task.latest_status_at = event.created_at
        task.latest_status_event_id = event.id
        task.latest_status_build_id = event.build_id
        task.latest_completed_at = event.created_at
        task.latest_error_message = event.error_message
        if event_commit is not None:
            task.latest_commit_hash = event_commit
    elif et == EventType.TASK_CANCELLED:
        task.latest_status = TaskStatus.CANCELLED
        task.latest_status_at = event.created_at
        task.latest_status_event_id = event.id
        task.latest_status_build_id = event.build_id
        task.latest_completed_at = event.created_at
        if event_commit is not None:
            task.latest_commit_hash = event_commit
    elif et == EventType.TASK_SKIPPED:
        task.latest_status = TaskStatus.SKIPPED
        task.latest_status_at = event.created_at
        task.latest_status_event_id = event.id
        task.latest_status_build_id = event.build_id
        task.latest_completed_at = event.created_at
    elif et == EventType.TASK_SUSPENDED:
        task.latest_status = TaskStatus.SUSPENDED
        task.latest_status_at = event.created_at
        task.latest_status_event_id = event.id
        task.latest_status_build_id = event.build_id
    elif et == EventType.TASK_WAITING_FOR_LOCK:
        if task.latest_status == TaskStatus.PENDING:
            task.latest_waiting_for_lock = True
    elif et == EventType.TASK_PENDING:
        # Initial PENDING for a brand-new task: keep PENDING but record the
        # event so the latest_status_at field reflects the most recent
        # contributing event. Don't downgrade an already-non-PENDING state.
        if task.latest_status == TaskStatus.PENDING:
            task.latest_status_at = event.created_at
            task.latest_status_event_id = event.id
            task.latest_status_build_id = event.build_id
    # TASK_REFERENCED is informational and never affects latest_* state
    # (the global semantics treat it as a no-op).


async def get_build_status(
    db: AsyncSession, build_id: UUID
) -> tuple[BuildStatus, datetime | None, datetime | None, str | None, bool]:
    """Get derived build status from events.

    Returns:
        Tuple of (status, started_at, completed_at,
                  status_triggered_by_user_id, is_resumed).

    ``is_resumed`` is True iff the build has at least one BUILD_RESUMED
    event AND that event is more recent than any terminal event — i.e. the
    build was picked up again after finishing/failing and is currently
    treated as RUNNING under the resume semantics. The UI uses this to
    surface a "running (resumed)" affordance.
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
    is_resumed = False

    # Process events from oldest to newest to build final state
    for event in reversed(events):
        if event.event_type == EventType.BUILD_STARTED:
            status = BuildStatus.RUNNING
            started_at = event.created_at
            status_triggered_by_user_id = None  # Not user-triggered
            # Reset is_resumed so the flag stays consistent with its
            # documented semantic (latest build-level event is
            # BUILD_RESUMED). The SDK never emits BUILD_STARTED after a
            # BUILD_RESUMED in normal flow, but the replay shouldn't rely
            # on event ordering — admin/manual event inserts could
            # produce any sequence.
            is_resumed = False
        elif event.event_type == EventType.BUILD_RESUMED:
            # Treat like BUILD_STARTED, but flip the is_resumed flag and
            # clear completed_at so the UI doesn't keep showing a stale
            # "completed at" from the previous terminal event.
            status = BuildStatus.RUNNING
            completed_at = None
            status_triggered_by_user_id = None
            is_resumed = True
            # Don't overwrite started_at — the build started when it first
            # started; resume is a separate concept.
        elif event.event_type == EventType.BUILD_COMPLETED:
            status = BuildStatus.COMPLETED
            completed_at = event.created_at
            is_resumed = False
            # Check if this was user-triggered (manual override)
            status_triggered_by_user_id = (
                event.event_metadata.get("triggered_by_user_id")
                if event.event_metadata
                else None
            )
        elif event.event_type == EventType.BUILD_FAILED:
            status = BuildStatus.FAILED
            completed_at = event.created_at
            is_resumed = False
            status_triggered_by_user_id = (
                event.event_metadata.get("triggered_by_user_id")
                if event.event_metadata
                else None
            )
        elif event.event_type == EventType.BUILD_CANCELLED:
            status = BuildStatus.CANCELLED
            completed_at = event.created_at
            is_resumed = False
            status_triggered_by_user_id = (
                event.event_metadata.get("triggered_by_user_id")
                if event.event_metadata
                else None
            )
        elif event.event_type == EventType.BUILD_EXIT_EARLY:
            status = BuildStatus.EXIT_EARLY
            completed_at = event.created_at
            is_resumed = False
            status_triggered_by_user_id = None  # Not user-triggered

    return status, started_at, completed_at, status_triggered_by_user_id, is_resumed


async def get_task_status_in_build(
    db: AsyncSession, build_id: UUID, task_db_id: UUID
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
        elif event.event_type == EventType.TASK_RETRIED:
            # Retry: reset a retryable status back to PENDING so the task is
            # schedulable again (a re-trigger of a failed build, a new build
            # referencing a previously-failed task, or an abandoned
            # suspension). No-op for completed/running — a retry never
            # downgrades those. See _RETRYABLE_STATUSES.
            if status in _RETRYABLE_STATUSES:
                status = TaskStatus.PENDING
                completed_at = None
                error_message = None
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
    db: AsyncSession, build_id: UUID
) -> dict[UUID, tuple[TaskStatus, datetime | None, datetime | None, str | None]]:
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
        UUID, tuple[TaskStatus, datetime | None, datetime | None, str | None]
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
        elif event.event_type == EventType.TASK_RETRIED:
            # Retry: reset a retryable status back to PENDING so the task is
            # schedulable again (a re-trigger of a failed build, a new build
            # referencing a previously-failed task, or an abandoned
            # suspension). No-op for completed/running — a retry never
            # downgrades those. See _RETRYABLE_STATUSES.
            if status in _RETRYABLE_STATUSES:
                status = TaskStatus.PENDING
                completed_at = None
                error_message = None
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
    db: AsyncSession, task_db_id: UUID
) -> tuple[TaskStatus, datetime | None, datetime | None, str | None, UUID | None]:
    """Get task status considering events from ALL builds.

    Reads the denormalised ``latest_*`` columns on tasks (maintained
    in-transaction by ``apply_event_to_task`` whenever a task event is
    created). Falls back to PENDING for tasks that don't exist.

    Returns:
        Tuple of (status, started_at, completed_at, error_message, completed_in_build_id)
    """
    task = await db.get(Task, task_db_id)
    if task is None:
        return TaskStatus.PENDING, None, None, None, None

    # The "completed_in_build_id" semantic was: the build where TASK_COMPLETED
    # fired. With denormalised columns we use latest_status_build_id when the
    # status is a terminal one (matches existing UI use, since the field is
    # only consulted for non-pending statuses).
    completed_in_build_id = (
        task.latest_status_build_id
        if task.latest_status
        in (
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.SKIPPED,
        )
        else None
    )

    return (
        task.latest_status,
        task.latest_started_at,
        task.latest_completed_at,
        task.latest_error_message,
        completed_in_build_id,
    )


async def get_all_task_global_statuses(
    db: AsyncSession, task_db_ids: list[UUID]
) -> dict[
    UUID,
    tuple[
        TaskStatus,
        datetime | None,
        datetime | None,
        str | None,
        UUID | None,
        bool,
        str | None,
    ],
]:
    """Get global status for multiple tasks considering events from ALL builds.

    Reads the denormalised ``latest_*`` columns on tasks. Tasks not present
    in the DB default to PENDING with no metadata.

    Returns:
        Dict mapping task_db_id to:
        (status, started_at, completed_at, error_message, status_build_id,
         waiting_for_lock, commit_hash)
    """
    if not task_db_ids:
        return {}

    result = await db.execute(
        select(
            Task.id,
            Task.latest_status,
            Task.latest_started_at,
            Task.latest_completed_at,
            Task.latest_error_message,
            Task.latest_status_build_id,
            Task.latest_waiting_for_lock,
            Task.latest_commit_hash,
        ).where(Task.id.in_(task_db_ids))
    )

    found: dict[
        UUID,
        tuple[
            TaskStatus,
            datetime | None,
            datetime | None,
            str | None,
            UUID | None,
            bool,
            str | None,
        ],
    ] = {}
    for (
        task_id,
        latest_status,
        latest_started_at,
        latest_completed_at,
        latest_error_message,
        latest_status_build_id,
        latest_waiting_for_lock,
        latest_commit_hash,
    ) in result.all():
        # latest_status comes back as the string value because the column is
        # String(32). Coerce back to the enum so callers see a TaskStatus.
        status = (
            latest_status
            if isinstance(latest_status, TaskStatus)
            else TaskStatus(latest_status)
        )
        found[task_id] = (
            status,
            latest_started_at,
            latest_completed_at,
            latest_error_message,
            latest_status_build_id,
            bool(latest_waiting_for_lock),
            latest_commit_hash,
        )

    # Preserve the contract of the prior implementation: every requested
    # task_db_id appears in the result dict, with a default for missing rows.
    return {
        task_id: found.get(
            task_id, (TaskStatus.PENDING, None, None, None, None, False, None)
        )
        for task_id in task_db_ids
    }
