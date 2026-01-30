"""Build model for tracking DAG executions."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import ForeignKey, Index, JSON, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from stardag_api.models.base import Base, TimestampMixin, generate_uuid7

if TYPE_CHECKING:
    from stardag_api.models.event import Event
    from stardag_api.models.user import User
    from stardag_api.models.environment import Environment


class Build(Base, TimestampMixin):
    """Represents execution of sd.build() for a DAG/set of tasks.

    Status is derived from events (no stored status field).
    Timestamps (started_at, completed_at) are derived from events.
    """

    __tablename__ = "builds"
    __table_args__ = (
        Index("ix_builds_environment_created", "environment_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid,
        primary_key=True,
        default=generate_uuid7,
    )
    environment_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("environments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[UUID | None] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # Memorable slug name (e.g., "brave-tiger-42")
    name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    # Optional user-provided documentation
    description: Mapped[str | None] = mapped_column(Text)

    # Git context
    commit_hash: Mapped[str | None] = mapped_column(String(64), index=True)

    # Root task IDs (the tasks passed to sd.build())
    root_task_ids: Mapped[list[str]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
    )

    # Relationships
    environment: Mapped[Environment] = relationship(back_populates="builds")
    user: Mapped[User | None] = relationship(back_populates="builds")
    events: Mapped[list[Event]] = relationship(
        back_populates="build",
        cascade="all, delete-orphan",
        order_by="Event.created_at",
    )
