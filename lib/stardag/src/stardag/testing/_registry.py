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
can let a claim lapse. ``add_deployment`` and ``add_instance`` seed rows a
build would otherwise have written. ``add_instance`` is how a test serves a
pinned ``Task.ref``: seed the instance body recorded next to the code
(``stardag tasks show <task-id> --json``) under its real task and instance
ids, and the reference hydrates from it with no registry. Every call is recorded (:meth:`calls_to`) for
assertions.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from types import EllipsisType
from typing import TYPE_CHECKING, Any
from uuid import UUID

from stardag.build._registration import new_id
from stardag.registry import (
    BuildInfo,
    ConcurrencyLimitHolderInfo,
    ConcurrencyLimitInfo,
    ExecutionInfo,
    PlanRoots,
    RegistryABC,
    ResumeResult,
    SettingsInfo,
    TaskArtifactInfo,
    TaskInfo,
    TaskInstanceInfo,
    TransitionResult,
)
from stardag.exceptions import APIError
from stardag.registry._api_reads import _tag_list
from stardag.registry._models import DeploymentKind, StopOutcome
from stardag.testing._registry_deployments import DeploymentsMixin
from stardag.testing._registry_exclusion import ExclusionMixin
from stardag.testing._registry_plans import plan_info
from stardag.testing._registry_reactive import ReactiveMixin
from stardag.testing._registry_reads import ReadsMixin
from stardag.testing._registry_state import (
    TERMINAL_BUILD_STATUSES,
    ArtifactRow,
    BuildRow,
    MAX_DESCRIPTION_LENGTH,
    MAX_TAGS,
    Event,
    InstanceRow,
    TaskRow,
    normalise_tags,
    refuse,
    refuse_plan_only,
    settings_hash,
)
from stardag.testing._registry_transitions import _outcome
from stardag.testing._registry_yield import YieldMixin

if TYPE_CHECKING:
    from stardag.artifact import Artifact


class InMemoryRegistry(
    YieldMixin,
    ExclusionMixin,
    ReactiveMixin,
    DeploymentsMixin,
    ReadsMixin,
    RegistryABC,
):
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

    def add_instance(
        self,
        *,
        body: Mapping[str, Any],
        task_id: str | UUID,
        instance_id: str | UUID,
    ) -> TaskInstanceInfo:
        """Seed one instance, as a registry holding it would serve it,
        without a build, a deployment or a class import.

        The intended use: code pins ``MyTask.ref("<task-id>",
        instance_id="<instance-id>")`` to a real instance, its body is
        recorded next to the code (``stardag tasks show <task-id> --json``),
        and a test seeds it under the same ids, so the pinned reference
        hydrates offline::

            registry = InMemoryRegistry()
            for rec in recorded:  # {task_id, instance_id, body}
                registry.add_instance(
                    body=rec.body, task_id=rec.task_id, instance_id=rec.instance_id
                )
            with test_harness(registry=registry):
                assert MyTask.ref(task_id, instance_id=instance_id).hydrate()

        ``task_id`` and ``instance_id`` are used exactly as given (never
        minted: a pinned reference names the instance id). The body is
        stored as is and **not validated**: whether it rebuilds into a task
        with ``task_id`` is checked by hydration, as against a real
        registry, so a test can also seed a mismatch on purpose.

        An unpinned reference (``task_get``) reads the most recently seeded
        instance of the task. Seeding the same instance again is a no-op
        with the same body and task id, and a ``ValueError`` otherwise. The
        task row is created when missing; a later registration of the task
        fills in its output URI, which seeding cannot know.
        """
        from stardag._core.task_id import canonical_body_json, instance_hash_of_body
        from stardag.polymorphic import NAME_KEY, NAMESPACE_KEY

        tid, iid = str(task_id), UUID(str(instance_id))
        body = dict(body)
        existing = self.instances.get(iid)
        if existing is not None:
            if existing.task_id != tid or existing.body != body:
                raise ValueError(
                    f"Instance {iid} is already seeded with a different "
                    f"{'task id' if existing.task_id != tid else 'body'}."
                )
            return self.task_instance_get(iid)
        if tid not in self.tasks:
            self.tasks[tid] = TaskRow(
                tid,
                str(body.get(NAMESPACE_KEY, "")),
                str(body.get(NAME_KEY, "")),
                body.get("version"),
                None,
                status_at=self.now(),
                seeded=True,
            )
        self.instances[iid] = InstanceRow(
            iid,
            new_id(),
            settings_hash({}),
            str(instance_hash_of_body(canonical_body_json(body))),
            tid,
            body,
        )
        return self.task_instance_get(iid)

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
        plan_only: bool = False,
    ) -> BuildInfo:
        self._record(
            "build_create",
            root_task_ids=list(root_task_ids),
            build_id=build_id,
            plan_only=plan_only,
        )
        if not root_task_ids:
            raise refuse("invalid_request", "root_task_ids is required", status=422)
        build_id = build_id or new_id()
        if build_id in self.builds:
            existing = self.builds[build_id]
            if existing.plan_only != plan_only:
                raise refuse("build_id_conflict")
            return self._info(existing)
        now = self.now()
        build = BuildRow(
            id=build_id,
            name=name or f"build-{len(self.builds) + 1}",
            root_task_ids=sorted(set(root_task_ids)),
            description=description,
            executor_metadata=executor_metadata,
            created_at=now,
            last_active_at=now,
            plan_only=plan_only,
            status="pending" if plan_only else "running",
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
            refuse_plan_only(build, "resume")
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

    def _refuse_if_terminal(self, build: BuildRow, event_type: str) -> None:
        """A terminal build status is sticky: another lifecycle transition
        is recorded (``applied=False``) and refused ``build_terminal``."""
        if build.status in TERMINAL_BUILD_STATUSES:
            self.log(
                Event(
                    event_type,
                    build_id=build.id,
                    applied=False,
                    detail={"refused": "build_terminal"},
                )
            )
            raise refuse(
                "build_terminal",
                f"the build is {build.status.upper()}",
                build_id=str(build.id),
                build_status=build.status,
            )

    def _terminal(
        self, build_id: UUID, status: str, error: str | None = None
    ) -> BuildInfo:
        build = self.build(build_id)
        if build.status == status:
            return self._info(build)
        self._refuse_if_terminal(build, f"BUILD_{status.upper()}")
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
        self._refuse_if_terminal(build, "BUILD_COMPLETED")
        refuse_plan_only(build, "complete")
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
        if build.status == "exit_early":
            return self._info(build)
        self._refuse_if_terminal(build, "BUILD_EXIT_EARLY")
        refuse_plan_only(build, "exit early")
        build.status = "exit_early"
        build.last_active_at = self.now()
        return self._info(build)

    def build_registered(self, build_id: UUID) -> BuildInfo:
        self._record("build_registered", build_id=build_id)
        # A ``build_terminal`` refusal is recorded (its unapplied event
        # kept), as the server commits it with the refusal.
        with self.transaction(
            keep=lambda e: getattr(e, "code", None)
            in ("build_terminal", "instance_conflict")
        ):
            build = self.build(build_id)
            if build.status == "registered":
                return self._info(build)
            if not build.plan_only:
                raise refuse("build_not_plan_only", build_status=build.status)
            self._refuse_if_terminal(build, "BUILD_REGISTERED")
            plan = self.active_plan(build_id)
            if plan is None or plan.sealed_at is None:
                raise refuse("plan_incomplete", reason="not_sealed")
            try:
                self._closure(plan)
            except APIError as e:
                if e.code != "instance_conflict":
                    raise
                # As the server's closure step: the conflict fails the build
                # (committed, with its BUILD_FAILED) and refuses the call.
                self._terminal(build_id, "failed", f"instance conflict: {e}")
                raise
            build.status = "registered"
            build.last_active_at = self.now()
            self.log(Event("BUILD_REGISTERED", build_id=build_id))
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
        self,
        execution_id: UUID,
        *,
        outcome: StopOutcome = "stopped",
        only_if_unspawned: bool = False,
    ) -> TransitionResult:
        self._record(
            "execution_report_stopped",
            execution_id=execution_id,
            outcome=outcome,
            only_if_unspawned=only_if_unspawned,
        )
        if outcome not in ("stopped", "lost"):
            raise refuse("invalid_request", f"outcome {outcome!r}", status=422)
        execution = self.executions.get(execution_id)
        if execution is None:
            raise refuse("unknown_execution", status=404)
        task = self.task(execution.task_id)
        if execution.ended_at is not None:
            return _outcome(task)
        if only_if_unspawned and execution.executor_ref is not None:
            raise refuse("execution_spawned", execution_id=str(execution_id))
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

    def task_annotate(
        self,
        task_id: str,
        *,
        tags: Sequence[str] | EllipsisType = ...,
        description: str | None | EllipsisType = ...,
    ) -> TaskInfo:
        """Mirrors ``PATCH /tasks/{task_id}``: omitted fields unchanged,
        tags normalised, a blank description cleared; neither the status
        nor ``status_at`` moves."""
        self._record(
            "task_annotate", task_id=task_id, tags=tags, description=description
        )
        if tags is not ...:
            tags = _tag_list(tags)
        task = self.task(task_id)
        # The request's own bounds, checked before normalising (duplicates
        # and surrounding whitespace may make up the difference).
        if (tags is not ... and len(tags) > 4 * MAX_TAGS) or (
            isinstance(description, str)
            and len(description) > 4 * MAX_DESCRIPTION_LENGTH
        ):
            raise refuse("validation_error", "request too large", status=422)
        new_tags = normalise_tags(tags) if tags is not ... else task.tags
        if description is not ...:
            text = (description or "").strip()
            if len(text) > MAX_DESCRIPTION_LENGTH or "\x00" in text:
                raise refuse("invalid_description", status=422)
            task.description = text or None
        task.tags = new_tags
        return self._task_summary(task)

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
