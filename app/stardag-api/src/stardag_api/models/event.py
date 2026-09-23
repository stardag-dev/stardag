"""``event``: the append-only log.

Events are never updated. ``build_id``, ``plan_id`` and ``execution_id`` are
``ON DELETE SET NULL``, not CASCADE: deleting a build must not erase the
history the global status was folded from. ``report_applied`` and
``batch_id`` are typed columns — facts queries depend on are never JSON
flags.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    Text,
    Uuid,
    text,
    true,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from stardag_api.models.base import (
    Base,
    EnvironmentScopedMixin,
    generate_uuid7,
    pg_enum,
)
from stardag_api.models.enums import BUILD_EVENT_TYPES, EventType

_BUILD_EVENT_LABELS = ", ".join(f"'{t.value}'" for t in BUILD_EVENT_TYPES)


class Event(EnvironmentScopedMixin, Base):
    """One recorded fact: a build lifecycle step, a task transition, a report.

    ``task_pk`` is NULL for build-level events; ``plan_id`` is NULL for
    build-level events (CHECK) and for events of no plan; ``build_id`` is
    NULL for events that belong to no build, and after the build is deleted.
    """

    __tablename__ = "event"
    __table_args__ = (
        CheckConstraint(
            f"plan_id IS NULL OR event_type NOT IN ({_BUILD_EVENT_LABELS})",
            name="ck_event_build_level_has_no_plan",
        ),
        ForeignKeyConstraint(
            ["environment_id", "build_id"],
            ["build.environment_id", "build.id"],
            name="fk_event_build",
            ondelete="SET NULL (build_id)",
        ),
        ForeignKeyConstraint(
            ["environment_id", "task_pk"],
            ["task.environment_id", "task.id"],
            name="fk_event_task",
        ),
        ForeignKeyConstraint(
            ["environment_id", "plan_id"],
            ["plan.environment_id", "plan.id"],
            name="fk_event_plan",
            ondelete="SET NULL (plan_id)",
        ),
        ForeignKeyConstraint(
            ["environment_id", "execution_id"],
            ["execution.environment_id", "execution.id"],
            name="fk_event_execution",
            ondelete="SET NULL (execution_id)",
        ),
        Index("ix_event_build_created", "build_id", "created_at"),
        Index("ix_event_task_created", "task_pk", "created_at"),
        Index("ix_event_environment_created", "environment_id", "created_at"),
        Index("ix_event_plan", "plan_id"),
        Index("ix_event_execution", "execution_id"),
        # A /yield batch is applied once per execution: two concurrent
        # retries cannot both miss the lookup.
        Index(
            "uq_event_execution_batch",
            "execution_id",
            "batch_id",
            unique=True,
            postgresql_where=text("batch_id IS NOT NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=generate_uuid7)
    build_id: Mapped[UUID | None] = mapped_column(Uuid)
    task_pk: Mapped[UUID | None] = mapped_column(Uuid)
    plan_id: Mapped[UUID | None] = mapped_column(Uuid)
    execution_id: Mapped[UUID | None] = mapped_column(Uuid)
    event_type: Mapped[EventType] = mapped_column(
        pg_enum(EventType, "event_type"), nullable=False
    )
    # False for a report that was recorded but refused (a late report, a
    # second terminal report, a stale execution): it is history, not state.
    report_applied: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=true()
    )
    # Client-minted id of one /yield batch (TASK_YIELDED).
    batch_id: Mapped[UUID | None] = mapped_column(Uuid)
    error_message: Mapped[str | None] = mapped_column(Text)
    event_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    # Re-declared only to type it for readers; the mixin supplies the column.
    created_at: Mapped[datetime]
