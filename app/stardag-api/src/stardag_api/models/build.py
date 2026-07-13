"""Build model for tracking DAG executions."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Index, JSON, String, Text, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from stardag_api.models.base import Base, TimestampMixin, generate_uuid7, utc_now

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
        Index(
            "ix_builds_environment_last_active",
            "environment_id",
            "last_active_at",
        ),
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

    # Root task IDs (the tasks passed to sd.build()).
    # JSONB on Postgres for consistency and to avoid reparsing on access.
    root_task_ids: Mapped[list[str]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"),
        nullable=False,
        default=list,
    )

    # Bumped on build-level lifecycle events only (BUILD_RESUMED,
    # BUILD_COMPLETED, BUILD_FAILED, BUILD_CANCELLED, BUILD_EXIT_EARLY) —
    # initial creation sets it via DEFAULT. Task events do NOT touch this
    # column, so the per-task hot path is free of contention on the build
    # row.
    #
    # This column drives the "Home" / list-builds ordering: a resumed
    # build (BUILD_RESUMED) jumps to the top instead of staying buried at
    # its original ``created_at`` position. The trade-off vs touching on
    # every task event is that a long-running build won't bump position
    # while it's mid-execution — but its ``status=running`` badge already
    # signals activity, and "most recent lifecycle change" is a cleaner
    # sort key than "any event in the build's subtree." See
    # ``_touch_build_last_active`` in ``routes/builds.py``.
    last_active_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        nullable=False,
    )

    # Executor-descriptive metadata of the trigger that created (or most
    # recently resumed-with-metadata) the build, e.g. {"kind": "modal",
    # "app_name": ..., "workspace": ..., "environment": ...,
    # "function_name": ..., "reactive": ...}. Set from BuildCreate /
    # the resume endpoint; kept (not cleared) on resumes that don't carry
    # metadata — the in-container SDK resume of a Modal-triggered build
    # doesn't know its trigger metadata.
    executor_metadata: Mapped[dict | None] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"),
        nullable=True,
    )

    # Reactive-scheduler dirty flag: set by POST /builds/{id}/notify (e.g. a
    # worker finishing a task), cleared by the scheduler tick before it
    # computes the frontier (DELETE /builds/{id}/notify). A notify landing
    # between clear and compute re-sets it, so the tick's linger poll picks
    # the wake-up back up — no lost signals. NULL = no pending wake-up.
    needs_tick_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # Relationships
    environment: Mapped[Environment] = relationship(back_populates="builds")
    user: Mapped[User | None] = relationship(back_populates="builds")
    events: Mapped[list[Event]] = relationship(
        back_populates="build",
        cascade="all, delete-orphan",
        order_by="Event.created_at",
    )
