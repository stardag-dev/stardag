"""Enumeration types for database models."""

import enum


class TaskStatus(str, enum.Enum):
    """Derived status for tasks, computed from events."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class BuildStatus(str, enum.Enum):
    """Derived status for builds, computed from events."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class EventType(str, enum.Enum):
    """Event types for the append-only event log."""

    # Build events
    BUILD_STARTED = "build_started"
    BUILD_COMPLETED = "build_completed"
    BUILD_FAILED = "build_failed"
    BUILD_CANCELLED = "build_cancelled"

    # Task events (within a build)
    TASK_PENDING = "task_pending"
    TASK_STARTED = "task_started"
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"
    TASK_SKIPPED = "task_skipped"
