"""``transition_task()``: the one writer of task events.

Every move of ``task.status``, every claim grant, renewal and release, and
every execution-ledger write goes through here (engineering rule 2). See
design.md, "The runnable rule" and "Claim × plan invariants".

Interface only in this commit.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.models.enums import TaskStatus

#: Claim TTL when a claiming start or a renewal names none.
DEFAULT_CLAIM_TTL_SECONDS = 3600
#: Upper bound on any requested TTL: nothing is live forever (D11).
MAX_CLAIM_TTL_SECONDS = 24 * 3600


class TransitionKind(str, enum.Enum):
    START = "start"
    COMPLETE = "complete"
    FAIL = "fail"
    SUSPEND = "suspend"
    RETRY = "retry"
    RENEW = "renew"
    # Written by registration, from a driver's observation of the target.
    OBSERVE_COMPLETE = "observe_complete"
    INVALIDATE = "invalidate"


@dataclass(frozen=True)
class Transition:
    kind: TransitionKind
    execution_id: UUID | None = None
    claim: bool = False
    claim_ttl_seconds: int | None = None
    executor: str | None = None
    executor_ref: str | None = None
    executor_metadata: dict[str, Any] | None = None
    error_message: str | None = None
    observed_at: datetime | None = None

    @classmethod
    def start(
        cls,
        execution_id: UUID,
        *,
        claim: bool = True,
        claim_ttl_seconds: int | None = None,
        executor: str | None = None,
        executor_ref: str | None = None,
        executor_metadata: dict[str, Any] | None = None,
    ) -> Transition:
        return cls(
            TransitionKind.START,
            execution_id=execution_id,
            claim=claim,
            claim_ttl_seconds=claim_ttl_seconds,
            executor=executor,
            executor_ref=executor_ref,
            executor_metadata=executor_metadata,
        )

    @classmethod
    def complete(cls, execution_id: UUID) -> Transition:
        return cls(TransitionKind.COMPLETE, execution_id=execution_id)

    @classmethod
    def fail(cls, execution_id: UUID, error_message: str | None = None) -> Transition:
        return cls(
            TransitionKind.FAIL, execution_id=execution_id, error_message=error_message
        )

    @classmethod
    def suspend(cls, execution_id: UUID) -> Transition:
        return cls(TransitionKind.SUSPEND, execution_id=execution_id)

    @classmethod
    def retry(cls) -> Transition:
        return cls(TransitionKind.RETRY)

    @classmethod
    def renew(
        cls, execution_id: UUID, claim_ttl_seconds: int | None = None
    ) -> Transition:
        return cls(
            TransitionKind.RENEW,
            execution_id=execution_id,
            claim_ttl_seconds=claim_ttl_seconds,
        )


@dataclass(frozen=True)
class TransitionOutcome:
    """What a transition did. ``applied`` is False for a no-op or a refusal."""

    applied: bool
    status: TaskStatus
    execution_id: UUID | None
    claim_expires_at: datetime | None


async def transition_task(
    session: AsyncSession,
    environment_id: UUID,
    *,
    task_pk: UUID,
    plan_id: UUID | None,
    transition: Transition,
    now: datetime,
) -> TransitionOutcome:
    """Apply one transition inside the caller's transaction (no commit)."""
    raise NotImplementedError


async def apply_member_transition(
    session: AsyncSession,
    environment_id: UUID,
    *,
    plan_id: UUID,
    task_id: str,
    transition: Transition,
) -> TransitionOutcome:
    """A plan member's start/complete/fail/suspend/retry, as one transaction."""
    raise NotImplementedError


async def renew_claim(
    session: AsyncSession,
    environment_id: UUID,
    *,
    task_id: str,
    execution_id: UUID,
    claim_ttl_seconds: int | None = None,
) -> TransitionOutcome:
    """Extend a live claim, for its holder only."""
    raise NotImplementedError
