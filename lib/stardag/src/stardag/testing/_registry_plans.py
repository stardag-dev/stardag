"""Plans, registration, the frontier and the member transitions of
:class:`~stardag.testing.InMemoryRegistry` — the server's registration and
transition services, in memory (design.md, "Registration", "The runnable
rule", "Claim × plan invariants")."""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from datetime import timedelta
from typing import Any
from uuid import UUID

from stardag.build._registration import new_id
from stardag.registry import (
    BuildFrontier,
    FrontierMember,
    MembersResult,
    PlanInfo,
    RegistrationItem,
    TransitionResult,
)
from stardag.testing._registry_state import (
    ACTIONABLE,
    CLOCK_SKEW_TOLERANCE,
    DEFAULT_CLAIM_TTL,
    MAX_CLAIM_TTL,
    PREEMPT_GRACE,
    Event,
    ExecutionRow,
    InstanceRow,
    MemberRow,
    PlanRow,
    RegistryState,
    TaskRow,
    refuse,
)


def plan_info(plan: PlanRow, *, created: bool = False) -> PlanInfo:
    return PlanInfo(
        id=plan.id,
        build_id=plan.build_id,
        deployment_id=plan.deployment_id,
        settings_hash=plan.settings_hash,
        generation=plan.generation,
        activated_at=plan.activated_at,
        sealed_at=plan.sealed_at,
        superseded_at=plan.superseded_at,
        created=created,
    )


def _outcome(task: TaskRow, applied: bool = True) -> TransitionResult:
    return TransitionResult(
        applied=applied,
        status=task.status,
        execution_id=task.execution_id,
        claim_expires_at=task.claim_expires_at,
    )


class PlansMixin(RegistryState):
    """See the module docstring."""

    # -- registration -----------------------------------------------------------------

    def _register_items(
        self,
        plan: PlanRow,
        items: Sequence[RegistrationItem],
        *,
        as_roots: bool,
        dynamic: Collection[str] = (),
    ) -> MembersResult:
        now = self.now()
        counts = dict.fromkeys(MembersResult.model_fields, 0)
        by_hash: dict[str, RegistrationItem] = {}
        for item in items:
            if item.observed_at > now + CLOCK_SKEW_TOLERANCE:
                raise refuse(
                    "clock_skew", "observed_at is ahead of the registry", status=400
                )
            seen = by_hash.get(item.instance_hash)
            if seen is not None and (
                seen.body != item.body or seen.task_id != item.task_id
            ):
                raise refuse("instance_body_conflict", "two bodies under one hash")
            by_hash[item.instance_hash] = item
        ordered = sorted(by_hash.values(), key=lambda i: (i.task_id, i.instance_hash))
        members = self.members.setdefault(plan.id, {})
        scope = (plan.deployment_id, plan.settings_hash)
        instance_of: dict[str, UUID] = {}
        for item in ordered:
            task = self.tasks.get(item.task_id)
            identity = (
                item.task_namespace,
                item.task_name,
                item.version,
                item.output_uri,
            )
            if task is None:
                task = TaskRow(item.task_id, *identity, status_at=now)
                self.tasks[item.task_id] = task
                counts["tasks_created"] += 1
            elif (
                task.task_namespace,
                task.task_name,
                task.version,
                task.output_uri,
            ) != identity:
                raise refuse("task_identity_conflict", f"task {item.task_id}")
            key = (*scope, item.instance_hash)
            instance_id = self.instance_index.get(key)
            if instance_id is None:
                instance_id = new_id()
                self.instances[instance_id] = InstanceRow(
                    instance_id,
                    *scope,
                    item.instance_hash,
                    item.task_id,
                    dict(item.body),
                )
                self.instance_index[key] = instance_id
                counts["instances_created"] += 1
            elif self.instances[instance_id].body != item.body:
                raise refuse("instance_body_conflict", f"instance {item.instance_hash}")
            instance_of[item.instance_hash] = instance_id
        for item in ordered:
            instance = self.instances[instance_of[item.instance_hash]]
            if item.declared_upstreams is not None:
                for upstream_hash in item.declared_upstreams:
                    upstream_id = self.instance_index.get((*scope, upstream_hash))
                    if upstream_id is None:
                        raise refuse(
                            "unknown_upstream",
                            f"upstream {upstream_hash} is not an instance in this scope",
                            status=400,
                        )
                    if upstream_id not in instance.upstreams:
                        instance.upstreams[upstream_id] = False
                        counts["edges_created"] += 1
                    if self._admit(plan, self.instances[upstream_id], "closure"):
                        counts["closure_admitted"] += 1
                instance.expanded = True
            admitted_by = (
                "root"
                if as_roots
                else ("dynamic" if item.instance_hash in dynamic else "static")
            )
            if self._admit(plan, instance, admitted_by, is_root=as_roots):
                counts["members_admitted"] += 1
                self.events.append(
                    Event("TASK_PENDING", item.task_id, plan.build_id, plan.id)
                )
            elif as_roots:
                members[item.task_id].is_root = True
            self._observe(plan, self.tasks[item.task_id], item, counts)
        return MembersResult(**counts)

    def _admit(
        self,
        plan: PlanRow,
        instance: InstanceRow,
        admitted_by: str,
        *,
        is_root: bool = False,
    ) -> bool:
        members = self.members.setdefault(plan.id, {})
        member = members.get(instance.task_id)
        if member is None:
            members[instance.task_id] = MemberRow(
                instance.task_id, instance.id, is_root, admitted_by
            )
            return True
        if member.instance_id != instance.id:
            other = self.instances[member.instance_id]
            fields = sorted(
                k
                for k in other.body.keys() | instance.body.keys()
                if other.body.get(k) != instance.body.get(k)
            )
            raise refuse(
                "instance_conflict",
                f"the plan already holds another instance of task {instance.task_id}",
                task_id=instance.task_id,
                fields=fields,
            )
        return False

    def _observe(
        self,
        plan: PlanRow,
        task: TaskRow,
        item: RegistrationItem,
        counts: dict[str, int],
    ) -> None:
        if item.observed_complete:
            if task.status != "completed" and not self.live(task):
                if task.status == "running":
                    self.close_claim(task, "lapsed")
                self.move(task, "completed")
                task.completed_at = self.now()
                counts["completed"] += 1
                self.events.append(
                    Event(
                        "TASK_OBSERVED_COMPLETE", task.task_id, plan.build_id, plan.id
                    )
                )
        elif task.status == "completed" and (
            task.completed_at is None or task.completed_at < item.observed_at
        ):
            self.move(task, "pending")
            task.completed_at = None
            counts["invalidated"] += 1
            self.events.append(
                Event("TASK_INVALIDATED", task.task_id, plan.build_id, plan.id)
            )

    def _check_settings(self, settings: Mapping[str, str]) -> None:
        for key, value in settings.items():
            if not isinstance(value, str):
                raise refuse("invalid_settings", key, status=400)
            if key.startswith(("STARDAG_", "MODAL_")):
                raise refuse("reserved_settings_key", key, status=400)

    def plan_create(
        self,
        build_id: UUID,
        *,
        plan_id: UUID,
        deployment_id: UUID,
        settings: Mapping[str, str],
        roots: Sequence[RegistrationItem],
    ) -> PlanInfo:
        from stardag.testing._registry_state import settings_hash

        self._record(
            "plan_create",
            build_id=build_id,
            plan_id=plan_id,
            deployment_id=deployment_id,
            settings=dict(settings),
            roots=list(roots),
        )
        with self.transaction():
            if not roots:
                raise refuse("no_roots", status=400)
            if any(r.declared_upstreams is not None for r in roots):
                raise refuse("root_declared_upstreams", status=400)
            build = self.build(build_id)
            if {r.task_id for r in roots} != set(build.root_task_ids):
                raise refuse("root_mismatch", status=400)
            deployment = self.deployments.get(deployment_id)
            if deployment is None:
                raise refuse("unknown_deployment", status=400)
            if deployment.activated_at is None:
                raise refuse("deployment_not_activated", status=400)
            self._check_settings(settings)
            shash = settings_hash(settings)
            self.settings.setdefault(shash, dict(settings))
            existing = next(
                (
                    p
                    for p in self.plans.values()
                    if p.build_id == build_id
                    and p.deployment_id == deployment_id
                    and p.settings_hash == shash
                ),
                None,
            )
            if existing is not None:
                recorded = {
                    self.instances[m.instance_id].instance_hash
                    for m in self.members.get(existing.id, {}).values()
                    if m.is_root
                }
                if recorded != {r.instance_hash for r in roots}:
                    raise refuse("root_instance_conflict", "start a new build")
                self._register_items(existing, roots, as_roots=True)
                return plan_info(existing)
            if plan_id in self.plans:
                raise refuse("plan_id_conflict")
            generation = 1 + max(
                (p.generation for p in self.plans.values() if p.build_id == build_id),
                default=0,
            )
            plan = PlanRow(plan_id, build_id, deployment_id, shash, generation)
            if generation == 1:
                plan.activated_at = self.now()
            self.plans[plan_id] = plan
            self._register_items(plan, roots, as_roots=True)
            return plan_info(plan, created=True)

    def plan_register_members(
        self, plan_id: UUID, items: Sequence[RegistrationItem]
    ) -> MembersResult:
        self._record("plan_register_members", plan_id=plan_id, items=list(items))
        if len(items) > 1000:
            raise refuse("chunk_too_large", status=400)
        with self.transaction():
            return self._register_items(self.plan(plan_id), items, as_roots=False)

    def _closure(self, plan: PlanRow) -> None:
        """Admit every upstream instance reachable over edges from a member
        (whatever its status), so membership and edges agree."""
        changed = True
        while changed:
            changed = False
            for member in list(self.members.get(plan.id, {}).values()):
                for upstream_id in self.instances[member.instance_id].upstreams:
                    if self._admit(plan, self.instances[upstream_id], "closure"):
                        changed = True

    def plan_seal(self, plan_id: UUID) -> PlanInfo:
        self._record("plan_seal", plan_id=plan_id)
        with self.transaction():
            plan = self.plan(plan_id)
            if plan.sealed_at is not None:
                return plan_info(plan)
            self._closure(plan)
            for member in self.members.get(plan.id, {}).values():
                if member.is_root:
                    instance = self.instances[member.instance_id]
                    if (
                        not instance.expanded
                        and self.tasks[member.task_id].status != "completed"
                    ):
                        raise refuse(
                            "plan_incomplete_registration", reason="roots_unexpanded"
                        )
            deployment = self.deployments[plan.deployment_id]
            current = self.current_deployment(deployment.kind, deployment.app_name)
            if current is None or current.id != deployment.id:
                raise refuse("deployment_not_current")
            if any(
                p.build_id == plan.build_id and p.generation > plan.generation
                for p in self.plans.values()
            ):
                raise refuse("plan_superseded")
            now = self.now()
            plan.sealed_at = now
            if plan.activated_at is None:
                for other in self.plans.values():
                    if other.build_id == plan.build_id and other.active:
                        other.superseded_at = now
                plan.activated_at = now
            return plan_info(plan)

    def plan_roots(self, plan_id: UUID) -> list[FrontierMember]:
        plan = self.plan(plan_id)
        return [
            self._frontier_member(m)
            for m in self.members.get(plan.id, {}).values()
            if m.is_root
        ]

    # -- the frontier -------------------------------------------------------------------

    def _frontier_member(self, member: MemberRow) -> FrontierMember:
        instance = self.instances[member.instance_id]
        return FrontierMember(
            task_id=member.task_id,
            instance_id=instance.id,
            instance_hash=instance.instance_hash,
            status=self.tasks[member.task_id].status,
            is_root=member.is_root,
            body=dict(instance.body),
        )

    def _blocked(self, instance: InstanceRow) -> bool:
        return any(
            self.tasks[self.instances[u].task_id].status != "completed"
            for u in instance.upstreams
        )

    def build_get_frontier(self, build_id: UUID) -> BuildFrontier:
        self._record("build_get_frontier", build_id=build_id)
        build = self.build(build_id)
        plan = self.active_plan(build_id)
        base: dict[str, Any] = dict(
            build_id=build_id,
            build_status=build.status,
            reactive_app_name=build.reactive_app_name,
            reactive_tick_kwargs=build.reactive_tick_kwargs,
        )
        if plan is None:
            return BuildFrontier(**base)
        self._closure(plan)
        runnable, discovery, running = [], [], []
        complete = plan.sealed_at is not None
        for member in sorted(
            self.members.get(plan.id, {}).values(), key=lambda m: m.task_id
        ):
            if member.excluded_reason is not None:
                continue
            task = self.tasks[member.task_id]
            instance = self.instances[member.instance_id]
            if task.status != "completed":
                complete = False
            live = self.live(task)
            actionable = task.status in ACTIONABLE or (
                task.status == "running" and not live
            )
            if live:
                running.append(self._frontier_member(member))
            elif not instance.expanded:
                if task.status != "completed":
                    discovery.append(self._frontier_member(member))
            elif actionable and not self._blocked(instance):
                runnable.append(self._frontier_member(member))
        return BuildFrontier(
            **base,
            plan_id=plan.id,
            deployment_id=plan.deployment_id,
            settings_hash=plan.settings_hash,
            sealed=plan.sealed_at is not None,
            plan_complete=complete,
            runnable=runnable,
            discovery_jobs=discovery,
            running=running,
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
            self.events.append(
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
            self.events.append(
                Event(
                    outcome.upper(),
                    task_id,
                    plan.build_id,
                    plan_id,
                    execution_id,
                    applied=False,
                )
            )
            raise refuse("execution_not_current")
        self.close_claim(task, outcome)
        self.move(task, status, flag_except=plan.build_id)
        if status == "completed":
            task.completed_at = self.now()
            task.error_message = None
        elif status == "failed":
            task.error_message = error_message
        self.events.append(
            Event(
                f"TASK_{status.upper()}", task_id, plan.build_id, plan_id, execution_id
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
            raise refuse("claim_not_held")
        task.claim_expires_at = self.now() + self._ttl(claim_ttl_seconds)
        return _outcome(task)
