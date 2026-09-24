"""An in-memory v2 registry, for tests and local experiments.

:class:`InMemoryRegistry` implements :class:`~stardag.registry.RegistryABC`
with the server's semantics — the double the SDK's own tests drive the
engines, the tick, the bootstrap and the worker against. It follows the
server's seams one to one (engineering rule 7, "a fake per server seam,
changed in the same PR as the seam"): the refusals carry the server's codes
(``instance_conflict``, ``plan_superseded``, ``upstream_incomplete``, ...),
each write is all-or-nothing, and the frontier is computed from the same
rows the transitions write.

Time is ``registry.clock`` (UTC-aware ``datetime``), replaceable so a test
can let a claim lapse. Every call is recorded (:meth:`calls_to`) for
assertions.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID

from stardag.build._registration import new_id
from stardag.registry import (
    BuildInfo,
    BuildNotifyResult,
    ConcurrencyLimitHolderInfo,
    ConcurrencyLimitInfo,
    DeploymentInfo,
    ExecutionInfo,
    PlanRoots,
    RegistryABC,
    ResumeResult,
    SchedulerLeaseResult,
    SettingsInfo,
    TaskArtifactInfo,
    TaskInfo,
    TaskInstanceInfo,
    TickSummaryRecord,
    TransitionResult,
    WakeCandidate,
)
from stardag.registry._models import DeploymentKind, StopOutcome
from stardag.testing._registry_exclusion import ExclusionMixin
from stardag.testing._registry_plans import _outcome, plan_info
from stardag.testing._registry_reads import ReadsMixin
from stardag.testing._registry_state import (
    WAKE_HANDOUT_WINDOW,
    ArtifactRow,
    BuildRow,
    DeploymentRow,
    Event,
    refuse,
    settings_hash,
)
from stardag.testing._registry_yield import YieldMixin

if TYPE_CHECKING:
    from stardag.artifact import Artifact


class InMemoryRegistry(YieldMixin, ExclusionMixin, ReadsMixin, RegistryABC):
    """See the module docstring."""

    # -- setup helpers for tests ----------------------------------------------------

    def add_deployment(
        self,
        *,
        kind: DeploymentKind = "modal",
        app_name: str = "app",
        code_id: str = "code",
        activated: bool = True,
        deployment_id: UUID | None = None,
    ) -> UUID:
        """Record (and by default activate) a deployment; returns its id."""
        row = self.deployment_create(
            kind=kind,
            code_id=code_id,
            deployment_id=deployment_id or new_id(),
            app_name=app_name,
        )
        if activated and kind == "modal":
            self.deployment_activate(row.id)
        return row.id

    def status_of(self, task_id: object) -> str:
        return self.task(str(task_id)).status

    # -- builds ------------------------------------------------------------------------

    def build_create(
        self,
        *,
        root_task_ids: Sequence[str],
        build_id: UUID | None = None,
        name: str | None = None,
        description: str | None = None,
        executor_metadata: dict[str, Any] | None = None,
    ) -> BuildInfo:
        self._record(
            "build_create", root_task_ids=list(root_task_ids), build_id=build_id
        )
        if not root_task_ids:
            raise refuse("invalid_request", "root_task_ids is required", status=422)
        build_id = build_id or new_id()
        if build_id in self.builds:
            return self._info(self.builds[build_id])
        now = self.now()
        build = BuildRow(
            id=build_id,
            name=name or f"build-{len(self.builds) + 1}",
            root_task_ids=sorted(set(root_task_ids)),
            description=description,
            executor_metadata=executor_metadata,
            created_at=now,
            last_active_at=now,
        )
        self.builds[build_id] = build
        self.log(Event("BUILD_STARTED", build_id=build_id))
        return self._info(build)

    def build_get(self, build_id: UUID) -> BuildInfo:
        return self._info(self.build(build_id))

    def build_resume(
        self,
        build_id: UUID,
        *,
        deployment_id: UUID | None = None,
        settings: Mapping[str, str] | None = None,
        executor_metadata: dict[str, Any] | None = None,
    ) -> ResumeResult:
        self._record(
            "build_resume",
            build_id=build_id,
            deployment_id=deployment_id,
            settings=dict(settings or {}),
        )
        with self.transaction():
            build = self.build(build_id)
            changed = False
            plan = None
            if deployment_id is not None:
                shash = settings_hash(settings or {})
                plan = next(
                    (
                        p
                        for p in self.plans.values()
                        if p.build_id == build_id
                        and p.deployment_id == deployment_id
                        and p.settings_hash == shash
                    ),
                    None,
                )
                if plan is not None and plan.superseded_at is not None:
                    deployment = self.deployments[deployment_id]
                    self.verify_deployment_current(deployment)
                    active = self.active_plan(build_id)
                    if active is not None:
                        active.superseded_at = self.now()
                    plan.superseded_at = None
                    plan.activated_at = self.now()
                    changed = True
            if build.status != "running":
                build.status = "running"
                build.error_message = None
                changed = True
            if executor_metadata is not None:
                build.executor_metadata = executor_metadata
            if changed:
                build.is_resumed = True
                build.last_active_at = self.now()
                self.log(Event("BUILD_RESUMED", build_id=build_id))
            return ResumeResult(
                build=self._info(build),
                plan=plan_info(plan) if plan is not None else None,
                changed=changed,
            )

    def _terminal(
        self, build_id: UUID, status: str, error: str | None = None
    ) -> BuildInfo:
        build = self.build(build_id)
        if build.status == status:
            return self._info(build)
        build.status = status
        build.error_message = error
        build.last_active_at = self.now()
        self.release_build_claims(build)
        self.log(Event(f"BUILD_{status.upper()}", build_id=build_id))
        return self._info(build)

    def build_complete(self, build_id: UUID, *, force: bool = False) -> BuildInfo:
        self._record("build_complete", build_id=build_id, force=force)
        build = self.build(build_id)
        if build.status == "completed":
            return self._info(build)
        plan = self.active_plan(build_id)
        if plan is None or plan.sealed_at is None:
            raise refuse("plan_incomplete", reason="not_sealed")
        members = self.members.get(plan.id, {})
        if any(m.is_root and m.excluded_reason for m in members.values()):
            raise refuse("plan_incomplete", reason="root_excluded")
        outstanding = [
            m.task_id
            for m in members.values()
            if m.excluded_reason is None and self.tasks[m.task_id].status != "completed"
        ]
        if outstanding and not force:
            raise refuse(
                "plan_incomplete", reason="members_incomplete", task_ids=outstanding
            )
        return self._terminal(build_id, "completed")

    def build_fail(self, build_id: UUID, error_message: str | None = None) -> BuildInfo:
        self._record("build_fail", build_id=build_id, error_message=error_message)
        return self._terminal(build_id, "failed", error_message)

    def build_cancel(self, build_id: UUID) -> BuildInfo:
        self._record("build_cancel", build_id=build_id)
        return self._terminal(build_id, "cancelled")

    def build_exit_early(self, build_id: UUID) -> BuildInfo:
        self._record("build_exit_early", build_id=build_id)
        build = self.build(build_id)
        build.status = "exit_early"
        build.last_active_at = self.now()
        return self._info(build)

    def plan_roots_info(self, plan_id: UUID) -> PlanRoots:
        plan = self.plan(plan_id)
        return PlanRoots(
            plan_id=plan.id,
            build_id=plan.build_id,
            deployment_id=plan.deployment_id,
            settings_hash=plan.settings_hash,
            roots=self.plan_roots(plan_id),
        )

    # -- executions and tasks ------------------------------------------------------------

    def build_list_executions(
        self,
        build_id: UUID,
        *,
        not_in_current_plan: bool = False,
        include_ended: bool = False,
    ) -> list[ExecutionInfo]:
        self._record(
            "build_list_executions",
            build_id=build_id,
            not_in_current_plan=not_in_current_plan,
            include_ended=include_ended,
        )
        self.build(build_id)
        rows = [
            self._execution_info(e)
            for e in self.executions.values()
            if (include_ended or e.ended_at is None)
            and self.plans[e.plan_id].build_id == build_id
        ]
        return [r for r in rows if not (not_in_current_plan and r.in_current_plan)]

    def execution_report_stopped(
        self, execution_id: UUID, *, outcome: StopOutcome = "stopped"
    ) -> TransitionResult:
        self._record(
            "execution_report_stopped", execution_id=execution_id, outcome=outcome
        )
        if outcome not in ("stopped", "lost"):
            raise refuse("invalid_request", f"outcome {outcome!r}", status=422)
        execution = self.executions.get(execution_id)
        if execution is None:
            raise refuse("unknown_execution", status=404)
        task = self.task(execution.task_id)
        if execution.ended_at is None:
            execution.ended_at = self.now()
            execution.outcome = outcome
        # The server's rule: the current execution's unreleased claim is
        # released ``cancelled`` and the task CANCELLED (actionable) — a
        # revocation is not a result.
        if (
            task.execution_id == execution_id
            and execution.claim_released_at is None
            and task.status == "running"
        ):
            self.close_claim(task, "cancelled")
            self.move(task, "cancelled")
        return _outcome(task)

    def task_get(self, task_id: str) -> TaskInfo:
        task = self.task(task_id)
        # Insertion order is oldest-first; the server reads newest first.
        instances = [
            TaskInstanceInfo(
                id=i.id,
                deployment_id=i.deployment_id,
                settings_hash=i.settings_hash,
                instance_hash=i.instance_hash,
                body=i.body,
            )
            for i in self.instances.values()
            if i.task_id == task.task_id
        ]
        instances.reverse()
        return self._task_summary(task, instances=instances)

    def task_list_artifacts(self, task_id: str) -> list[TaskArtifactInfo]:
        self.task(task_id)
        return [
            TaskArtifactInfo(
                id=a.id,
                task_id=task_id,
                artifact_type=a.artifact_type,
                name=a.name,
                # The HTTP contract normalises a markdown body to
                # ``{"content": ...}`` (``_artifacts_body`` on upload); a
                # json artifact's body is already a dict.
                body={"content": a.body} if a.artifact_type == "markdown" else a.body,
                created_at=a.created_at,
            )
            for a in self.artifacts.get(task_id, [])
        ]

    def task_upload_artifacts(
        self,
        plan_id: UUID,
        task_id: str,
        artifacts: Sequence[Artifact],
        *,
        execution_id: UUID | None = None,
    ) -> None:
        if task_id not in self.members.get(plan_id, {}):
            raise refuse(
                "not_a_member",
                f"task {task_id} is not a member of plan {plan_id}",
                status=404,
            )
        # Upsert per (type, name), like the server's `on_conflict_do_update`
        # (services/artifacts.py): a re-upload replaces only `body`, so `id`
        # and `created_at` are minted once and kept -- not reset on every
        # upload -- exactly like the real ``TaskArtifact`` row.
        by_key = {
            (row.artifact_type, row.name): row
            for row in self.artifacts.get(task_id, [])
        }
        now = self.now()
        for artifact in artifacts:
            key = (artifact.type, artifact.name)
            prior = by_key.get(key)
            by_key[key] = ArtifactRow(
                id=prior.id if prior is not None else new_id(),
                artifact_type=artifact.type,
                name=artifact.name,
                body=artifact.body,
                created_at=prior.created_at if prior is not None else now,
            )
        self.artifacts[task_id] = list(by_key.values())

    # -- deployments and settings -------------------------------------------------------

    def _deployment_info(
        self, row: DeploymentRow, *, created: bool = False
    ) -> DeploymentInfo:
        current = self.current_deployment(row.kind, row.app_name)
        return DeploymentInfo(
            id=row.id,
            kind=row.kind,
            app_name=row.app_name,
            code_id=row.code_id,
            image_id=row.image_id,
            modal_app_id=row.modal_app_id,
            generation=row.generation,
            deployed_at=row.deployed_at,
            activated_at=row.activated_at,
            is_current=current is not None and current.id == row.id,
            created=created,
        )

    def deployment_create(
        self,
        *,
        kind: DeploymentKind,
        code_id: str,
        deployment_id: UUID | None = None,
        app_name: str | None = None,
        image_id: str | None = None,
        modal_app_id: str | None = None,
    ) -> DeploymentInfo:
        self._record(
            "deployment_create",
            kind=kind,
            code_id=code_id,
            deployment_id=deployment_id,
            app_name=app_name,
        )
        now = self.now()
        if kind == "local":
            app_name = app_name or "local"
            for row in self.deployments.values():
                if row.kind == "local" and row.code_id == code_id:
                    if row.app_name != app_name:
                        raise refuse("local_deployment_conflict")
                    return self._deployment_info(row)
        else:
            if deployment_id is None:
                raise refuse("deployment_id_required", status=400)
            if not app_name:
                raise refuse("app_name_required", status=400)
            existing = self.deployments.get(deployment_id)
            if existing is not None:
                if (existing.kind, existing.app_name, existing.code_id) != (
                    kind,
                    app_name,
                    code_id,
                ):
                    raise refuse("deployment_id_conflict")
                return self._deployment_info(existing)
        generation = 1 + max(
            (
                d.generation
                for d in self.deployments.values()
                if d.kind == kind and d.app_name == app_name
            ),
            default=0,
        )
        row = DeploymentRow(
            id=deployment_id or new_id(),
            kind=kind,
            app_name=app_name,
            code_id=code_id,
            generation=generation,
            deployed_at=now,
            activated_at=now if kind == "local" else None,
            image_id=image_id,
            modal_app_id=modal_app_id,
        )
        self.deployments[row.id] = row
        return self._deployment_info(row, created=True)

    def deployment_activate(
        self,
        deployment_id: UUID,
        *,
        modal_app_id: str | None = None,
        image_id: str | None = None,
    ) -> DeploymentInfo:
        self._record(
            "deployment_activate",
            deployment_id=deployment_id,
            modal_app_id=modal_app_id,
            image_id=image_id,
        )
        row = self.deployments.get(deployment_id)
        if row is None:
            raise refuse("unknown_deployment", status=404)
        # A given value fills a NULL or must match (the server's rule):
        # 409 `deployment_activation_conflict` with every clashing field,
        # nothing written (services/deployments.py, activate_deployment).
        given = {"modal_app_id": modal_app_id, "image_id": image_id}
        clashing = sorted(
            column
            for column, value in given.items()
            if value is not None and getattr(row, column) not in (None, value)
        )
        if clashing:
            raise refuse("deployment_activation_conflict", fields=clashing)
        for column, value in given.items():
            if value is not None:
                setattr(row, column, value)
        if row.activated_at is None:
            row.activated_at = self.now()
        return self._deployment_info(row)

    def deployment_list(
        self,
        *,
        kind: DeploymentKind | None = None,
        app_name: str | None = None,
        current: bool = False,
        limit: int = 100,
    ) -> list[DeploymentInfo]:
        self._record(
            "deployment_list",
            kind=kind,
            app_name=app_name,
            current=current,
            limit=limit,
        )
        rows = sorted(
            (
                d
                for d in self.deployments.values()
                if (kind is None or d.kind == kind)
                and (app_name is None or d.app_name == app_name)
            ),
            key=lambda d: d.deployed_at,
            reverse=True,
        )
        infos = [self._deployment_info(d) for d in rows]
        if current:
            infos = [i for i in infos if i.is_current]
        return infos[:limit]

    def settings_get(self, settings_hash: str) -> SettingsInfo:
        body = self.settings.get(settings_hash)
        if body is None:
            raise refuse("unknown_settings", status=404)
        return SettingsInfo(hash=settings_hash, body=dict(body))

    # -- concurrency limits --------------------------------------------------------------

    def concurrency_limit_set(self, key: str, max_concurrent: int) -> None:
        self._record("concurrency_limit_set", key=key, max_concurrent=max_concurrent)
        self.limits[key] = max_concurrent

    def concurrency_limit_delete(self, key: str) -> None:
        self._record("concurrency_limit_delete", key=key)
        if self.limits.pop(key, None) is None:
            raise refuse("unknown_limit", status=404)

    def concurrency_limit_list(self) -> dict[str, int]:
        return dict(sorted(self.limits.items()))

    def concurrency_limit_list_detailed(
        self, *, include_holders: bool = False
    ) -> list[ConcurrencyLimitInfo]:
        """Mirrors the server's ``list_limits``: ``in_use`` from the same
        ``live()`` definition the claiming start enforces against, holders
        added only when asked."""
        in_use_by_key: dict[str, int] = {}
        holders_by_key: dict[str, list[ConcurrencyLimitHolderInfo]] = {}
        for task in self.tasks.values():
            if not self.live(task) or not task.limit_keys:
                continue
            for key in task.limit_keys:
                in_use_by_key[key] = in_use_by_key.get(key, 0) + 1
            if not include_holders:
                continue
            plan = self.plans.get(task.claim_plan_id) if task.claim_plan_id else None
            if plan is None:
                continue
            execution = (
                self.executions.get(task.execution_id) if task.execution_id else None
            )
            holder = ConcurrencyLimitHolderInfo(
                task_id=task.task_id,
                task_name=task.task_name,
                build_id=plan.build_id,
                plan_id=plan.id,
                execution_id=task.execution_id,
                started_at=execution.started_at if execution else None,
            )
            for key in task.limit_keys:
                holders_by_key.setdefault(key, []).append(holder)
        for holders in holders_by_key.values():
            holders.sort(key=lambda h: (h.started_at is None, h.started_at, h.task_id))
        return [
            ConcurrencyLimitInfo(
                key=key,
                max_concurrent=max_concurrent,
                in_use=in_use_by_key.get(key, 0),
                holders=holders_by_key.get(key, []) if include_holders else None,
            )
            for key, max_concurrent in sorted(self.limits.items())
        ]

    # -- reactive scheduling ---------------------------------------------------------------

    def build_set_reactive_meta(
        self,
        build_id: UUID,
        *,
        app_name: str,
        tick_kwargs: dict[str, Any] | None = None,
    ) -> BuildInfo:
        self._record(
            "build_set_reactive_meta",
            build_id=build_id,
            app_name=app_name,
            tick_kwargs=tick_kwargs,
        )
        build = self.build(build_id)
        build.reactive_app_name = app_name
        if tick_kwargs is not None:
            build.reactive_tick_kwargs = dict(tick_kwargs)
        return self._info(build)

    def _lease_live(self, build: BuildRow) -> bool:
        return (
            build.lease_expires_at is not None and build.lease_expires_at > self.now()
        )

    def build_notify(
        self, build_id: UUID, *, can_spawn: bool = True
    ) -> BuildNotifyResult:
        self._record("build_notify", build_id=build_id, can_spawn=can_spawn)
        build = self.build(build_id)
        if build.status != "running":
            return BuildNotifyResult(
                build_id=build_id, needs_tick=False, scheduler_live=False
            )
        build.needs_tick = True
        if can_spawn:
            build.handed_out_at = self.now()
        return BuildNotifyResult(
            build_id=build_id, needs_tick=True, scheduler_live=self._lease_live(build)
        )

    def build_get_notify(self, build_id: UUID) -> BuildNotifyResult:
        build = self.build(build_id)
        return BuildNotifyResult(build_id=build_id, needs_tick=build.needs_tick)

    def build_clear_notify(self, build_id: UUID) -> None:
        self._record("build_clear_notify", build_id=build_id)
        self.build(build_id).needs_tick = False

    def build_wake_candidates(self, limit: int = 20) -> list[WakeCandidate]:
        now = self.now()
        chosen: list[WakeCandidate] = []
        for build in self.builds.values():
            if len(chosen) >= limit:
                break
            if not build.needs_tick or build.status != "running":
                continue
            if build.reactive_app_name is None or self._lease_live(build):
                continue
            if (
                build.handed_out_at is not None
                and now - build.handed_out_at < WAKE_HANDOUT_WINDOW
            ):
                continue
            build.handed_out_at = now
            chosen.append(
                WakeCandidate(
                    build_id=build.id, reactive_app_name=build.reactive_app_name
                )
            )
        return chosen

    def scheduler_lease_acquire(
        self, build_id: UUID, *, owner_id: str, ttl_seconds: int
    ) -> SchedulerLeaseResult:
        build = self.build(build_id)
        if self._lease_live(build) and build.lease_owner != owner_id:
            return SchedulerLeaseResult(held=False, expires_at=build.lease_expires_at)
        build.lease_owner = owner_id
        build.lease_expires_at = self.now() + timedelta(seconds=ttl_seconds)
        return SchedulerLeaseResult(held=True, expires_at=build.lease_expires_at)

    def scheduler_lease_renew(
        self, build_id: UUID, *, owner_id: str, ttl_seconds: int
    ) -> SchedulerLeaseResult:
        build = self.build(build_id)
        if build.lease_owner != owner_id or not self._lease_live(build):
            return SchedulerLeaseResult(held=False)
        build.lease_expires_at = self.now() + timedelta(seconds=ttl_seconds)
        return SchedulerLeaseResult(held=True, expires_at=build.lease_expires_at)

    def scheduler_lease_release(
        self, build_id: UUID, *, owner_id: str
    ) -> SchedulerLeaseResult:
        build = self.build(build_id)
        if build.lease_owner != owner_id:
            return SchedulerLeaseResult(held=False)
        build.lease_owner = None
        build.lease_expires_at = None
        return SchedulerLeaseResult(held=True)

    def build_report_tick_summary(
        self, build_id: UUID, summary: Mapping[str, Any]
    ) -> None:
        self.tick_summaries.setdefault(build_id, []).append(dict(summary))

    def build_list_tick_summaries(
        self, build_id: UUID, *, limit: int = 20
    ) -> list[TickSummaryRecord]:
        return [
            TickSummaryRecord(outcome=s.get("outcome", "?"), summary=s)
            for s in reversed(self.tick_summaries.get(build_id, []))
        ][:limit]
