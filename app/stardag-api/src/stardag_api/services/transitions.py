"""``transition_task()``: the one writer of task events.

Every move of ``task.status``, every claim grant, renewal and release, and
every execution-ledger write goes through here (engineering rule 2). See
design.md, "The runnable rule", "Claim × plan invariants" and the
``execution`` entity.

Every transition first locks the ``task`` row ``FOR NO KEY UPDATE`` — not
``FOR UPDATE``, which would conflict with the ``FOR KEY SHARE`` every
insert referencing the task takes through its foreign key (STA-51). The
rules, in one place:

- **The claim.** Live when ``status = RUNNING AND claim_expires_at > now``.
  A claiming start names its plan and a client-minted execution id; the same
  execution retrying a granted start is a no-op, checked *before* the plan
  (S36); otherwise COMPLETED is 409 ``task_already_completed``, a live claim
  409 ``task_already_running``, an inactive plan 409 ``plan_superseded``, a
  build that is not RUNNING 409 ``build_not_running``, and
  an upstream not COMPLETED — re-read under a share lock, so an invalidation
  in flight is waited for — 409 ``upstream_incomplete`` (S39). A lapsed claim
  is taken over (``claim_outcome = taken_over``, S21).
- **Authority.** A report changes status only if it names the task's current
  execution and the claim is live. Otherwise it writes that execution's
  ledger end, is recorded with ``report_applied = false`` and refused
  (S19). One terminal report per execution (S35).
- **The ledger's two ends.** Every move off RUNNING closes the current
  execution's claim (``claim_released_at``/``claim_outcome``, the server's
  end); ``ended_at``/``outcome`` are written only by the execution's own
  report.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.models import (
    Build,
    BuildStatus,
    ClaimOutcome,
    EventType,
    Execution,
    ExecutionOutcome,
    Plan,
    PlanMember,
    Task,
    TaskInstance,
    TaskInstanceDependency,
    TaskStatus,
)
from stardag_api.models.base import utc_now
from stardag_api.services import event_log
from stardag_api.services.errors import (
    BadRequest,
    Conflict,
    NotFound,
    RecordedConflict,
)
from stardag_api.services.tx import transaction

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
    """What a transition did. ``applied`` is False for a no-op."""

    applied: bool
    status: TaskStatus
    execution_id: UUID | None
    claim_expires_at: datetime | None


# A report's event type, the status it moves the task to, and the two ledger
# outcomes it writes.
_REPORTS: dict[
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
}


# ---------------------------------------------------------------------------
# Route-facing calls: one transaction each
# ---------------------------------------------------------------------------


async def apply_member_transition(
    session: AsyncSession,
    environment_id: UUID,
    *,
    plan_id: UUID,
    task_id: str,
    transition: Transition,
) -> TransitionOutcome:
    """A plan member's start/complete/fail/suspend/retry, as one transaction.

    A recorded refusal (:class:`RecordedConflict`) is committed, then raised.
    """
    async with transaction(session):
        task_pk = await session.scalar(
            select(PlanMember.task_pk)
            .join(Task, Task.id == PlanMember.task_pk)
            .where(
                PlanMember.environment_id == environment_id,
                PlanMember.plan_id == plan_id,
                Task.task_id == task_id,
            )
        )
        if task_pk is None:
            raise NotFound(
                "not_a_member",
                f"task {task_id} is not a member of plan {plan_id}",
                task_id=task_id,
                plan_id=str(plan_id),
            )
        return await transition_task(
            session,
            environment_id,
            task_pk=task_pk,
            plan_id=plan_id,
            transition=transition,
            now=utc_now(),
        )


async def renew_claim(
    session: AsyncSession,
    environment_id: UUID,
    *,
    task_id: str,
    execution_id: UUID,
    claim_ttl_seconds: int | None = None,
) -> TransitionOutcome:
    """Extend a live claim, for its holder only (409 ``claim_not_held``)."""
    async with transaction(session):
        task_pk = await _task_pk(session, environment_id, task_id)
        return await transition_task(
            session,
            environment_id,
            task_pk=task_pk,
            plan_id=None,
            transition=Transition.renew(execution_id, claim_ttl_seconds),
            now=utc_now(),
        )


async def _task_pk(session: AsyncSession, environment_id: UUID, task_id: str) -> UUID:
    task_pk = await session.scalar(
        select(Task.id).where(
            Task.environment_id == environment_id, Task.task_id == task_id
        )
    )
    if task_pk is None:
        raise NotFound("unknown_task", f"no task {task_id}", task_id=task_id)
    return task_pk


# ---------------------------------------------------------------------------
# transition_task
# ---------------------------------------------------------------------------


async def transition_task(
    session: AsyncSession,
    environment_id: UUID,
    *,
    task_pk: UUID,
    plan_id: UUID | None,
    transition: Transition,
    now: datetime,
) -> TransitionOutcome:
    """Apply one transition inside the caller's transaction (no commit).

    Locks the task row first. Raises :class:`Conflict` for a refusal that
    leaves no trace, :class:`RecordedConflict` for one whose record is in
    the session.
    """
    task = await session.scalar(
        select(Task)
        .where(Task.environment_id == environment_id, Task.id == task_pk)
        .with_for_update(key_share=True)
        .execution_options(populate_existing=True)
    )
    if task is None:
        raise NotFound("unknown_task", f"no task {task_pk}")
    step = _Step(session, environment_id, task, plan_id, transition, now)
    kind = transition.kind
    if kind is TransitionKind.START:
        return await (step.claim() if transition.claim else step.self_report_start())
    if kind in _REPORTS:
        return await step.report()
    if kind is TransitionKind.RETRY:
        return await step.retry()
    if kind is TransitionKind.RENEW:
        return await step.renew()
    if kind is TransitionKind.OBSERVE_COMPLETE:
        return await step.observe_complete()
    if kind is TransitionKind.INVALIDATE:
        return await step.invalidate()
    raise AssertionError(kind)  # pragma: no cover


def _ttl(requested: int | None) -> timedelta:
    seconds = DEFAULT_CLAIM_TTL_SECONDS if requested is None else requested
    if not 0 < seconds <= MAX_CLAIM_TTL_SECONDS:
        raise BadRequest(
            "invalid_claim_ttl",
            f"claim_ttl_seconds must be in (0, {MAX_CLAIM_TTL_SECONDS}]",
            claim_ttl_seconds=seconds,
        )
    return timedelta(seconds=seconds)


class _Step:
    """One transition on one locked task row."""

    def __init__(
        self,
        session: AsyncSession,
        environment_id: UUID,
        task: Task,
        plan_id: UUID | None,
        transition: Transition,
        now: datetime,
    ) -> None:
        self.session = session
        self.environment_id = environment_id
        self.task = task
        self.plan_id = plan_id
        self.transition = transition
        self.now = now

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
                    at=self.now,
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

    # -- start -----------------------------------------------------------------

    async def claim(self) -> TransitionOutcome:
        t, eid = self.task, self.execution_id()
        assert self.plan_id is not None, "a claiming start names its plan"
        # The same execution retrying a granted start: a no-op, decided
        # before the plan check (S36).
        if t.execution_id == eid and self.live:
            return self.outcome(applied=False)
        if t.status == TaskStatus.COMPLETED:
            raise Conflict(
                "task_already_completed", "the task is COMPLETED", task_id=t.task_id
            )
        if self.live:
            raise Conflict(
                "task_already_running",
                "another execution holds a live claim on the task",
                task_id=t.task_id,
            )
        if await self.session.get(Execution, eid) is not None:
            raise Conflict(
                "execution_superseded",
                "this execution's claim has ended; a new claim needs a new"
                " execution id",
                execution_id=str(eid),
            )
        plan = await self.session.get(Plan, self.plan_id)
        assert plan is not None
        if plan.activated_at is None or plan.superseded_at is not None:
            raise Conflict(
                "plan_superseded",
                "the plan is not the build's active plan",
                plan_id=str(plan.id),
            )
        # A plain read, not a lock: the build row is locked before task rows
        # elsewhere (plan creation observing its roots), so locking it here,
        # after the task row, could deadlock. A start racing the build's end
        # is left to that end, which releases the claims held by the build's
        # plans (design.md, "The runnable rule"; the build lifecycle routes).
        build_status = await self.session.scalar(
            select(Build.status).where(Build.id == plan.build_id)
        )
        if build_status != BuildStatus.RUNNING:
            raise Conflict(
                "build_not_running",
                "the plan's build is not RUNNING; it hands out no more work",
                build_id=str(plan.build_id),
                build_status=build_status.value if build_status else None,
            )
        member = await self.session.scalar(
            select(PlanMember).where(
                PlanMember.plan_id == plan.id, PlanMember.task_pk == t.id
            )
        )
        assert member is not None
        if member.excluded_at is not None:
            raise Conflict(
                "member_excluded", "the member is excluded", task_id=t.task_id
            )
        await self._check_upstreams(member.instance_id)

        ttl = _ttl(self.transition.claim_ttl_seconds)
        taken_over = None
        if t.status == TaskStatus.RUNNING:  # a lapsed claim, taken over
            taken_over = t.execution_id
            await self.release_ledger(ClaimOutcome.TAKEN_OVER)
        self.session.add(
            Execution(
                id=eid,
                environment_id=self.environment_id,
                task_pk=t.id,
                plan_id=plan.id,
                instance_id=member.instance_id,
                executor=self.transition.executor,
                executor_ref=self.transition.executor_ref,
                executor_metadata=self.transition.executor_metadata,
                started_at=self.now,
                created_at=self.now,
            )
        )
        await self.session.flush()
        self.move(TaskStatus.RUNNING)
        t.started_at = self.now
        t.claim_plan_id = plan.id
        t.execution_id = eid
        expires_at = self.now + ttl
        t.claim_expires_at = expires_at
        t.error_message = None
        t.preempted_at = None
        await self.record(
            EventType.TASK_STARTED,
            execution_id=eid,
            metadata={
                "claim": True,
                "claim_expires_at": expires_at.isoformat(),
                **({"taken_over": str(taken_over)} if taken_over else {}),
            },
        )
        await self.session.flush()
        return self.outcome(applied=True)

    async def _check_upstreams(self, instance_id: UUID) -> None:
        """The runnable predicate, re-read under the claim's lock: the
        instance is expanded and every upstream task COMPLETED. Upstream rows
        are read ``FOR SHARE``, in ``task_id`` order, so an invalidation
        holding one (``FOR NO KEY UPDATE``) is waited for, not raced (S39)."""
        expanded = await self.session.scalar(
            select(TaskInstance.expanded_at).where(TaskInstance.id == instance_id)
        )
        if expanded is None:
            raise Conflict(
                "upstream_incomplete",
                "the member's instance is not expanded; its upstreams are unknown",
                task_id=self.task.task_id,
                reason="not_expanded",
            )
        upstream = TaskInstance.__table__.alias("upstream")
        rows = (
            await self.session.execute(
                select(Task.task_id, Task.status)
                .select_from(TaskInstanceDependency)
                .join(
                    upstream,
                    upstream.c.id == TaskInstanceDependency.upstream_instance_id,
                )
                .join(Task, Task.id == upstream.c.task_pk)
                .where(TaskInstanceDependency.downstream_instance_id == instance_id)
                .order_by(Task.task_id)
                .with_for_update(read=True, of=Task)
            )
        ).all()
        incomplete = [tid for tid, status in rows if status != TaskStatus.COMPLETED]
        if incomplete:
            raise Conflict(
                "upstream_incomplete",
                "an upstream task is not COMPLETED",
                task_id=self.task.task_id,
                upstream_task_ids=incomplete,
            )

    async def self_report_start(self) -> TransitionOutcome:
        """A non-claiming start: the claim holder reporting that it runs,
        with the executor details the claim could not know."""
        t, eid = self.task, self.execution_id()
        execution = await self._execution(EventType.TASK_STARTED, eid)
        if t.execution_id != eid or not self.live or execution.ended_at is not None:
            await self.record(
                EventType.TASK_STARTED,
                execution_id=eid,
                report_applied=False,
                metadata={"claim": False},
            )
            raise RecordedConflict(
                "execution_not_current",
                "the execution does not hold the task's live claim",
                execution_id=str(eid),
            )
        for column in ("executor", "executor_ref", "executor_metadata"):
            value = getattr(self.transition, column)
            if value is not None:
                setattr(execution, column, value)
        await self.record(
            EventType.TASK_STARTED, execution_id=eid, metadata={"claim": False}
        )
        await self.session.flush()
        return self.outcome(applied=True)

    async def _execution(self, event_type: EventType, eid: UUID) -> Execution:
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

    # -- reports ---------------------------------------------------------------

    async def report(self) -> TransitionOutcome:
        """complete / fail / suspend, under the authority rule."""
        t, eid = self.task, self.execution_id()
        event_type, status, claim_outcome, outcome = _REPORTS[self.transition.kind]
        execution = await self._execution(event_type, eid)
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
        if t.execution_id != eid or not self.live:
            await self.record(
                event_type, execution_id=eid, report_applied=False, error_message=error
            )
            await self.session.flush()
            raise RecordedConflict(
                "execution_not_current",
                "the execution does not hold the task's live claim; its end is"
                " recorded and the task is unchanged",
                execution_id=str(eid),
            )
        await self.close_claim(claim_outcome)
        self.move(status)
        if status == TaskStatus.COMPLETED:
            t.completed_at = self.now
            t.error_message = None
        elif status == TaskStatus.FAILED:
            t.error_message = error
        await self.record(event_type, execution_id=eid, error_message=error)
        await self.session.flush()
        return self.outcome(applied=True)

    async def retry(self) -> TransitionOutcome:
        """Reset to PENDING (fail mode's retry, or an operator's). Idempotent
        by state; refused on COMPLETED and on a live claim."""
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
        if t.status == TaskStatus.PENDING:
            return self.outcome(applied=False)
        if t.status == TaskStatus.RUNNING:  # a lapsed claim
            await self.close_claim(ClaimOutcome.LAPSED)
        self.move(TaskStatus.PENDING)
        t.error_message = None
        await self.record(EventType.TASK_RETRIED)
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
        t.claim_expires_at = self.now + _ttl(self.transition.claim_ttl_seconds)
        await self.session.flush()
        return self.outcome(applied=True)

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
