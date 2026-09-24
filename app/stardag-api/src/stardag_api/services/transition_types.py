"""The value types of ``transition_task()``: what a transition asks for
(:class:`Transition`), what it did (:class:`TransitionOutcome`), and the
claim-TTL bounds. Split from ``transitions.py`` to keep it under the
module-size rule; ``transitions.py`` re-exports every name.
"""

from __future__ import annotations

import enum
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from stardag_api.models import ClaimOutcome, EventType, ExecutionOutcome, TaskStatus
from stardag_api.services.errors import BadRequest

#: Statuses a member can be started from (plus RUNNING with a lapsed claim).
#: FAILED is absent: the fail mode decides (a retry makes it PENDING).
ACTIONABLE_STATUSES = (
    TaskStatus.PENDING,
    TaskStatus.SUSPENDED,
    TaskStatus.INTERRUPTED,
    TaskStatus.CANCELLED,
    TaskStatus.SKIPPED,
)

#: Claim TTL when a claiming start or a renewal names none.
DEFAULT_CLAIM_TTL_SECONDS = 3600
#: Upper bound on any requested TTL: nothing is live forever (D11).
MAX_CLAIM_TTL_SECONDS = 24 * 3600
#: What a preemption leaves of a claim: the platform restarts the same
#: execution, and a restart that has not reported within this window did not
#: arrive (v1's ``preempt_restart_grace_seconds``, STA-44).
PREEMPT_RESTART_GRACE = timedelta(seconds=900)


class TransitionKind(str, enum.Enum):
    START = "start"
    COMPLETE = "complete"
    FAIL = "fail"
    SUSPEND = "suspend"
    INTERRUPT = "interrupt"
    # Status-neutral: the platform restarts the same execution.
    PREEMPT = "preempt"
    RETRY = "retry"
    # Scheduling decisions that name no execution.
    SKIP = "skip"
    CANCEL = "cancel"
    # An operator (``builds stop``) reports it ended an execution.
    STOP = "stop"
    RENEW = "renew"
    # Written by registration, from a driver's observation of the target.
    OBSERVE_COMPLETE = "observe_complete"
    INVALIDATE = "invalidate"
    # Written by a build's terminal transition (complete / fail / cancel).
    RELEASE = "release"


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
    #: A claiming start's concurrency-limit keys, computed by the tick from
    #: the instance body it is about to run; replace the task's keys.
    limit_keys: tuple[str, ...] = ()
    #: Why a claim is released (the build transition that released it), or
    #: why a member is skipped.
    reason: str | None = None

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
        limit_keys: Sequence[str] = (),
    ) -> Transition:
        return cls(
            TransitionKind.START,
            execution_id=execution_id,
            claim=claim,
            claim_ttl_seconds=claim_ttl_seconds,
            executor=executor,
            executor_ref=executor_ref,
            executor_metadata=executor_metadata,
            limit_keys=tuple(limit_keys),
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
    def interrupt(
        cls, execution_id: UUID, error_message: str | None = None
    ) -> Transition:
        return cls(
            TransitionKind.INTERRUPT,
            execution_id=execution_id,
            error_message=error_message,
        )

    @classmethod
    def preempt(cls, execution_id: UUID) -> Transition:
        return cls(TransitionKind.PREEMPT, execution_id=execution_id)

    @classmethod
    def retry(cls) -> Transition:
        return cls(TransitionKind.RETRY)

    @classmethod
    def skip(cls, reason: str | None = None) -> Transition:
        return cls(TransitionKind.SKIP, reason=reason)

    @classmethod
    def cancel(cls) -> Transition:
        return cls(TransitionKind.CANCEL)

    @classmethod
    def stop(cls, execution_id: UUID) -> Transition:
        return cls(TransitionKind.STOP, execution_id=execution_id)

    @classmethod
    def release(cls, reason: str) -> Transition:
        return cls(TransitionKind.RELEASE, reason=reason)

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
    """What a transition did. ``applied`` is False for a no-op."""

    applied: bool
    status: TaskStatus
    execution_id: UUID | None
    claim_expires_at: datetime | None


# A report's event type, the status it moves the task to, and the two ledger
# outcomes it writes.
REPORTS: dict[
    TransitionKind, tuple[EventType, TaskStatus, ClaimOutcome, ExecutionOutcome]
] = {
    TransitionKind.COMPLETE: (
        EventType.TASK_COMPLETED,
        TaskStatus.COMPLETED,
        ClaimOutcome.COMPLETED,
        ExecutionOutcome.COMPLETED,
    ),
    TransitionKind.FAIL: (
        EventType.TASK_FAILED,
        TaskStatus.FAILED,
        ClaimOutcome.FAILED,
        ExecutionOutcome.FAILED,
    ),
    TransitionKind.SUSPEND: (
        EventType.TASK_SUSPENDED,
        TaskStatus.SUSPENDED,
        ClaimOutcome.SUSPENDED,
        ExecutionOutcome.SUSPENDED,
    ),
    TransitionKind.INTERRUPT: (
        EventType.TASK_INTERRUPTED,
        TaskStatus.INTERRUPTED,
        ClaimOutcome.INTERRUPTED,
        ExecutionOutcome.INTERRUPTED,
    ),
}


def claim_ttl(requested: int | None) -> timedelta:
    seconds = DEFAULT_CLAIM_TTL_SECONDS if requested is None else requested
    if not 0 < seconds <= MAX_CLAIM_TTL_SECONDS:
        raise BadRequest(
            "invalid_claim_ttl",
            f"claim_ttl_seconds must be in (0, {MAX_CLAIM_TTL_SECONDS}]",
            claim_ttl_seconds=seconds,
        )
    return timedelta(seconds=seconds)
