"""Task model for task definitions."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import ForeignKey, Index, JSON, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from stardag_api.models.base import Base, TimestampMixin, generate_uuid7

if TYPE_CHECKING:
    from stardag_api.models.event import Event
    from stardag_api.models.task_asset import TaskRegistryAsset
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

    # Full task data (Pydantic model dump from SDK)
    task_data: Mapped[dict] = mapped_column(JSON, nullable=False)

    # Version from task definition (optional)
    version: Mapped[str | None] = mapped_column(String(64))

    # Output URI (path to task output if it has a FileSystemTarget)
    output_uri: Mapped[str | None] = mapped_column(String(2048))

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

    # Registry assets (rich outputs from completed tasks)
    registry_assets: Mapped[list[TaskRegistryAsset]] = relationship(
        back_populates="task",
        cascade="all, delete-orphan",
        order_by="TaskRegistryAsset.created_at",
    )
