"""The reports and the non-claiming transitions of ``transition_task()``.

Split from ``transitions.py`` by concern (module-size rule). Every method
here runs on the locked task row of a :class:`StepBase`:

- **Execution reports** — complete, fail, suspend, interrupt, preempt —
  follow the authority rule: applied when they name the task's current
  execution whose claim has not been released, lapsed or not; a late one
  writes its ledger end (except a preemption, which is not an end) and is
  recorded ``report_applied = false``.
- **Scheduling decisions** that name no execution — retry, skip, a build's
  release, a single task's cancel — are refused against a live claim they
  do not own and idempotent by state.
- **Observations** from registration — observe-complete, invalidate.
"""

from __future__ import annotations

from sqlalchemy import select

from stardag_api.models import (
    ClaimOutcome,
    EventType,
    ExecutionOutcome,
    Plan,
    TaskStatus,
)
from stardag_api.services.errors import Conflict, RecordedConflict
from stardag_api.services.transition_step import StepBase
from stardag_api.services.transition_types import (
    PREEMPT_RESTART_GRACE,
    REPORTS,
    TransitionOutcome,
    claim_ttl,
)

#: Statuses a single ``skip`` (and ``skip-blocked``) moves to SKIPPED: a
#: member that has not run to a result and holds no live claim. FAILED and
#: CANCELLED are results of their own a skip must not overwrite.
SKIPPABLE_STATUSES = (
    TaskStatus.PENDING,
    TaskStatus.SUSPENDED,
    TaskStatus.INTERRUPTED,
)


class ReportSteps(StepBase):
    # -- execution reports ------------------------------------------------------

    async def report(self) -> TransitionOutcome:
        """complete / fail / suspend / interrupt, under the authority rule."""
        t, eid = self.task, self.execution_id()
        event_type, status, claim_outcome, outcome = REPORTS[self.transition.kind]
        execution = await self.named_execution(event_type, eid)
        error = self.transition.error_message
        if execution.ended_at is not None:
            await self.record(
                event_type, execution_id=eid, report_applied=False, error_message=error
            )
            raise RecordedConflict(
                "execution_already_ended",
                "this execution has already reported its end",
                execution_id=str(eid),
                outcome=execution.outcome.value if execution.outcome else None,
            )
        # The execution's own end, whether or not it may still move the task.
        execution.ended_at = self.now
        execution.outcome = outcome
        # Current and not yet released — a lapsed claim included: it names
        # this execution until a claiming start takes it over.
        if t.execution_id != eid or execution.claim_released_at is not None:
            await self.record(
                event_type, execution_id=eid, report_applied=False, error_message=error
            )
            await self.session.flush()
            raise RecordedConflict(
                "execution_not_current",
                "the execution's claim has been released (taken over, closed or"
                " released by its build); its end is recorded and the task is"
                " unchanged",
                execution_id=str(eid),
            )
        # An unreleased claim of the current execution is RUNNING: every
        # move off RUNNING releases it.
        assert t.status == TaskStatus.RUNNING, t.status
        await self.close_claim(claim_outcome)
        self.move(status)
        t.preempted_at = None
        if status == TaskStatus.COMPLETED:
            t.completed_at = self.now
            t.error_message = None
        elif status in (TaskStatus.FAILED, TaskStatus.INTERRUPTED):
            # Assigned unconditionally: a previous failure's text must not
            # explain this one.
            t.error_message = error
        await self.record(event_type, execution_id=eid, error_message=error)
        await self.session.flush()
        return self.outcome(applied=True)

    async def preempt(self) -> TransitionOutcome:
        """The platform is restarting the same execution itself: status-
        neutral, the claim kept, its expiry pulled forward to a restart
        grace — strictly forward, never back — and ``preempted_at`` set, so
        a restart that never arrives leaves a lapsed claim within minutes
        (v1's rule, STA-44). Not an end of the execution: its restart
        reports under the same id. Authority as for every report."""
        t, eid = self.task, self.execution_id()
        execution = await self.named_execution(EventType.TASK_PREEMPTED, eid)
        if (
            execution.ended_at is not None
            or t.execution_id != eid
            or execution.claim_released_at is not None
        ):
            await self.record(
                EventType.TASK_PREEMPTED, execution_id=eid, report_applied=False
            )
            raise RecordedConflict(
                "execution_not_current",
                "the execution does not hold the task's claim; the preemption"
                " is recorded and the task is unchanged",
                execution_id=str(eid),
            )
        assert t.status == TaskStatus.RUNNING and t.claim_expires_at is not None
        expires_at = min(t.claim_expires_at, self.now + PREEMPT_RESTART_GRACE)
        t.claim_expires_at = expires_at
        t.preempted_at = self.now
        await self.record(
            EventType.TASK_PREEMPTED,
            execution_id=eid,
            metadata={"claim_expires_at": expires_at.isoformat()},
        )
        await self.session.flush()
        return self.outcome(applied=True)

    # -- scheduling decisions ----------------------------------------------------

    async def retry(self) -> TransitionOutcome:
        """Reset to PENDING (fail mode's retry, or an operator's). Idempotent
        by state; refused on COMPLETED and on a live claim."""
        t = self.task
        self._refuse_completed_or_live()
        if t.status == TaskStatus.PENDING:
            return self.outcome(applied=False)
        if t.status == TaskStatus.RUNNING:  # a lapsed claim
            await self.close_claim(ClaimOutcome.LAPSED)
        self.move(TaskStatus.PENDING)
        t.error_message = None
        await self.record(EventType.TASK_RETRIED)
        await self.session.flush()
        return self.outcome(applied=True)

    async def skip(self) -> TransitionOutcome:
        """SKIPPED: the member cannot run in this build (an upstream failed,
        was cancelled or skipped). A scheduling decision, not a report: it
        names no execution. Idempotent by state; refused on COMPLETED, on a
        live claim and on FAILED / CANCELLED (409 ``task_not_skippable``). A
        lapsed claim is closed first."""
        t = self.task
        if t.status == TaskStatus.SKIPPED:
            return self.outcome(applied=False)
        self._refuse_completed_or_live()
        if t.status not in (*SKIPPABLE_STATUSES, TaskStatus.RUNNING):
            raise Conflict(
                "task_not_skippable",
                f"a {t.status.value} task is not skipped",
                task_id=t.task_id,
                status=t.status.value,
            )
        if t.status == TaskStatus.RUNNING:  # a lapsed claim
            await self.close_claim(ClaimOutcome.LAPSED)
        self.move(TaskStatus.SKIPPED)
        await self.record(
            EventType.TASK_SKIPPED,
            metadata={"reason": self.transition.reason}
            if self.transition.reason
            else None,
        )
        await self.session.flush()
        return self.outcome(applied=True)

    async def cancel(self) -> TransitionOutcome:
        """A single task's cancel, by **the build holding its claim** only
        (409 ``not_claim_holder`` otherwise): CANCELLED, ``claim_outcome =
        cancelled``. Idempotent on CANCELLED. The execution's ``ended_at``
        is untouched — cancellation is cooperative, the worker finds out at
        its own checkpoints, and its report is late from then on."""
        t = self.task
        if t.status == TaskStatus.CANCELLED:
            return self.outcome(applied=False)
        holder = None
        if t.status == TaskStatus.RUNNING and t.claim_plan_id is not None:
            holder = await self.session.scalar(
                select(Plan.build_id).where(Plan.id == t.claim_plan_id)
            )
        caller = await self.build_id()
        if holder is None or holder != caller:
            raise Conflict(
                "not_claim_holder",
                "only the build holding the task's claim can cancel it",
                task_id=t.task_id,
            )
        execution_id = t.execution_id
        await self.close_claim(ClaimOutcome.CANCELLED)
        self.move(TaskStatus.CANCELLED)
        await self.record(EventType.TASK_CANCELLED, execution_id=execution_id)
        await self.session.flush()
        return self.outcome(applied=True)

    async def stop(self) -> TransitionOutcome:
        """An operator stopped the execution (``builds stop``), or the
        backend reports its container gone: the ledger end ``ended_at``,
        ``outcome = stopped``. Idempotent: an execution that already ended
        is left as it ended. If it still holds the task's claim (live or
        lapsed), nothing will ever report for it, so the claim is released
        too — ``claim_outcome = cancelled``, the task CANCELLED, which is
        ACTIONABLE for every build holding it."""
        t, eid = self.task, self.execution_id()
        execution = await self.named_execution(EventType.TASK_CANCELLED, eid)
        if execution.ended_at is not None:
            return self.outcome(applied=False)
        execution.ended_at = self.now
        execution.outcome = ExecutionOutcome.STOPPED
        holds = t.execution_id == eid and execution.claim_released_at is None
        if holds:
            assert t.status == TaskStatus.RUNNING, t.status
            await self.close_claim(ClaimOutcome.CANCELLED)
            self.move(TaskStatus.CANCELLED)
        await self.record(
            EventType.TASK_CANCELLED,
            execution_id=eid,
            report_applied=holds,
            metadata={"stopped": True},
        )
        await self.session.flush()
        return self.outcome(applied=True)

    async def renew(self) -> TransitionOutcome:
        t, eid = self.task, self.execution_id()
        if t.execution_id != eid or not self.live:
            raise Conflict(
                "claim_not_held",
                "only the execution holding the live claim can renew it",
                execution_id=str(eid),
            )
        t.claim_expires_at = self.now + claim_ttl(self.transition.claim_ttl_seconds)
        await self.session.flush()
        return self.outcome(applied=True)

    async def release(self) -> TransitionOutcome:
        """A build's terminal transition releases the claim its plan holds
        (``claim_outcome = released``): the task goes CANCELLED — the build
        stopped wanting it, which is not a result, so CANCELLED is
        ACTIONABLE for every other build ("revocation is not a result").
        A no-op unless ``plan_id`` holds the task's claim, live or lapsed.
        The execution is not touched: it may still be running, and its
        report, now late, is recorded and refused."""
        t = self.task
        if t.status != TaskStatus.RUNNING or t.claim_plan_id != self.plan_id:
            return self.outcome(applied=False)
        execution_id = t.execution_id
        await self.close_claim(ClaimOutcome.RELEASED)
        self.move(TaskStatus.CANCELLED)
        await self.record(
            EventType.TASK_CANCELLED,
            execution_id=execution_id,
            metadata={"released_by": self.transition.reason},
        )
        await self.session.flush()
        return self.outcome(applied=True)

    def _refuse_completed_or_live(self) -> None:
        t = self.task
        if t.status == TaskStatus.COMPLETED:
            raise Conflict(
                "task_already_completed", "the task is COMPLETED", task_id=t.task_id
            )
        if self.live:
            raise Conflict(
                "task_already_running",
                "an execution holds a live claim on the task",
                task_id=t.task_id,
            )

    # -- observations (from registration) ------------------------------------

    async def observe_complete(self) -> TransitionOutcome:
        """The target exists: COMPLETED, unless a live claim holds the task
        (its holder reports). A lapsed claim is closed first."""
        t = self.task
        if t.status == TaskStatus.COMPLETED or self.live:
            return self.outcome(applied=False)
        if t.status == TaskStatus.RUNNING:
            await self.close_claim(ClaimOutcome.LAPSED)
        self.move(TaskStatus.COMPLETED)
        t.completed_at = self.now
        t.error_message = None
        observed_at = self.transition.observed_at
        await self.record(
            EventType.TASK_OBSERVED_COMPLETE,
            metadata={
                "observed_at": None if observed_at is None else observed_at.isoformat()
            },
        )
        await self.session.flush()
        return self.outcome(applied=True)

    async def invalidate(self) -> TransitionOutcome:
        """The target is missing: COMPLETED → PENDING, only if the completion
        on record precedes the observation (S31)."""
        t, observed_at = self.task, self.transition.observed_at
        assert observed_at is not None
        if t.status != TaskStatus.COMPLETED:
            return self.outcome(applied=False)
        if t.completed_at is not None and t.completed_at >= observed_at:
            return self.outcome(applied=False)
        self.move(TaskStatus.PENDING)
        t.completed_at = None
        await self.record(
            EventType.TASK_INVALIDATED,
            metadata={
                "reason": "target_missing",
                "observed_at": observed_at.isoformat(),
                "plan_id": str(self.plan_id) if self.plan_id else None,
            },
        )
        await self.session.flush()
        return self.outcome(applied=True)
