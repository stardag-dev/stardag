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

from sqlalchemy import select
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
from stardag_api.services.claim_limits import full_limits, replace_limit_keys
from stardag_api.services.errors import Conflict, NotFound, RecordedConflict
from stardag_api.services.transition_reports import ReportSteps
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
    "lock_task",
    "member_task_pk",
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
        task_pk = await member_task_pk(session, environment_id, plan_id, task_id)
        return await transition_task(
            session,
            environment_id,
            task_pk=task_pk,
            plan_id=plan_id,
            transition=transition,
            now=utc_now(),
        )


async def member_task_pk(
    session: AsyncSession, environment_id: UUID, plan_id: UUID, task_id: str
) -> UUID:
    """The task pk of ``task_id`` as a member of ``plan_id`` (404
    ``not_a_member`` otherwise)."""
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
    return task_pk


async def lock_task(session: AsyncSession, environment_id: UUID, task_pk: UUID) -> Task:
    """The task row, locked ``FOR NO KEY UPDATE`` (never ``FOR UPDATE``: that
    would conflict with the FK key-share locks of every insert referencing
    it, STA-51) and re-read."""
    task = await session.scalar(
        select(Task)
        .where(Task.environment_id == environment_id, Task.id == task_pk)
        .with_for_update(key_share=True)
        .execution_options(populate_existing=True)
    )
    if task is None:
        raise NotFound("unknown_task", f"no task {task_pk}")
    return task


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

    Locks the task row first, then reads the clock. Raises :class:`Conflict` for a refusal that
    leaves no trace, :class:`RecordedConflict` for one whose record is in
    the session. A transition that changes the status flags the other
    builds it is news for (``wakeups.flag_after_transition``), here, so no
    path that moves a status can forget to.
    """
    task = await lock_task(session, environment_id, task_pk)
    # Stamped after the lock: every timestamp the transition writes is when
    # it took effect, not when the caller began waiting (the observed_at
    # guard compares against the real completion time). ``now`` orders the
    # transaction's events.
    stamp = max(now, utc_now())
    step = _Step(session, environment_id, task, plan_id, transition, stamp, now)
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


async def _dispatch(step: _Step) -> TransitionOutcome:
    kind = step.transition.kind
    if kind is TransitionKind.START:
        return await (
            step.claim() if step.transition.claim else step.self_report_start()
        )
    if kind in REPORTS:
        return await step.report()
    if kind is TransitionKind.PREEMPT:
        return await step.preempt()
    if kind is TransitionKind.RETRY:
        return await step.retry()
    if kind is TransitionKind.SKIP:
        return await step.skip()
    if kind is TransitionKind.CANCEL:
        return await step.cancel()
    if kind is TransitionKind.STOP:
        return await step.stop()
    if kind is TransitionKind.RENEW:
        return await step.renew()
    if kind is TransitionKind.OBSERVE_COMPLETE:
        return await step.observe_complete()
    if kind is TransitionKind.INVALIDATE:
        return await step.invalidate()
    if kind is TransitionKind.RELEASE:
        return await step.release()
    raise AssertionError(kind)  # pragma: no cover


class _Step(ReportSteps):
    """One transition on one locked task row: the claim and the
    non-claiming start here; reports in ``transition_reports.py``; shared
    state in ``transition_step.py``."""

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
        with the executor details the claim could not know. The authority
        rule of every report: applied when it names the task's current
        execution whose claim has not been released, lapsed or not; late
        (recorded, refused) only after a takeover, an observation or a
        build released the claim.

        After a preemption this is the restart arriving: the claim gets a
        fresh TTL (``claim_ttl_seconds``, or the default) and
        ``preempted_at`` is cleared. Otherwise the expiry is unchanged."""
        t, eid = self.task, self.execution_id()
        execution = await self.named_execution(EventType.TASK_STARTED, eid)
        if (
            t.execution_id != eid
            or execution.claim_released_at is not None
            or execution.ended_at is not None
        ):
            await self.record(
                EventType.TASK_STARTED,
                execution_id=eid,
                report_applied=False,
                metadata={"claim": False},
            )
            raise RecordedConflict(
                "execution_not_current",
                "the execution's claim has been released (taken over, closed"
                " or released by its build), or it has ended",
                execution_id=str(eid),
            )
        self.check_claim_plan(eid)
        for column in ("executor", "executor_ref", "executor_metadata"):
            value = getattr(self.transition, column)
            if value is not None:
                setattr(execution, column, value)
        metadata: dict[str, Any] = {"claim": False}
        if t.preempted_at is not None:
            expires_at = self.now + claim_ttl(self.transition.claim_ttl_seconds)
            t.claim_expires_at = expires_at
            t.preempted_at = None
            metadata["restart"] = True
            metadata["claim_expires_at"] = expires_at.isoformat()
        await self.record(EventType.TASK_STARTED, execution_id=eid, metadata=metadata)
        await self.session.flush()
        return self.outcome(applied=True)
