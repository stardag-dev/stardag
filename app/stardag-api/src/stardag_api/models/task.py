"""``task``: the completion and its global state.

One row per completion hash (``task_id``) per environment. It holds **no
parameters** — those live on ``task_instance`` bodies — only the identity
fields the hash is computed from, the global status, and the claim.
See ``docs/design/registry-v2/design.md``, "Entities".
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from stardag_api.models.base import (
    Base,
    EnvironmentScopedMixin,
    generate_uuid7,
    pg_enum,
)
from stardag_api.models.enums import TaskStatus

if TYPE_CHECKING:
    from stardag_api.models.environment import Environment


class Task(EnvironmentScopedMixin, Base):
    """A completion: "is this output done?" and "who is running it?".

    The claim is **live** when ``status = RUNNING AND claim_expires_at >
    now()``; RUNNING with a past expiry is a **lapsed** claim, which the
    next claiming start takes over. Claim arbitration locks this row with
    ``FOR NO KEY UPDATE`` (not ``FOR UPDATE``), so the ``FOR KEY SHARE``
    that every insert referencing it takes through its foreign key does not
    block on a claim.
    """

    __tablename__ = "task"
    __table_args__ = (
        UniqueConstraint(
            "environment_id", "task_id", name="uq_task_environment_task_id"
        ),
        # Target of every composite FK onto ``task`` (the environment rule).
        UniqueConstraint("environment_id", "id", name="uq_task_environment_id"),
        CheckConstraint(
            "status <> 'running' OR claim_expires_at IS NOT NULL",
            name="ck_task_running_has_claim_expiry",
        ),
        # The holder: a claim can only name a plan that holds this task.
        # SET NULL on the pointer column only — a plain SET NULL would null
        # ``environment_id`` and ``id`` too.
        ForeignKeyConstraint(
            ["environment_id", "claim_plan_id", "id"],
            [
                "plan_member.environment_id",
                "plan_member.plan_id",
                "plan_member.task_pk",
            ],
            name="fk_task_claim_plan_member",
            ondelete="SET NULL (claim_plan_id)",
            use_alter=True,
        ),
        # The current execution, which must be an execution of this task.
        ForeignKeyConstraint(
            ["environment_id", "id", "execution_id"],
            ["execution.environment_id", "execution.task_pk", "execution.id"],
            name="fk_task_execution",
            ondelete="SET NULL (execution_id)",
            use_alter=True,
        ),
        Index("ix_task_environment_status", "environment_id", "status", "status_at"),
        Index("ix_task_environment_name", "environment_id", "task_name"),
        Index(
            "ix_task_running_claim_plan",
            "claim_plan_id",
            postgresql_where=text("status = 'running'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=generate_uuid7)

    # The completion hash, computed by the SDK over the completion-significant
    # parameters. User-facing, and the key every client addresses a task by.
    task_id: Mapped[str] = mapped_column(String(64), nullable=False)

    # Identity-level: part of the hash, or derived from it. An existing row
    # must agree on all four at registration (409 task_identity_conflict).
    task_namespace: Mapped[str] = mapped_column(
        String(255), nullable=False, default="", server_default=""
    )
    task_name: Mapped[str] = mapped_column(String(255), nullable=False)
    version: Mapped[str | None] = mapped_column(String(64))
    output_uri: Mapped[str | None] = mapped_column(String(2048))

    status: Mapped[TaskStatus] = mapped_column(
        pg_enum(TaskStatus, "task_status"),
        nullable=False,
        default=TaskStatus.PENDING,
        server_default=TaskStatus.PENDING.value,
    )
    status_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Timestamp of the completion now on record; an invalidation compares the
    # observation's ``observed_at`` against it.
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_message: Mapped[str | None] = mapped_column(Text)

    # Expiry of the claim. NOT NULL whenever RUNNING (CHECK above): every
    # execution has a finite expiry, nothing is live forever.
    claim_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # The plan holding the claim. Implies the build and, via ``plan_member``,
    # the instance body that is running.
    claim_plan_id: Mapped[UUID | None] = mapped_column(Uuid)

    # Current execution, minted by the client before the claim. Executor
    # details are read from the execution row, never copied here.
    execution_id: Mapped[UUID | None] = mapped_column(Uuid)

    # When the platform last reported a preemption: a restart is due.
    preempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    environment: Mapped[Environment] = relationship(back_populates="tasks")
