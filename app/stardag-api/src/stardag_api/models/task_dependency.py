"""TaskDependency model for graph edges."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Index, Integer, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from stardag_api.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from stardag_api.models.task import Task


class TaskDependency(Base, TimestampMixin):
    """Graph edges representing task dependencies.

    upstream_task_id -> downstream_task_id means:
    "downstream depends on upstream" or "upstream must complete before downstream"

    Supports efficient graph traversal queries for:
    - Finding all upstream dependencies (what does this task depend on?)
    - Finding all downstream dependents (what depends on this task?)
    - Full DAG visualization
    """

    __tablename__ = "task_dependencies"
    __table_args__ = (
        UniqueConstraint(
            "upstream_task_id",
            "downstream_task_id",
            name="uq_task_dependency_edge",
        ),
        Index("ix_task_dep_upstream", "upstream_task_id"),
        Index("ix_task_dep_downstream", "downstream_task_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    upstream_task_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
    )
    downstream_task_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Relationships
    upstream_task: Mapped[Task] = relationship(
        foreign_keys=[upstream_task_id],
        back_populates="downstream_edges",
    )
    downstream_task: Mapped[Task] = relationship(
        foreign_keys=[downstream_task_id],
        back_populates="upstream_edges",
    )
