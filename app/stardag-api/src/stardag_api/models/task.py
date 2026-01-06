"""Task model for task definitions."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Index, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from stardag_api.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from stardag_api.models.event import Event
    from stardag_api.models.task_asset import TaskRegistryAsset
    from stardag_api.models.task_dependency import TaskDependency
    from stardag_api.models.workspace import Workspace


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
        UniqueConstraint("workspace_id", "task_id", name="uq_task_workspace_taskid"),
        Index("ix_tasks_workspace_name", "workspace_id", "task_name"),
        Index("ix_tasks_workspace_namespace", "workspace_id", "task_namespace"),
    )

    # Auto-increment primary key for efficient joins
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # Deterministic hash from SDK, unique within workspace
    task_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        index=True,
    )
    workspace_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
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

    # Relationships
    workspace: Mapped[Workspace] = relationship(back_populates="tasks")
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
