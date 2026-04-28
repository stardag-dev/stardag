"""Task model for task definitions."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    JSON,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from stardag_api.models.base import Base, TimestampMixin, generate_uuid7
from stardag_api.models.enums import TaskStatus

if TYPE_CHECKING:
    from stardag_api.models.event import Event
    from stardag_api.models.task_artifact import TaskArtifact
    from stardag_api.models.task_dependency import TaskDependency
    from stardag_api.models.environment import Environment


class Task(Base, TimestampMixin):
    """Task definition - represents the static properties of a task.

    task_id is a deterministic hash computed by the SDK based on:
    - namespace
    - name
    - parameters (hash)

    A Task can participate in multiple Runs. Status per-run is tracked via Events.
    """

    __tablename__ = "tasks"
    __table_args__ = (
        UniqueConstraint(
            "environment_id", "task_id", name="uq_task_environment_taskid"
        ),
        Index("ix_tasks_environment_name", "environment_id", "task_name"),
        Index("ix_tasks_environment_namespace", "environment_id", "task_namespace"),
        # Covers the common "list/search tasks in env, newest first" pattern
        # used by the Task Explorer UI and /tasks/values, /tasks/keys.
        Index("ix_tasks_environment_created", "environment_id", "created_at"),
    )

    # UUID7 primary key for time-sortable, globally unique IDs
    id: Mapped[UUID] = mapped_column(
        Uuid,
        primary_key=True,
        default=generate_uuid7,
    )

    # Deterministic hash from SDK, unique within workspace
    task_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        index=True,
    )
    environment_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("environments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Task identity
    task_namespace: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        default="",
    )
    task_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        index=True,
    )

    # Full task data (Pydantic model dump from SDK).
    # Use JSONB on Postgres so `->>` doesn't reparse the source text on every
    # access (used by the Task Explorer search filters); SQLite keeps JSON.
    task_data: Mapped[dict] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"),
        nullable=False,
    )

    # Version from task definition (optional)
    version: Mapped[str | None] = mapped_column(String(64))

    # Output URI (path to task output if it has a FileSystemTarget)
    output_uri: Mapped[str | None] = mapped_column(String(2048))

    # Phantom flag: True for tasks created as placeholders for unresolved dependencies.
    # These are upgraded to real tasks when properly registered via the SDK.
    is_phantom: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # ------------------------------------------------------------------
    # Denormalised "latest global status" columns.
    #
    # These are maintained in-transaction whenever a task event is created
    # (see services.status.apply_event_to_task and routes/builds.py event
    # handlers). They mirror the semantics of get_all_task_global_statuses:
    # COMPLETED is sticky, RUNNING/FAILED/CANCELLED win over PENDING, etc.
    #
    # They exist so that read endpoints (Task Explorer, build graph, search)
    # can return per-task status without scanning the events table for
    # every request — see the 2026-04-27 incident for the motivation.
    # ------------------------------------------------------------------
    latest_status: Mapped[TaskStatus] = mapped_column(
        String(32),
        nullable=False,
        default=TaskStatus.PENDING,
        index=True,
    )
    latest_status_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
    )
    latest_status_event_id: Mapped[UUID | None] = mapped_column(Uuid)
    latest_status_build_id: Mapped[UUID | None] = mapped_column(
        Uuid,
        ForeignKey("builds.id", ondelete="SET NULL"),
    )
    latest_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
    )
    latest_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
    )
    latest_error_message: Mapped[str | None] = mapped_column(Text)
    latest_waiting_for_lock: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    latest_commit_hash: Mapped[str | None] = mapped_column(String(64))

    # Relationships
    environment: Mapped[Environment] = relationship(back_populates="tasks")
    events: Mapped[list[Event]] = relationship(
        back_populates="task",
        cascade="all, delete-orphan",
        order_by="Event.created_at",
    )

    # Dependencies (self-referential many-to-many)
    upstream_edges: Mapped[list[TaskDependency]] = relationship(
        "TaskDependency",
        foreign_keys="TaskDependency.downstream_task_id",
        back_populates="downstream_task",
        cascade="all, delete-orphan",
    )
    downstream_edges: Mapped[list[TaskDependency]] = relationship(
        "TaskDependency",
        foreign_keys="TaskDependency.upstream_task_id",
        back_populates="upstream_task",
        cascade="all, delete-orphan",
    )

    # Artifacts (rich outputs from completed tasks)
    artifacts: Mapped[list[TaskArtifact]] = relationship(
        back_populates="task",
        cascade="all, delete-orphan",
        order_by="TaskArtifact.created_at",
    )
