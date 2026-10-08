"""The member transitions of :class:`~stardag.testing.InMemoryRegistry` —
the server's transition services, in memory (design.md, "Claim × plan
invariants"). Split from :mod:`._registry_plans`, which builds on it."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from typing import Any
from uuid import UUID

from stardag.registry import (
    TransitionResult,
)
from stardag.testing._registry_state import (
    DEFAULT_CLAIM_TTL,
    MAX_CLAIM_TTL,
    PREEMPT_GRACE,
    Event,
    ExecutionRow,
    InstanceRow,
    RegistryState,
    TaskRow,
    refuse,
)


def _outcome(task: TaskRow, applied: bool = True) -> TransitionResult:
    return TransitionResult(
        applied=applied,
        status=task.status,
        execution_id=task.execution_id,
        claim_expires_at=task.claim_expires_at,
    )


class TransitionsMixin(RegistryState):
    """Starts, reports, retries, cancels, skips and renewals of a plan's
    members, over the shared :class:`RegistryState`."""

    def _blocked(self, instance: InstanceRow) -> bool:
        return any(
            self.tasks[self.instances[u].task_id].status != "completed"
            for u in instance.upstreams
        )

    # -- transitions --------------------------------------------------------------------

    def _ttl(self, requested: int | None) -> timedelta:
        seconds = DEFAULT_CLAIM_TTL if requested is None else requested
        if not 0 < seconds <= MAX_CLAIM_TTL:
            raise refuse("invalid_claim_ttl", status=400)
        return timedelta(seconds=seconds)

    def member_start(
        self,
        plan_id: UUID,
        task_id: str,
        *,
        execution_id: UUID,
        claim: bool = True,
        claim_ttl_seconds: int | None = None,
        executor: str | None = None,
        executor_ref: str | None = None,
        executor_metadata: dict[str, Any] | None = None,
        limit_keys: Sequence[str] = (),
    ) -> TransitionResult:
        self._record(
            "member_start",
            plan_id=plan_id,
            task_id=task_id,
            execution_id=execution_id,
            claim=claim,
            claim_ttl_seconds=claim_ttl_seconds,
            executor=executor,
            executor_ref=executor_ref,
            executor_metadata=executor_metadata,
            limit_keys=list(limit_keys),
        )
        with self.transaction(
            keep=lambda e: getattr(e, "code", None) == "concurrency_limit_reached"
        ):
            plan = self.plan(plan_id)
            member = self.member(plan_id, task_id)
            task = self.task(task_id)
            if not claim:
                execution = self.executions.get(execution_id)
                if execution is None or execution.task_id != task_id:
                    raise refuse("unknown_execution")
                if (
                    task.execution_id != execution_id
                    or execution.claim_released_at is not None
                    or execution.ended_at is not None
                ):
                    raise refuse("execution_not_current")
                self._check_claim_plan(task, plan_id)
                if task.preempted_at is not None:
                    # The restart of a preempted execution: a fresh TTL.
                    task.claim_expires_at = self.now() + self._ttl(claim_ttl_seconds)
                    task.preempted_at = None
                for name, value in (
                    ("executor", executor),
                    ("executor_ref", executor_ref),
                    ("executor_metadata", executor_metadata),
                ):
                    if value is not None:
                        setattr(execution, name, value)
                return _outcome(task)
            if task.execution_id == execution_id and self.live(task):
                return _outcome(task, applied=False)
            if task.status == "completed":
                raise refuse("task_already_completed")
            if self.live(task):
                raise refuse("task_already_running")
            if execution_id in self.executions:
                raise refuse("execution_superseded")
            if not plan.active:
                raise refuse("plan_superseded")
            build_status = self.builds[plan.build_id].status
            if build_status != "running":
                raise refuse("build_not_running", build_status=build_status)
            if member.excluded_reason is not None:
                raise refuse("member_excluded")
            instance = self.instances[member.instance_id]
            if not instance.expanded:
                raise refuse("upstream_incomplete", reason="not_expanded")
            if self._blocked(instance):
                raise refuse("upstream_incomplete")
            full = [
                key
                for key in sorted(set(limit_keys))
                if key in self.limits
                and sum(
                    1
                    for other in self.tasks.values()
                    if key in other.limit_keys and self.live(other)
                )
                >= self.limits[key]
            ]
            task.limit_keys = set(limit_keys)
            if full:
                raise refuse("concurrency_limit_reached", keys=full)
            ttl = self._ttl(claim_ttl_seconds)
            if task.status == "running":
                self.close_claim(task, "taken_over")
            now = self.now()
            self.executions[execution_id] = ExecutionRow(
                execution_id,
                task_id,
                plan_id,
                instance.id,
                now,
                executor=executor,
                executor_ref=executor_ref,
                executor_metadata=executor_metadata,
            )
            task.claim_plan_id = plan_id
            task.execution_id = execution_id
            task.claim_expires_at = now + ttl
            task.error_message = None
            self.move(task, "running", flag_except=plan.build_id)
            self.log(
                Event("TASK_STARTED", task_id, plan.build_id, plan_id, execution_id)
            )
            return _outcome(task)

    def _report(
        self,
        plan_id: UUID,
        task_id: str,
        execution_id: UUID,
        *,
        status: str,
        outcome: str,
        error_message: str | None = None,
    ) -> TransitionResult:
        plan = self.plan(plan_id)
        task = self.task(task_id)
        execution = self.executions.get(execution_id)
        if execution is None or execution.task_id != task_id:
            raise refuse("unknown_execution")
        if execution.ended_at is not None:
            raise refuse("execution_already_ended")
        if task.execution_id == execution_id and execution.claim_released_at is None:
            self._check_claim_plan(task, plan_id)
        execution.ended_at = self.now()
        execution.outcome = outcome
        if task.execution_id != execution_id or execution.claim_released_at is not None:
            self.log(
                Event(
                    outcome.upper(),
                    task_id,
                    plan.build_id,
                    plan_id,
                    execution_id,
                    applied=False,
                    error_message=error_message,
                )
            )
            raise refuse("execution_not_current")
        self.close_claim(task, outcome)
        self.move(task, status, flag_except=plan.build_id)
        if status == "completed":
            task.completed_at = self.now()
            task.error_message = None
        elif status in ("failed", "interrupted"):
            # As the server: assigned unconditionally, so a previous
            # failure's text never explains this one.
            task.error_message = error_message
        self.log(
            Event(
                f"TASK_{status.upper()}",
                task_id,
                plan.build_id,
                plan_id,
                execution_id,
                error_message=error_message,
            )
        )
        return _outcome(task)

    def _check_claim_plan(
        self, task: TaskRow, plan_id: UUID, *, build_level: bool = False
    ) -> None:
        """A holder's report comes through the plan its claim was granted
        through (409 ``not_claim_holder``, leaving no trace); a cancel, through
        any plan of the build holding it."""
        if task.claim_plan_id == plan_id:
            return
        if (
            build_level
            and task.claim_plan_id is not None
            and self.plans[task.claim_plan_id].build_id == self.plans[plan_id].build_id
        ):
            return
        raise refuse("not_claim_holder")

    def _reporting(self):
        # A late report's ledger end is recorded before the refusal.
        return self.transaction(
            keep=lambda e: getattr(e, "code", None) == "execution_not_current"
        )

    def member_complete(
        self, plan_id: UUID, task_id: str, *, execution_id: UUID
    ) -> TransitionResult:
        self._record(
            "member_complete",
            plan_id=plan_id,
            task_id=task_id,
            execution_id=execution_id,
        )
        with self._reporting():
            return self._report(
                plan_id, task_id, execution_id, status="completed", outcome="completed"
            )

    def member_fail(
        self,
        plan_id: UUID,
        task_id: str,
        *,
        execution_id: UUID,
        error_message: str | None = None,
    ) -> TransitionResult:
        self._record(
            "member_fail",
            plan_id=plan_id,
            task_id=task_id,
            execution_id=execution_id,
            error_message=error_message,
        )
        with self._reporting():
            return self._report(
                plan_id,
                task_id,
                execution_id,
                status="failed",
                outcome="failed",
                error_message=error_message,
            )

    def member_interrupt(
        self,
        plan_id: UUID,
        task_id: str,
        *,
        execution_id: UUID,
        error_message: str | None = None,
    ) -> TransitionResult:
        self._record(
            "member_interrupt",
            plan_id=plan_id,
            task_id=task_id,
            execution_id=execution_id,
            error_message=error_message,
        )
        with self._reporting():
            return self._report(
                plan_id,
                task_id,
                execution_id,
                status="interrupted",
                outcome="interrupted",
                error_message=error_message,
            )

    def member_preempt(
        self, plan_id: UUID, task_id: str, *, execution_id: UUID
    ) -> TransitionResult:
        self._record(
            "member_preempt",
            plan_id=plan_id,
            task_id=task_id,
            execution_id=execution_id,
        )
        task = self.task(task_id)
        if task.execution_id != execution_id or not self.live(task):
            raise refuse("execution_not_current")
        self._check_claim_plan(task, plan_id)
        assert task.claim_expires_at is not None
        task.claim_expires_at = min(task.claim_expires_at, self.now() + PREEMPT_GRACE)
        task.preempted_at = self.now()
        return _outcome(task)

    def member_retry(self, plan_id: UUID, task_id: str) -> TransitionResult:
        self._record("member_retry", plan_id=plan_id, task_id=task_id)
        with self.transaction():
            self.member(plan_id, task_id)
            task = self.task(task_id)
            if task.status == "completed":
                raise refuse("task_already_completed")
            if self.live(task):
                raise refuse("task_already_running")
            if task.status == "pending":
                return _outcome(task, applied=False)
            if task.status == "running":
                self.close_claim(task, "lapsed")
            self.move(task, "pending")
            task.error_message = None
            return _outcome(task)

    def member_cancel(self, plan_id: UUID, task_id: str) -> TransitionResult:
        self._record("member_cancel", plan_id=plan_id, task_id=task_id)
        task = self.task(task_id)
        if task.status == "running" and self.live(task):
            self._check_claim_plan(task, plan_id, build_level=True)
            self.close_claim(task, "cancelled")
            self.move(task, "cancelled")
        return _outcome(task)

    def member_skip(self, plan_id: UUID, task_id: str) -> TransitionResult:
        self._record("member_skip", plan_id=plan_id, task_id=task_id)
        task = self.task(task_id)
        if task.status == "skipped":
            return _outcome(task, applied=False)
        if task.status == "completed":
            raise refuse("task_already_completed")
        if self.live(task):
            raise refuse("task_already_running")
        if task.status in ("failed", "cancelled"):
            raise refuse("task_not_skippable")
        if task.status == "running":
            self.close_claim(task, "lapsed")
        self.move(task, "skipped")
        return _outcome(task)

    def claim_renew(
        self,
        task_id: str,
        *,
        execution_id: UUID,
        claim_ttl_seconds: int | None = None,
    ) -> TransitionResult:
        self._record(
            "claim_renew",
            task_id=task_id,
            execution_id=execution_id,
            claim_ttl_seconds=claim_ttl_seconds,
        )
        task = self.task(task_id)
        if task.execution_id != execution_id or not self.live(task):
            execution = self.executions.get(execution_id)
            raise refuse(
                "claim_not_held",
                claim_outcome=execution.claim_outcome if execution else None,
            )
        task.claim_expires_at = self.now() + self._ttl(claim_ttl_seconds)
        task.preempted_at = None  # the holder is alive: no restart outstanding
        return _outcome(task)
