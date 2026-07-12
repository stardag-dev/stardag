"""Named concurrency limits, enforced at task start.

An :class:`EnvironmentConcurrencyLimit` caps how many tasks tagged with a
given key may be RUNNING concurrently within an environment — across all
builds. A task "holds a slot" simply by being in RUNNING status with the
key recorded (see :class:`TaskLimitKey`); the slot frees when any terminal
event lands. No leases or TTLs: task-status liveness is already maintained
by worker-side lifecycle reporting and scheduler-tick self-healing.

Enforcement is atomic in the task-start transaction (the limit row is
locked FOR UPDATE while active holders are counted), see
``routes/builds.py::start_task``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import ForeignKey, Index, Integer, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from stardag_api.models.base import Base, generate_uuid7

if TYPE_CHECKING:
    from stardag_api.models.environment import Environment


class EnvironmentConcurrencyLimit(Base):
    """Cap on concurrently-RUNNING tasks per (environment, key)."""

    __tablename__ = "environment_concurrency_limits"
    __table_args__ = (
        UniqueConstraint(
            "environment_id", "key", name="uq_environment_concurrency_limit_key"
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
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    max_concurrent: Mapped[int] = mapped_column(Integer, nullable=False)

    environment: Mapped[Environment] = relationship()


class TaskLimitKey(Base):
    """Concurrency-limit keys a task was started under.

    Written when a TASK_STARTED event carries ``limit_keys``; a row plus
    the task's RUNNING status together constitute one occupied slot for
    that key.
    """

    __tablename__ = "task_limit_keys"
    __table_args__ = (
        UniqueConstraint("task_pk", "key", name="uq_task_limit_key"),
        Index("ix_task_limit_keys_key", "key"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid,
        primary_key=True,
        default=generate_uuid7,
    )
    task_pk: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    key: Mapped[str] = mapped_column(String(255), nullable=False)
