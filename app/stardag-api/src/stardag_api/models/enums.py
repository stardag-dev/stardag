"""Enumeration types for database models."""

import enum


class WorkspaceRole(str, enum.Enum):
    """Role of a user within a workspace."""

    OWNER = "owner"  # Full control, cannot be removed, can transfer ownership
    ADMIN = "admin"  # Can manage members and environments
    MEMBER = "member"  # Read/write access to environments


class InviteStatus(str, enum.Enum):
    """Status of a workspace invite."""

    PENDING = "pending"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    CANCELLED = "cancelled"


class TaskStatus(str, enum.Enum):
    """Derived status for tasks, computed from events."""

    PENDING = "pending"
    RUNNING = "running"
    SUSPENDED = "suspended"  # Waiting for dynamic dependencies
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"  # Explicitly cancelled by user


class BuildStatus(str, enum.Enum):
    """Derived status for builds, computed from events."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXIT_EARLY = "exit_early"  # All remaining tasks running in other builds


class EventType(str, enum.Enum):
    """Event types for the append-only event log."""

    # Build events
    BUILD_STARTED = "build_started"
    BUILD_COMPLETED = "build_completed"
    BUILD_FAILED = "build_failed"
    BUILD_CANCELLED = "build_cancelled"
    BUILD_EXIT_EARLY = "build_exit_early"  # All remaining tasks running in other builds

    # Task events (within a build)
    TASK_PENDING = "task_pending"
    TASK_REFERENCED = (
        "task_referenced"  # Task already existed, referenced by this build
    )
    TASK_STARTED = "task_started"
    TASK_SUSPENDED = "task_suspended"  # Task waiting for dynamic dependencies
    TASK_RESUMED = "task_resumed"  # Task resuming after dynamic deps complete
    TASK_WAITING_FOR_LOCK = (
        "task_waiting_for_lock"  # Blocked by global lock in another build
    )
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"
    TASK_SKIPPED = "task_skipped"
    TASK_CANCELLED = "task_cancelled"  # Explicitly cancelled by user
