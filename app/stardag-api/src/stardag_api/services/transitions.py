"""``transition_task()``: the one writer of task events.

Every move of ``task.status``, every claim grant, renewal and release, and
every execution-ledger write goes through here (engineering rule 2). See
design.md, "The runnable rule", "Claim × plan invariants" and the
``execution`` entity.

Every transition first locks the ``task`` row ``FOR NO KEY UPDATE`` — not
``FOR UPDATE``, which would conflict with the ``FOR KEY SHARE`` every
insert referencing the task takes through its foreign key (STA-51). A
claiming start takes its build row ``FOR SHARE`` before that (lock order
build → task), so a terminal build transition or a delete — ``FOR NO KEY
UPDATE`` on the build — is a synchronisation point for claims. The rules,
in one place:

- **The claim.** Live when ``status = RUNNING AND claim_expires_at > now``.
  A claiming start names its plan and a client-minted execution id; the same
  execution retrying a granted start is a no-op, checked *before* the plan
  (S36); otherwise COMPLETED is 409 ``task_already_completed``, a live claim
  409 ``task_already_running``, any other status outside ACTIONABLE (FAILED:
  the fail mode decides, through ``retry``) 409 ``task_not_actionable``, an
  inactive plan 409 ``plan_superseded``, a
  build that is not RUNNING 409 ``build_not_running``, and
  an upstream not COMPLETED — re-read under a share lock, so an invalidation
  in flight is waited for — 409 ``upstream_incomplete`` (S39). A lapsed claim
  is taken over (``claim_outcome = taken_over``, S21).
- **Authority.** A report changes status when it names the task's current
  execution (``task.execution_id``) whose claim has not been released —
  **whether or not the claim has lapsed**: a lapsed claim still names its
  execution until a claiming start takes it over, and a worker finishing
  seconds after expiry must not have a real completion discarded. Only
  once the claim is released (taken over, closed by an observation, or
  released by a build transition — ``execution.claim_released_at`` set) is
  a report *late*: it writes that execution's ledger end, is recorded with
  ``report_applied = false`` and refused (S19). One terminal report per
  execution (S35). A report — and the holder's self-report start — on the
  current execution comes through the plan the claim was granted through
  (``task.claim_plan_id``); under any other plan it is 409
  ``not_claim_holder``, with no trace.
- **The ledger's two ends.** Every move off RUNNING closes the current
  execution's claim (``claim_released_at``/``claim_outcome``, the server's
  end); ``ended_at``/``outcome`` are written only by the execution's own
  report.
"""

from __future__ import annotations

from datetime import datetime
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
    Plan,
    PlanMember,
    Task,
    TaskInstance,
    TaskInstanceDependency,
    TaskStatus,
)
from stardag_api.models.base import utc_now
from stardag_api.services import event_log
from stardag_api.services.claim_limits import full_limits, replace_limit_keys
from stardag_api.services.errors import (
    BadRequest,
    Conflict,
    NotFound,
    RecordedConflict,
)
from stardag_api.services.transition_types import (
    ACTIONABLE_STATUSES,
    DEFAULT_CLAIM_TTL_SECONDS,
    MAX_CLAIM_TTL_SECONDS,
    REPORTS,
    Transition,
    TransitionKind,
    TransitionOutcome,
    claim_ttl,
)
from stardag_api.services.tx import transaction
from stardag_api.services.wakeups import flag_after_transition

__all__ = [
    "ACTIONABLE_STATUSES",
    "DEFAULT_CLAIM_TTL_SECONDS",
    "MAX_CLAIM_TTL_SECONDS",
    "Transition",
    "TransitionKind",
    "TransitionOutcome",
    "apply_member_transition",
    "renew_claim",
    "transition_task",
]

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
    the session. A transition that changes the status flags the other
    builds it is news for (``wakeups.flag_after_transition``), here, so no
    path that moves a status can forget to.
    """
    if transition.kind is TransitionKind.START and transition.claim:
        assert plan_id is not None, "a claiming start names its plan"
        await _share_build(session, plan_id)
    task = await session.scalar(
        select(Task)
        .where(Task.environment_id == environment_id, Task.id == task_pk)
        .with_for_update(key_share=True)
        .execution_options(populate_existing=True)
    )
    if task is None:
        raise NotFound("unknown_task", f"no task {task_pk}")
    step = _Step(session, environment_id, task, plan_id, transition, now)
    previous = task.status
    outcome = await _dispatch(step)
    if task.status != previous:
        await flag_after_transition(
            session,
            environment_id,
            task.id,
            previous=previous,
            current=task.status,
            source_build_id=await step.build_id(),
            now=now,
        )
    return outcome


async def _share_build(session: AsyncSession, plan_id: UUID) -> None:
    """A claiming start takes its build row ``FOR SHARE`` before the task
    row. Terminal transitions and deletes hold the build ``FOR NO KEY
    UPDATE`` while they release the build's claims or check for live work,
    so a claim waits for them and then reads the build's new status
    (``build_not_running``): their snapshot of the build's claims is then
    the whole set. Claims do not wait for each other."""
    build_id = await session.scalar(select(Plan.build_id).where(Plan.id == plan_id))
    locked = (
        None
        if build_id is None
        else await session.scalar(
            select(Build.id).where(Build.id == build_id).with_for_update(read=True)
        )
    )
    if locked is None:
        raise Conflict(
            "build_not_running",
            "the plan's build no longer exists; it hands out no more work",
            build_id=str(build_id) if build_id else None,
            build_status=None,
        )


async def _dispatch(step: _Step) -> TransitionOutcome:
    kind = step.transition.kind
    if kind is TransitionKind.START:
        return await (
            step.claim() if step.transition.claim else step.self_report_start()
        )
    if kind in REPORTS:
        return await step.report()
    if kind is TransitionKind.RETRY:
        return await step.retry()
    if kind is TransitionKind.RENEW:
        return await step.renew()
    if kind is TransitionKind.OBSERVE_COMPLETE:
        return await step.observe_complete()
    if kind is TransitionKind.INVALIDATE:
        return await step.invalidate()
    if kind is TransitionKind.RELEASE:
        return await step.release()
    raise AssertionError(kind)  # pragma: no cover


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
        # A lapsed RUNNING claim is taken over below; anything else outside
        # ACTIONABLE (FAILED) is not a claiming start's to decide.
        if t.status != TaskStatus.RUNNING and t.status not in ACTIONABLE_STATUSES:
            raise Conflict(
                "task_not_actionable",
                f"the task is {t.status.value.upper()}; it is started again"
                " only after a retry",
                task_id=t.task_id,
                status=t.status.value,
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
        # A plain read under the build row's share lock, which
        # transition_task() took before the task row (build → task order):
        # a terminal transition or a delete in flight was waited for, so
        # its status is the one read here, and a claim it has not seen
        # cannot be granted behind it (design.md, "The runnable rule").
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
        full = await full_limits(
            self.session,
            self.environment_id,
            t.id,
            self.transition.limit_keys,
            now=self.now,
        )
        if full:
            # Record the keys the claim asked for, so the release of a slot
            # on them wakes this task's builds; the task holds no slot (its
            # claim is not live), so nothing is occupied.
            await replace_limit_keys(
                self.session,
                self.environment_id,
                t.id,
                self.transition.limit_keys,
                now=self.now,
            )
            raise RecordedConflict(
                "concurrency_limit_reached",
                "a concurrency limit on the claim's keys is full",
                task_id=t.task_id,
                keys=full,
            )

        ttl = claim_ttl(self.transition.claim_ttl_seconds)
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
        await replace_limit_keys(
            self.session,
            self.environment_id,
            t.id,
            self.transition.limit_keys,
            now=self.now,
        )
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
        if t.execution_id == eid and execution.claim_released_at is None:
            # The current, unreleased execution — its claim lapsed or not —
            # under the wrong plan is a trace-free refusal, decided before
            # the live-claim one (as a report decides it).
            self.check_claim_plan(eid)
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

    def check_claim_plan(self, eid: UUID) -> None:
        """A holder's report comes through the plan its claim was granted
        through: a report naming the current execution under another plan
        (``task.claim_plan_id`` differs from the route's) is 409
        ``not_claim_holder``, and leaves no trace — the execution may still
        report its end through its own plan."""
        t = self.task
        if t.claim_plan_id != self.plan_id:
            raise Conflict(
                "not_claim_holder",
                "the task's claim is held through another plan; its execution"
                " reports through that plan",
                task_id=t.task_id,
                execution_id=str(eid),
                plan_id=str(self.plan_id) if self.plan_id else None,
                claim_plan_id=str(t.claim_plan_id) if t.claim_plan_id else None,
            )

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
        event_type, status, claim_outcome, outcome = REPORTS[self.transition.kind]
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
        current = t.execution_id == eid and execution.claim_released_at is None
        if current:
            # Before the ledger end: a report under the wrong plan must not
            # spend the execution's one terminal report.
            self.check_claim_plan(eid)
        # The execution's own end, whether or not it may still move the task.
        execution.ended_at = self.now
        execution.outcome = outcome
        # Current and not yet released — a lapsed claim included: it names
        # this execution until a claiming start takes it over.
        if not current:
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
