"""The shared state of one ``transition_task()`` step (:class:`StepBase`).

Split from ``transitions.py`` by concern, to keep each module under the
module-size rule: this module holds what every transition needs (the
locked row, the event record, the ledger's server end); the reports are in
``transition_reports.py``; the claim, and the dispatch, in
``transitions.py``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.models import (
    ClaimOutcome,
    EventType,
    Execution,
    Plan,
    Task,
    TaskStatus,
)
from stardag_api.services import event_log
from stardag_api.services.errors import BadRequest, RecordedConflict
from stardag_api.services.transition_types import Transition, TransitionOutcome


class StepBase:
    """One transition on one locked task row: the state and the helpers
    every kind of transition shares.

    ``now`` is read **after** the row lock was granted, so every timestamp
    written to the task and the ledger (``status_at``, ``completed_at``,
    ``started_at``, the claim expiry, ``claim_released_at``, ``ended_at``)
    is the time the transition actually took effect; the ``observed_at``
    guard compares against the real completion time, not the time the
    caller started waiting for the lock. ``event_at`` is the caller's
    clock, which orders the events of one transaction.
    """

    def __init__(
        self,
        session: AsyncSession,
        environment_id: UUID,
        task: Task,
        plan_id: UUID | None,
        transition: Transition,
        now: datetime,
        event_at: datetime,
    ) -> None:
        self.session = session
        self.environment_id = environment_id
        self.task = task
        self.plan_id = plan_id
        self.transition = transition
        self.now = now
        self.event_at = event_at

    # -- shared --------------------------------------------------------------

    @property
    def live(self) -> bool:
        t = self.task
        return (
            t.status == TaskStatus.RUNNING
            and t.claim_expires_at is not None
            and t.claim_expires_at > self.now
        )

    def outcome(self, applied: bool) -> TransitionOutcome:
        return TransitionOutcome(
            applied=applied,
            status=self.task.status,
            execution_id=self.task.execution_id,
            claim_expires_at=self.task.claim_expires_at,
        )

    def execution_id(self) -> UUID:
        if self.transition.execution_id is None:
            raise BadRequest(
                "execution_id_required",
                f"a {self.transition.kind.value} names its execution",
            )
        return self.transition.execution_id

    async def build_id(self) -> UUID | None:
        if self.plan_id is None:
            return None
        return await self.session.scalar(
            select(Plan.build_id).where(Plan.id == self.plan_id)
        )

    async def record(
        self,
        event_type: EventType,
        *,
        execution_id: UUID | None = None,
        report_applied: bool = True,
        error_message: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        await event_log.append(
            self.session,
            [
                event_log.event_row(
                    self.environment_id,
                    event_type,
                    at=self.event_at,
                    build_id=await self.build_id(),
                    task_pk=self.task.id,
                    plan_id=self.plan_id,
                    execution_id=execution_id,
                    report_applied=report_applied,
                    error_message=error_message,
                    metadata=metadata,
                )
            ],
        )

    async def release_ledger(self, outcome: ClaimOutcome) -> None:
        """The server's end of the ledger for the current execution: the
        claim moved (``claim_released_at``/``claim_outcome``)."""
        if self.task.execution_id is not None:
            await self.session.execute(
                update(Execution)
                .where(
                    Execution.id == self.task.execution_id,
                    Execution.claim_released_at.is_(None),
                )
                .values(claim_released_at=self.now, claim_outcome=outcome)
            )

    async def close_claim(self, outcome: ClaimOutcome) -> None:
        """Whatever moves the task off RUNNING closes the current claim:
        the ledger end, then the task's claim columns. The caller moves the
        status before the next flush."""
        await self.release_ledger(outcome)
        self.task.claim_plan_id = None
        self.task.claim_expires_at = None

    def move(self, status: TaskStatus) -> None:
        self.task.status = status
        self.task.status_at = self.now

    async def named_execution(self, event_type: EventType, eid: UUID) -> Execution:
        """The named execution of this task, or a recorded refusal."""
        execution = await self.session.get(Execution, eid)
        if execution is None or execution.task_pk != self.task.id:
            await self.record(
                event_type,
                report_applied=False,
                metadata={"execution_id": str(eid), "refused": "unknown_execution"},
            )
            raise RecordedConflict(
                "unknown_execution",
                "no execution with this id exists for the task",
                execution_id=str(eid),
            )
        return execution
