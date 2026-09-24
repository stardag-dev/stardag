"""``execution``: the ledger of executions.

One row per execution (the client-minted ``execution_id`` of STA-50),
updated only at its two ends. It never decides anything — liveness is the
claim on ``task`` — but it is where executor details live, where attempts
and interruptions are counted from, and what ``builds stop`` lists.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from stardag_api.models.base import Base, EnvironmentScopedMixin, pg_enum
from stardag_api.models.enums import ClaimOutcome, ExecutionOutcome


class Execution(EnvironmentScopedMixin, Base):
    """Which promise, under which request, from which body — and how it ended.

    Two ends, two writers, kept apart:

    - ``claim_released_at`` / ``claim_outcome``: written by the **server**
      whenever the task leaves RUNNING or the claim changes hands.
    - ``ended_at`` / ``outcome``: written only by the execution's **own
      report**, or an operator stop. ``ended_at IS NULL`` means "no report
      of this execution ending has arrived", independently of whether the
      claim has since moved (an execution ref is not a claim).
    """

    __tablename__ = "execution"
    __table_args__ = (
        # FK targets: event.execution_id, and task.execution_id (which must
        # name an execution of that task).
        UniqueConstraint("environment_id", "id", name="uq_execution_environment_id"),
        UniqueConstraint(
            "environment_id", "task_pk", "id", name="uq_execution_task_id"
        ),
        CheckConstraint(
            "(claim_released_at IS NULL) = (claim_outcome IS NULL)",
            name="ck_execution_claim_release_has_outcome",
        ),
        CheckConstraint(
            "(ended_at IS NULL) = (outcome IS NULL)",
            name="ck_execution_end_has_outcome",
        ),
        ForeignKeyConstraint(
            ["environment_id", "task_pk"],
            ["task.environment_id", "task.id"],
            name="fk_execution_task",
        ),
        ForeignKeyConstraint(
            ["environment_id", "instance_id", "task_pk"],
            [
                "task_instance.environment_id",
                "task_instance.id",
                "task_instance.task_pk",
            ],
            name="fk_execution_instance_task",
        ),
        ForeignKeyConstraint(
            ["environment_id", "plan_id", "instance_id"],
            [
                "plan_member.environment_id",
                "plan_member.plan_id",
                "plan_member.instance_id",
            ],
            name="fk_execution_plan_member",
            ondelete="CASCADE",
        ),
        Index("ix_execution_plan_instance", "plan_id", "instance_id"),
        Index("ix_execution_task_started", "task_pk", "started_at"),
        # What ``builds stop`` and the build-delete guard look for.
        Index(
            "ix_execution_unended",
            "plan_id",
            postgresql_where=text("ended_at IS NULL"),
        ),
    )

    # Client-minted before the claim.
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    task_pk: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    plan_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    instance_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)

    # The one place these live. Nullable: the claim is taken before the
    # spawn, so there is no executor ref yet at claim time.
    executor: Mapped[str | None] = mapped_column(String(32))
    executor_ref: Mapped[str | None] = mapped_column(String(255))
    executor_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    # The claim was granted.
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    claim_released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_outcome: Mapped[ClaimOutcome | None] = mapped_column(
        pg_enum(ClaimOutcome, "execution_claim_outcome")
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[ExecutionOutcome | None] = mapped_column(
        pg_enum(ExecutionOutcome, "execution_outcome")
    )
