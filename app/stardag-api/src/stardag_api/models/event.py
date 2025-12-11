"""Event model for immutable append-only event log."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Index, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from stardag_api.models.base import Base, generate_uuid, utc_now
from stardag_api.models.enums import EventType

if TYPE_CHECKING:
    from stardag_api.models.run import Run
    from stardag_api.models.task import Task


class Event(Base):
    """IMMUTABLE append-only event log.

    All state changes are recorded as events. Task and Run status
    are derived from the latest relevant event.

    Every event has a run_id. Most events also have a task_id
    (task-level events), but run-level events (RUN_STARTED, etc.)
    may not have a task_id.
    """

    __tablename__ = "events"
    __table_args__ = (
        Index("ix_events_run_created", "run_id", "created_at"),
        Index("ix_events_task_created", "task_id", "created_at"),
        Index("ix_events_type_created", "event_type", "created_at"),
        Index("ix_events_run_task_type", "run_id", "task_id", "event_type"),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=generate_uuid,
    )

    # Always required - every event belongs to a run
    run_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Optional - task-level events have this, run-level events may not
    task_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    event_type: Mapped[EventType] = mapped_column(
        String(32),
        nullable=False,
        index=True,
    )

    # Event timestamp (when the event occurred)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        nullable=False,
        index=True,
    )

    # Optional error message for failure events
    error_message: Mapped[str | None] = mapped_column(Text)

    # Additional event data (flexible JSON)
    event_metadata: Mapped[dict | None] = mapped_column(JSON)

    # Relationships
    run: Mapped[Run] = relationship(back_populates="events")
    task: Mapped[Task | None] = relationship(back_populates="events")
