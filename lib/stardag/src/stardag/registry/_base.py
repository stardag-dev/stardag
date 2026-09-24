"""The registry interface (v2), the do-nothing default, and the provider.

:class:`RegistryABC` is the SDK's one seam to the registry: every route of
``/api/v2`` the engines, the Modal integration and the CLI use is a method
here, and :class:`~stardag.registry.APIRegistry` implements them over HTTP.
Methods come in pairs — a sync one and an ``_aio`` one — because both kinds
of caller exist (a Modal worker's reporter and the CLI are sync; the engines
and the tick are async). The ``_aio`` defaults call the sync method, which
is what an in-memory double wants; the HTTP client overrides both.

Nothing here is abstract: a double implements the seams its test exercises,
and an unimplemented one raises :class:`NotImplementedError` naming itself.
A build without a registry uses :class:`NoOpRegistry`, which the engines
recognise by its exact type and never call (design.md D11: no registry, no
plan, no claims).

**Identities are client-minted** (build, plan, execution, deployment and
yield-batch ids), so every write is idempotent on re-delivery; see
design.md, "Registration".
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping, Sequence
from functools import lru_cache
from typing import TYPE_CHECKING, Any
from uuid import UUID

from stardag.registry._models import (
    BuildFrontier,
    BuildInfo,
    BuildNotifyResult,
    DeploymentInfo,
    DeploymentKind,
    ExclusionResult,
    ExecutionInfo,
    FrontierMember,
    MembersResult,
    PlanInfo,
    PlanRoots,
    RegistrationItem,
    ResumeResult,
    SchedulerLeaseResult,
    SettingsInfo,
    StopOutcome,
    TaskArtifactInfo,
    TaskInfo,
    TickSummaryRecord,
    TransitionResult,
    WakeCandidate,
    YieldResult,
)
from stardag.utils.resource_provider import resource_provider

if TYPE_CHECKING:
    from stardag.artifact import Artifact


def _missing(registry: object, method: str) -> NotImplementedError:
    return NotImplementedError(f"{type(registry).__name__} does not implement {method}")


class RegistryABC:
    """The v2 registry client interface. See the module docstring."""

    # -- builds ---------------------------------------------------------------

    def build_create(
        self,
        *,
        root_task_ids: Sequence[str],
        build_id: UUID | None = None,
        name: str | None = None,
        description: str | None = None,
        executor_metadata: dict[str, Any] | None = None,
    ) -> BuildInfo:
        """``POST /builds``: a RUNNING build requesting ``root_task_ids``.
        Idempotent on a client-minted ``build_id``."""
        raise _missing(self, "build_create")

    async def build_create_aio(
        self,
        *,
        root_task_ids: Sequence[str],
        build_id: UUID | None = None,
        name: str | None = None,
        description: str | None = None,
        executor_metadata: dict[str, Any] | None = None,
    ) -> BuildInfo:
        return self.build_create(
            root_task_ids=root_task_ids,
            build_id=build_id,
            name=name,
            description=description,
            executor_metadata=executor_metadata,
        )

    def build_get(self, build_id: UUID) -> BuildInfo:
        raise _missing(self, "build_get")

    async def build_get_aio(self, build_id: UUID) -> BuildInfo:
        return self.build_get(build_id)

    def build_resume(
        self,
        build_id: UUID,
        *,
        deployment_id: UUID | None = None,
        settings: Mapping[str, str] | None = None,
        executor_metadata: dict[str, Any] | None = None,
    ) -> ResumeResult:
        """``POST /builds/{id}/resume``: make the build RUNNING again and,
        when the caller names its scope, reuse or reactivate the plan for
        it."""
        raise _missing(self, "build_resume")

    async def build_resume_aio(
        self,
        build_id: UUID,
        *,
        deployment_id: UUID | None = None,
        settings: Mapping[str, str] | None = None,
        executor_metadata: dict[str, Any] | None = None,
    ) -> ResumeResult:
        return self.build_resume(
            build_id,
            deployment_id=deployment_id,
            settings=settings,
            executor_metadata=executor_metadata,
        )

    def build_complete(self, build_id: UUID, *, force: bool = False) -> BuildInfo:
        """``POST /builds/{id}/complete``, refused (409 ``plan_incomplete``)
        unless the active plan is sealed and every non-excluded member is
        COMPLETED; ``force`` overrides outstanding members only."""
        raise _missing(self, "build_complete")

    async def build_complete_aio(
        self, build_id: UUID, *, force: bool = False
    ) -> BuildInfo:
        return self.build_complete(build_id, force=force)

    def build_fail(self, build_id: UUID, error_message: str | None = None) -> BuildInfo:
        raise _missing(self, "build_fail")

    async def build_fail_aio(
        self, build_id: UUID, error_message: str | None = None
    ) -> BuildInfo:
        return self.build_fail(build_id, error_message)

    def build_cancel(self, build_id: UUID) -> BuildInfo:
        raise _missing(self, "build_cancel")

    async def build_cancel_aio(self, build_id: UUID) -> BuildInfo:
        return self.build_cancel(build_id)

    def build_exit_early(self, build_id: UUID) -> BuildInfo:
        """``POST /builds/{id}/exit-early``: the resident driver stops;
        nothing is released (its in-flight executions keep reporting)."""
        raise _missing(self, "build_exit_early")

    async def build_exit_early_aio(self, build_id: UUID) -> BuildInfo:
        return self.build_exit_early(build_id)

    def build_list(
        self,
        *,
        status: str | None = None,
        reactive_app_name: str | None = None,
        limit: int = 100,
    ) -> list[BuildInfo]:
        """``GET /builds``: builds, most recently active first, optionally
        by status and by reactive app."""
        raise _missing(self, "build_list")

    def build_list_running(
        self, *, reactive_app_name: str | None = None, limit: int = 100
    ) -> list[UUID]:
        """RUNNING builds, most recently active first (the watchdog sweep)."""
        raise _missing(self, "build_list_running")

    def build_get_frontier(self, build_id: UUID) -> BuildFrontier:
        raise _missing(self, "build_get_frontier")

    async def build_get_frontier_aio(self, build_id: UUID) -> BuildFrontier:
        return self.build_get_frontier(build_id)

    # -- plans and registration ----------------------------------------------

    def plan_create(
        self,
        build_id: UUID,
        *,
        plan_id: UUID,
        deployment_id: UUID,
        settings: Mapping[str, str],
        roots: Sequence[RegistrationItem],
    ) -> PlanInfo:
        """``POST /builds/{id}/plans``: look up or create the build's plan
        for ``(deployment, settings)``, admitting ``roots`` first and
        unexpanded. An existing plan is returned as it is (its own id, not
        ``plan_id``)."""
        raise _missing(self, "plan_create")

    async def plan_create_aio(
        self,
        build_id: UUID,
        *,
        plan_id: UUID,
        deployment_id: UUID,
        settings: Mapping[str, str],
        roots: Sequence[RegistrationItem],
    ) -> PlanInfo:
        return self.plan_create(
            build_id,
            plan_id=plan_id,
            deployment_id=deployment_id,
            settings=settings,
            roots=roots,
        )

    def plan_register_members(
        self, plan_id: UUID, items: Sequence[RegistrationItem]
    ) -> MembersResult:
        """``POST /plans/{id}/members``: one chunk (at most 1000 items), in
        one transaction."""
        raise _missing(self, "plan_register_members")

    async def plan_register_members_aio(
        self, plan_id: UUID, items: Sequence[RegistrationItem]
    ) -> MembersResult:
        return self.plan_register_members(plan_id, items)

    def plan_seal(self, plan_id: UUID) -> PlanInfo:
        """``POST /plans/{id}/seal``: verify the static phase and seal (a
        replacement activates here)."""
        raise _missing(self, "plan_seal")

    async def plan_seal_aio(self, plan_id: UUID) -> PlanInfo:
        return self.plan_seal(plan_id)

    def plan_roots_info(self, plan_id: UUID) -> PlanRoots:
        """``GET /plans/{id}/roots``: the plan's scope and root members."""
        raise _missing(self, "plan_roots_info")

    def plan_roots(self, plan_id: UUID) -> list[FrontierMember]:
        """The plan's root members with their instance bodies (rollover)."""
        raise _missing(self, "plan_roots")

    async def plan_roots_aio(self, plan_id: UUID) -> list[FrontierMember]:
        return self.plan_roots(plan_id)

    def build_skip_blocked(self, build_id: UUID) -> list[str]:
        """``POST /builds/{id}/skip-blocked``: mark the active plan's members
        transitively blocked by a failed, cancelled or skipped upstream
        SKIPPED; returns their task ids."""
        raise _missing(self, "build_skip_blocked")

    async def build_skip_blocked_aio(self, build_id: UUID) -> list[str]:
        return self.build_skip_blocked(build_id)

    # -- member transitions ----------------------------------------------------

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
        """``POST /plans/{id}/members/{task_id}/start``.

        A **claiming** start takes the claim for ``execution_id`` (minted by
        the caller before the spawn) and is the decision: refused 409 with
        ``task_already_completed``, ``task_already_running``,
        ``upstream_incomplete``, ``member_excluded``, ``plan_superseded`` or
        ``concurrency_limit_reached``. A retried granted start is a no-op. A
        **non-claiming** start is the holder's own "I am running" report,
        with the executor details the claim could not know."""
        raise _missing(self, "member_start")

    async def member_start_aio(
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
        return self.member_start(
            plan_id,
            task_id,
            execution_id=execution_id,
            claim=claim,
            claim_ttl_seconds=claim_ttl_seconds,
            executor=executor,
            executor_ref=executor_ref,
            executor_metadata=executor_metadata,
            limit_keys=limit_keys,
        )

    def member_complete(
        self, plan_id: UUID, task_id: str, *, execution_id: UUID
    ) -> TransitionResult:
        raise _missing(self, "member_complete")

    async def member_complete_aio(
        self, plan_id: UUID, task_id: str, *, execution_id: UUID
    ) -> TransitionResult:
        return self.member_complete(plan_id, task_id, execution_id=execution_id)

    def member_fail(
        self,
        plan_id: UUID,
        task_id: str,
        *,
        execution_id: UUID,
        error_message: str | None = None,
    ) -> TransitionResult:
        raise _missing(self, "member_fail")

    async def member_fail_aio(
        self,
        plan_id: UUID,
        task_id: str,
        *,
        execution_id: UUID,
        error_message: str | None = None,
    ) -> TransitionResult:
        return self.member_fail(
            plan_id, task_id, execution_id=execution_id, error_message=error_message
        )

    def member_yield(
        self,
        plan_id: UUID,
        task_id: str,
        *,
        execution_id: UUID,
        deployment_id: UUID,
        batch_id: UUID,
        items: Sequence[RegistrationItem],
        yielded: Sequence[str],
        suspend: bool,
    ) -> YieldResult:
        """``POST /plans/{id}/members/{task_id}/yield``: one yield batch in
        one transaction — the children and their static closure land, the
        parent gets dynamic edges to ``yielded`` (instance hashes), and with
        ``suspend`` the parent is SUSPENDED and its claim released. A batch
        re-delivered under the same ``batch_id`` is replayed. Refused 409
        ``deployment_mismatch`` when ``deployment_id`` is not the plan's."""
        raise _missing(self, "member_yield")

    async def member_yield_aio(
        self,
        plan_id: UUID,
        task_id: str,
        *,
        execution_id: UUID,
        deployment_id: UUID,
        batch_id: UUID,
        items: Sequence[RegistrationItem],
        yielded: Sequence[str],
        suspend: bool,
    ) -> YieldResult:
        return self.member_yield(
            plan_id,
            task_id,
            execution_id=execution_id,
            deployment_id=deployment_id,
            batch_id=batch_id,
            items=items,
            yielded=yielded,
            suspend=suspend,
        )

    def member_retry(self, plan_id: UUID, task_id: str) -> TransitionResult:
        """Reset to PENDING (the fail mode's retry). Idempotent by state;
        refused 409 on COMPLETED and on a live claim."""
        raise _missing(self, "member_retry")

    async def member_retry_aio(self, plan_id: UUID, task_id: str) -> TransitionResult:
        return self.member_retry(plan_id, task_id)

    def member_interrupt(
        self,
        plan_id: UUID,
        task_id: str,
        *,
        execution_id: UUID,
        error_message: str | None = None,
    ) -> TransitionResult:
        """The platform ended the execution and nothing will restart it:
        INTERRUPTED (actionable), claim released."""
        raise _missing(self, "member_interrupt")

    def member_preempt(
        self, plan_id: UUID, task_id: str, *, execution_id: UUID
    ) -> TransitionResult:
        """The backend restarts the execution itself: no status change, the
        claim kept but due to lapse soon. Not an end: the restart reports
        under the same execution id, and its non-claiming start restores
        the claim's TTL."""
        raise _missing(self, "member_preempt")

    def member_cancel(self, plan_id: UUID, task_id: str) -> TransitionResult:
        """One task's cancel, by the build holding its claim (409
        ``not_claim_holder`` otherwise)."""
        raise _missing(self, "member_cancel")

    async def member_cancel_aio(self, plan_id: UUID, task_id: str) -> TransitionResult:
        return self.member_cancel(plan_id, task_id)

    def member_skip(self, plan_id: UUID, task_id: str) -> TransitionResult:
        """A member that cannot run because an upstream failed: SKIPPED. A
        scheduling decision naming no execution; 409 ``task_not_skippable``
        on FAILED / CANCELLED."""
        raise _missing(self, "member_skip")

    async def member_skip_aio(self, plan_id: UUID, task_id: str) -> TransitionResult:
        return self.member_skip(plan_id, task_id)

    def member_exclude(
        self, plan_id: UUID, task_id: str, *, reason: str | None = None
    ) -> ExclusionResult:
        """An operator gives up on a member in this plan. Cascades to its
        downstream closure; an excluded root fails the build. The global
        status is untouched."""
        raise _missing(self, "member_exclude")

    def member_discovery_failed(
        self, plan_id: UUID, task_id: str, *, error: str
    ) -> ExclusionResult:
        """The tick could not discover a member (its ``requires()`` raised,
        its body did not rehydrate): excluded as ``discovery_failed``, with
        the same cascade as :meth:`member_exclude`."""
        raise _missing(self, "member_discovery_failed")

    async def member_discovery_failed_aio(
        self, plan_id: UUID, task_id: str, *, error: str
    ) -> ExclusionResult:
        return self.member_discovery_failed(plan_id, task_id, error=error)

    # -- claims and executions ------------------------------------------------

    def claim_renew(
        self,
        task_id: str,
        *,
        execution_id: UUID,
        claim_ttl_seconds: int | None = None,
    ) -> TransitionResult:
        """``POST /tasks/{task_id}/claim/renew``: extend an in-process
        execution's claim (D11). Refused 409 ``claim_not_held`` unless
        ``execution_id`` holds the live claim."""
        raise _missing(self, "claim_renew")

    async def claim_renew_aio(
        self,
        task_id: str,
        *,
        execution_id: UUID,
        claim_ttl_seconds: int | None = None,
    ) -> TransitionResult:
        return self.claim_renew(
            task_id, execution_id=execution_id, claim_ttl_seconds=claim_ttl_seconds
        )

    def build_list_executions(
        self,
        build_id: UUID,
        *,
        not_in_current_plan: bool = False,
        include_ended: bool = False,
    ) -> list[ExecutionInfo]:
        """``GET /builds/{id}/executions``: the build's executions with no end
        reported (``builds stop``, a worker's cancellation checkpoint);
        ``not_in_current_plan`` keeps the orphans; ``include_ended`` lists
        the whole ledger (every execution granted, ended or not)."""
        raise _missing(self, "build_list_executions")

    def execution_report_stopped(
        self, execution_id: UUID, *, outcome: StopOutcome = "stopped"
    ) -> TransitionResult:
        """``POST /executions/{id}/stopped``: an operator ends an execution —
        ``stopped`` (its call was cancelled) or ``lost`` (it could not be,
        and no report of it will ever be applied). If it is the task's
        current execution with its claim unreleased, the claim is released
        ``cancelled`` and the task is CANCELLED (a revocation is not a
        result)."""
        raise _missing(self, "execution_report_stopped")

    # -- tasks ------------------------------------------------------------------

    def task_get(self, task_id: str) -> TaskInfo:
        """``GET /tasks/{task_id}``: a completion's identity and state, with
        its instances (each a construction under one scope), newest first."""
        raise _missing(self, "task_get")

    def task_list_artifacts(self, task_id: str) -> list[TaskArtifactInfo]:
        """``GET /tasks/{task_id}/artifacts``."""
        raise _missing(self, "task_list_artifacts")

    def task_upload_artifacts(
        self,
        plan_id: UUID,
        task_id: str,
        artifacts: "Sequence[Artifact]",
        *,
        execution_id: UUID | None = None,
    ) -> None:
        """``POST /plans/{plan_id}/members/{task_id}/artifacts``: upsert
        artifacts onto the task named by its membership of ``plan_id`` (404
        ``not_a_member`` otherwise). Artifacts belong to the task once
        uploaded, not the plan or execution — ``execution_id`` is
        informational only."""
        raise _missing(self, "task_upload_artifacts")

    async def task_upload_artifacts_aio(
        self,
        plan_id: UUID,
        task_id: str,
        artifacts: "Sequence[Artifact]",
        *,
        execution_id: UUID | None = None,
    ) -> None:
        self.task_upload_artifacts(
            plan_id, task_id, artifacts, execution_id=execution_id
        )

    # -- deployments and settings ----------------------------------------------

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
        """``POST /deployments``. A Modal deployment is created **before**
        its deploy (the server assigns ``generation``) and activated after;
        a local one is looked up or created by ``code_id`` and is born
        activated."""
        raise _missing(self, "deployment_create")

    async def deployment_create_aio(
        self,
        *,
        kind: DeploymentKind,
        code_id: str,
        deployment_id: UUID | None = None,
        app_name: str | None = None,
        image_id: str | None = None,
        modal_app_id: str | None = None,
    ) -> DeploymentInfo:
        return self.deployment_create(
            kind=kind,
            code_id=code_id,
            deployment_id=deployment_id,
            app_name=app_name,
            image_id=image_id,
            modal_app_id=modal_app_id,
        )

    def deployment_activate(
        self,
        deployment_id: UUID,
        *,
        modal_app_id: str | None = None,
        image_id: str | None = None,
    ) -> DeploymentInfo:
        """``POST /deployments/{id}/activate``, with what only the finished
        deploy knows (a given value fills a NULL or must match)."""
        raise _missing(self, "deployment_activate")

    def deployment_list(
        self,
        *,
        kind: DeploymentKind | None = None,
        app_name: str | None = None,
        current: bool = False,
        limit: int = 100,
    ) -> list[DeploymentInfo]:
        """Newest first. ``current=True`` keeps one row per app: its
        activated deployment with the highest generation."""
        raise _missing(self, "deployment_list")

    async def deployment_list_aio(
        self,
        *,
        kind: DeploymentKind | None = None,
        app_name: str | None = None,
        current: bool = False,
        limit: int = 100,
    ) -> list[DeploymentInfo]:
        return self.deployment_list(
            kind=kind, app_name=app_name, current=current, limit=limit
        )

    def settings_get(self, settings_hash: str) -> SettingsInfo:
        raise _missing(self, "settings_get")

    async def settings_get_aio(self, settings_hash: str) -> SettingsInfo:
        return self.settings_get(settings_hash)

    # -- concurrency limits ------------------------------------------------------

    def concurrency_limit_set(self, key: str, max_concurrent: int) -> None:
        """``PUT /concurrency-limits/{key}``: create or replace the cap on
        how many tasks carrying ``key`` may hold a live claim at once."""
        raise _missing(self, "concurrency_limit_set")

    def concurrency_limit_delete(self, key: str) -> None:
        """``DELETE /concurrency-limits/{key}`` (404 ``unknown_limit``)."""
        raise _missing(self, "concurrency_limit_delete")

    def concurrency_limit_list(self) -> dict[str, int]:
        """``GET /concurrency-limits``: key -> max_concurrent."""
        raise _missing(self, "concurrency_limit_list")

    # -- reactive scheduling -----------------------------------------------------

    def build_set_reactive_meta(
        self,
        build_id: UUID,
        *,
        app_name: str,
        tick_kwargs: dict[str, Any] | None = None,
    ) -> BuildInfo:
        """Mark the build reactively scheduled by ``app_name``; ``None``
        ``tick_kwargs`` keeps the stored configuration."""
        raise _missing(self, "build_set_reactive_meta")

    def build_notify(
        self, build_id: UUID, *, can_spawn: bool = True
    ) -> BuildNotifyResult:
        raise _missing(self, "build_notify")

    async def build_notify_aio(
        self, build_id: UUID, *, can_spawn: bool = True
    ) -> BuildNotifyResult:
        return self.build_notify(build_id, can_spawn=can_spawn)

    def build_get_notify(self, build_id: UUID) -> BuildNotifyResult:
        raise _missing(self, "build_get_notify")

    async def build_get_notify_aio(self, build_id: UUID) -> BuildNotifyResult:
        return self.build_get_notify(build_id)

    def build_clear_notify(self, build_id: UUID) -> None:
        raise _missing(self, "build_clear_notify")

    async def build_clear_notify_aio(self, build_id: UUID) -> None:
        self.build_clear_notify(build_id)

    def build_wake_candidates(self, limit: int = 20) -> list[WakeCandidate]:
        raise _missing(self, "build_wake_candidates")

    async def build_wake_candidates_aio(self, limit: int = 20) -> list[WakeCandidate]:
        return self.build_wake_candidates(limit)

    def scheduler_lease_acquire(
        self, build_id: UUID, *, owner_id: str, ttl_seconds: int
    ) -> SchedulerLeaseResult:
        raise _missing(self, "scheduler_lease_acquire")

    async def scheduler_lease_acquire_aio(
        self, build_id: UUID, *, owner_id: str, ttl_seconds: int
    ) -> SchedulerLeaseResult:
        return self.scheduler_lease_acquire(
            build_id, owner_id=owner_id, ttl_seconds=ttl_seconds
        )

    def scheduler_lease_renew(
        self, build_id: UUID, *, owner_id: str, ttl_seconds: int
    ) -> SchedulerLeaseResult:
        raise _missing(self, "scheduler_lease_renew")

    async def scheduler_lease_renew_aio(
        self, build_id: UUID, *, owner_id: str, ttl_seconds: int
    ) -> SchedulerLeaseResult:
        return self.scheduler_lease_renew(
            build_id, owner_id=owner_id, ttl_seconds=ttl_seconds
        )

    def scheduler_lease_release(
        self, build_id: UUID, *, owner_id: str
    ) -> SchedulerLeaseResult:
        """Drop the lease if ``owner_id`` still holds it; ``held`` reports
        whether it did (a lost tick cannot clear its successor's lease)."""
        raise _missing(self, "scheduler_lease_release")

    async def scheduler_lease_release_aio(
        self, build_id: UUID, *, owner_id: str
    ) -> SchedulerLeaseResult:
        return self.scheduler_lease_release(build_id, owner_id=owner_id)

    def build_report_tick_summary(
        self, build_id: UUID, summary: Mapping[str, Any]
    ) -> None:
        raise _missing(self, "build_report_tick_summary")

    async def build_report_tick_summary_aio(
        self, build_id: UUID, summary: Mapping[str, Any]
    ) -> None:
        self.build_report_tick_summary(build_id, summary)

    def build_list_tick_summaries(
        self, build_id: UUID, *, limit: int = 20
    ) -> list[TickSummaryRecord]:
        raise _missing(self, "build_list_tick_summaries")

    # -- lifecycle ---------------------------------------------------------------

    def close(self) -> None:
        """Release connections (a no-op for registries that hold none)."""

    async def aclose(self) -> None:
        self.close()


class NoOpRegistry(RegistryABC):
    """No registry configured.

    The engines recognise it by exact type and make no registry call at all
    (D11: a single-process build without a registry has no plan and no
    claims). Its methods raise, so a code path that reaches one by mistake
    fails loudly rather than pretending to have recorded something.
    """


def is_noop_registry(registry: RegistryABC) -> bool:
    """Whether ``registry`` is the do-nothing default (exact type: a
    subclass is a double that means to be called)."""
    return type(registry) is NoOpRegistry


def init_registry() -> RegistryABC:
    """The configured registry: an :class:`APIRegistry` when a registry is
    configured, :class:`NoOpRegistry` otherwise."""
    from stardag.config import config_provider
    from stardag.registry._api_registry import APIRegistry

    if config_provider.get().registry is not None:
        return APIRegistry()
    return NoOpRegistry()


registry_provider = resource_provider(RegistryABC, init_registry)


@lru_cache
def get_git_commit_hash() -> str:
    """The short SHA of the current Git commit (``-dirty`` if uncommitted
    changes), or ``SHORT_SHA`` / ``COMMIT_HASH`` from the environment."""
    for env_var in ("SHORT_SHA", "COMMIT_HASH"):
        short_sha = os.environ.get(env_var)
        if short_sha:
            return short_sha
    try:
        short_sha = (
            subprocess.check_output(
                ["git", "rev-parse", "--short", "HEAD"], stderr=subprocess.DEVNULL
            )
            .strip()
            .decode("utf-8")
        )
        dirty = subprocess.check_output(
            ["git", "status", "--porcelain"], stderr=subprocess.DEVNULL
        ).strip()
        return short_sha + "-dirty" if dirty else short_sha
    except subprocess.CalledProcessError:
        raise RuntimeError(
            "Unable to get Git commit short SHA, you need to either run in an "
            "environment where git is available or set one of the env vars "
            "SHORT_SHA or COMMIT_HASH."
        )
