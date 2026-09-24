"""Named concurrency limits, enforced at claim time.

An :class:`EnvironmentConcurrencyLimit` caps how many tasks tagged with a
given key may hold a **live claim** concurrently within an environment,
across all builds. A slot is a :class:`TaskLimitKey` row joined to a live
claim on its ``task``; it frees when the claim does. Enforcement is atomic
in the claiming-start transaction (the limit row is locked while live
holders are counted).
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import (
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from stardag_api.models.base import Base, EnvironmentScopedMixin, generate_uuid7


class EnvironmentConcurrencyLimit(EnvironmentScopedMixin, Base):
    """Cap on concurrently claimed tasks per (environment, key)."""

    __tablename__ = "environment_concurrency_limit"
    __table_args__ = (
        UniqueConstraint(
            "environment_id", "key", name="uq_environment_concurrency_limit_key"
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=generate_uuid7)
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    max_concurrent: Mapped[int] = mapped_column(Integer, nullable=False)


class TaskLimitKey(EnvironmentScopedMixin, Base):
    """A concurrency-limit key of a task's current claim.

    Written **at claim time from the claiming instance** and replaced on
    every claim: limit-key selection may read non-significant fields, so it
    is per instance, while the slot it occupies is per task.
    """

    __tablename__ = "task_limit_key"
    __table_args__ = (
        UniqueConstraint("task_pk", "key", name="uq_task_limit_key_task_key"),
        ForeignKeyConstraint(
            ["environment_id", "task_pk"],
            ["task.environment_id", "task.id"],
            name="fk_task_limit_key_task",
            ondelete="CASCADE",
        ),
        # Slot counting: "live holders of key K in this environment".
        Index("ix_task_limit_key_environment_key", "environment_id", "key"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=generate_uuid7)
    task_pk: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    key: Mapped[str] = mapped_column(String(255), nullable=False)
