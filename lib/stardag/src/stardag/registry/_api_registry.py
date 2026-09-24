"""The v2 registry client over HTTP (``/api/v2``).

Each route is described once, as a :class:`~stardag.registry._api_http.Request`
built by a ``_*_req`` function (:mod:`stardag.registry._api_routes`), and
exposed as a sync method and an ``_aio`` method that send the same request.

Routes marked **(assumed)** are not served by the registry yet; they are
coded to the shape ``docs/design/registry-v2/design.md`` implies and listed
as open server-contract items in the I7 status of ``plan.md``.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any
from uuid import UUID

from stardag.registry._api_http import HTTPTransport, Request
from stardag.registry._api_routes import (
    _artifacts_body,
    _build_create_req,
    _build_list_req,
    _build_get_req,
    _build_resume_req,
    _build_transition_req,
    _deployment_create_req,
    _deployment_list_req,
    _discovery_failed_req,
    _drop_none,
    _exclude_req,
    _executions_req,
    _frontier_req,
    _lease_req,
    _member_req,
    _members_req,
    _notify_req,
    _plan_create_req,
    _plan_roots_req,
    _renew_req,
    _seal_req,
    _settings_req,
    _skip_blocked_req,
    _start_req,
    _stopped_req,
    _task_artifacts_req,
    _wake_candidates_req,
    _yield_req,
)
from stardag.registry._base import RegistryABC
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

if TYPE_CHECKING:
    from stardag.artifact import Artifact

logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# The client
# -----------------------------------------------------------------------------


class APIRegistry(HTTPTransport, RegistryABC):
    """The v2 registry over HTTP.

    Stateless with respect to builds (every call names its build, plan or
    task), so one instance serves many builds — it is a process-wide
    singleton through ``registry_provider``.

    Authentication: an API key (explicit, or ``STARDAG_API_KEY``) or the
    browser-login JWT of the active profile.
    """

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
        return self.call(
            _build_create_req(
                root_task_ids, build_id, name, description, executor_metadata
            )
        )

    async def build_create_aio(
        self,
        *,
        root_task_ids: Sequence[str],
        build_id: UUID | None = None,
        name: str | None = None,
        description: str | None = None,
        executor_metadata: dict[str, Any] | None = None,
    ) -> BuildInfo:
        return await self.acall(
            _build_create_req(
                root_task_ids, build_id, name, description, executor_metadata
            )
        )

    def build_get(self, build_id: UUID) -> BuildInfo:
        return self.call(_build_get_req(build_id))

    async def build_get_aio(self, build_id: UUID) -> BuildInfo:
        return await self.acall(_build_get_req(build_id))

    def build_resume(
        self,
        build_id: UUID,
        *,
        deployment_id: UUID | None = None,
        settings: Mapping[str, str] | None = None,
        executor_metadata: dict[str, Any] | None = None,
    ) -> ResumeResult:
        return self.call(
            _build_resume_req(build_id, deployment_id, settings, executor_metadata)
        )

    async def build_resume_aio(
        self,
        build_id: UUID,
        *,
        deployment_id: UUID | None = None,
        settings: Mapping[str, str] | None = None,
        executor_metadata: dict[str, Any] | None = None,
    ) -> ResumeResult:
        return await self.acall(
            _build_resume_req(build_id, deployment_id, settings, executor_metadata)
        )

    def build_complete(self, build_id: UUID, *, force: bool = False) -> BuildInfo:
        return self.call(_build_transition_req(build_id, "complete", {"force": force}))

    async def build_complete_aio(
        self, build_id: UUID, *, force: bool = False
    ) -> BuildInfo:
        return await self.acall(
            _build_transition_req(build_id, "complete", {"force": force})
        )

    def build_fail(self, build_id: UUID, error_message: str | None = None) -> BuildInfo:
        return self.call(
            _build_transition_req(build_id, "fail", {"error_message": error_message})
        )

    async def build_fail_aio(
        self, build_id: UUID, error_message: str | None = None
    ) -> BuildInfo:
        return await self.acall(
            _build_transition_req(build_id, "fail", {"error_message": error_message})
        )

    def build_cancel(self, build_id: UUID) -> BuildInfo:
        return self.call(_build_transition_req(build_id, "cancel"))

    async def build_cancel_aio(self, build_id: UUID) -> BuildInfo:
        return await self.acall(_build_transition_req(build_id, "cancel"))

    def build_exit_early(self, build_id: UUID) -> BuildInfo:
        return self.call(_build_transition_req(build_id, "exit-early"))

    async def build_exit_early_aio(self, build_id: UUID) -> BuildInfo:
        return await self.acall(_build_transition_req(build_id, "exit-early"))

    def build_list(
        self,
        *,
        status: str | None = None,
        reactive_app_name: str | None = None,
        limit: int = 100,
    ) -> list[BuildInfo]:
        return self.call(_build_list_req(status, reactive_app_name, limit))

    def build_list_running(
        self, *, reactive_app_name: str | None = None, limit: int = 100
    ) -> list[UUID]:
        builds = self.build_list(
            status="running", reactive_app_name=reactive_app_name, limit=limit
        )
        return [b.id for b in builds]

    def build_get_frontier(self, build_id: UUID) -> BuildFrontier:
        return self.call(_frontier_req(build_id))

    async def build_get_frontier_aio(self, build_id: UUID) -> BuildFrontier:
        return await self.acall(_frontier_req(build_id))

    # -- plans ------------------------------------------------------------------

    def plan_create(
        self,
        build_id: UUID,
        *,
        plan_id: UUID,
        deployment_id: UUID,
        settings: Mapping[str, str],
        roots: Sequence[RegistrationItem],
    ) -> PlanInfo:
        return self.call(
            _plan_create_req(build_id, plan_id, deployment_id, settings, roots)
        )

    async def plan_create_aio(
        self,
        build_id: UUID,
        *,
        plan_id: UUID,
        deployment_id: UUID,
        settings: Mapping[str, str],
        roots: Sequence[RegistrationItem],
    ) -> PlanInfo:
        return await self.acall(
            _plan_create_req(build_id, plan_id, deployment_id, settings, roots)
        )

    def plan_register_members(
        self, plan_id: UUID, items: Sequence[RegistrationItem]
    ) -> MembersResult:
        return self.call(_members_req(plan_id, items))

    async def plan_register_members_aio(
        self, plan_id: UUID, items: Sequence[RegistrationItem]
    ) -> MembersResult:
        return await self.acall(_members_req(plan_id, items))

    def plan_seal(self, plan_id: UUID) -> PlanInfo:
        return self.call(_seal_req(plan_id))

    async def plan_seal_aio(self, plan_id: UUID) -> PlanInfo:
        return await self.acall(_seal_req(plan_id))

    def plan_roots_info(self, plan_id: UUID) -> PlanRoots:
        return self.call(_plan_roots_req(plan_id))

    def plan_roots(self, plan_id: UUID) -> list[FrontierMember]:
        return self.plan_roots_info(plan_id).roots

    async def plan_roots_aio(self, plan_id: UUID) -> list[FrontierMember]:
        return (await self.acall(_plan_roots_req(plan_id))).roots

    def build_skip_blocked(self, build_id: UUID) -> list[str]:
        return self.call(_skip_blocked_req(build_id))

    async def build_skip_blocked_aio(self, build_id: UUID) -> list[str]:
        return await self.acall(_skip_blocked_req(build_id))

    # -- member transitions -------------------------------------------------------

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
        return self.call(
            _start_req(
                plan_id,
                task_id,
                execution_id,
                claim,
                claim_ttl_seconds,
                executor,
                executor_ref,
                executor_metadata,
                limit_keys,
            )
        )

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
        return await self.acall(
            _start_req(
                plan_id,
                task_id,
                execution_id,
                claim,
                claim_ttl_seconds,
                executor,
                executor_ref,
                executor_metadata,
                limit_keys,
            )
        )

    def member_complete(
        self, plan_id: UUID, task_id: str, *, execution_id: UUID
    ) -> TransitionResult:
        return self.call(
            _member_req(
                plan_id, task_id, "complete", {"execution_id": str(execution_id)}
            )
        )

    async def member_complete_aio(
        self, plan_id: UUID, task_id: str, *, execution_id: UUID
    ) -> TransitionResult:
        return await self.acall(
            _member_req(
                plan_id, task_id, "complete", {"execution_id": str(execution_id)}
            )
        )

    def member_fail(
        self,
        plan_id: UUID,
        task_id: str,
        *,
        execution_id: UUID,
        error_message: str | None = None,
    ) -> TransitionResult:
        return self.call(
            _member_req(
                plan_id,
                task_id,
                "fail",
                {"execution_id": str(execution_id), "error_message": error_message},
            )
        )

    async def member_fail_aio(
        self,
        plan_id: UUID,
        task_id: str,
        *,
        execution_id: UUID,
        error_message: str | None = None,
    ) -> TransitionResult:
        return await self.acall(
            _member_req(
                plan_id,
                task_id,
                "fail",
                {"execution_id": str(execution_id), "error_message": error_message},
            )
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
        return self.call(
            _yield_req(
                plan_id,
                task_id,
                execution_id,
                deployment_id,
                batch_id,
                items,
                yielded,
                suspend,
            )
        )

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
        return await self.acall(
            _yield_req(
                plan_id,
                task_id,
                execution_id,
                deployment_id,
                batch_id,
                items,
                yielded,
                suspend,
            )
        )

    def member_retry(self, plan_id: UUID, task_id: str) -> TransitionResult:
        return self.call(_member_req(plan_id, task_id, "retry"))

    async def member_retry_aio(self, plan_id: UUID, task_id: str) -> TransitionResult:
        return await self.acall(_member_req(plan_id, task_id, "retry"))

    def member_interrupt(
        self,
        plan_id: UUID,
        task_id: str,
        *,
        execution_id: UUID,
        error_message: str | None = None,
    ) -> TransitionResult:
        return self.call(
            _member_req(
                plan_id,
                task_id,
                "interrupt",
                _drop_none(
                    {"execution_id": str(execution_id), "error_message": error_message}
                ),
            )
        )

    def member_preempt(
        self, plan_id: UUID, task_id: str, *, execution_id: UUID
    ) -> TransitionResult:
        return self.call(
            _member_req(
                plan_id, task_id, "preempt", {"execution_id": str(execution_id)}
            )
        )

    def member_cancel(self, plan_id: UUID, task_id: str) -> TransitionResult:
        return self.call(_member_req(plan_id, task_id, "cancel"))

    async def member_cancel_aio(self, plan_id: UUID, task_id: str) -> TransitionResult:
        return await self.acall(_member_req(plan_id, task_id, "cancel"))

    def member_skip(self, plan_id: UUID, task_id: str) -> TransitionResult:
        return self.call(_member_req(plan_id, task_id, "skip"))

    async def member_skip_aio(self, plan_id: UUID, task_id: str) -> TransitionResult:
        return await self.acall(_member_req(plan_id, task_id, "skip"))

    def member_exclude(
        self, plan_id: UUID, task_id: str, *, reason: str | None = None
    ) -> ExclusionResult:
        return self.call(_exclude_req(plan_id, task_id, reason))

    def member_discovery_failed(
        self, plan_id: UUID, task_id: str, *, error: str
    ) -> ExclusionResult:
        return self.call(_discovery_failed_req(plan_id, task_id, error))

    async def member_discovery_failed_aio(
        self, plan_id: UUID, task_id: str, *, error: str
    ) -> ExclusionResult:
        return await self.acall(_discovery_failed_req(plan_id, task_id, error))

    # -- claims, executions, tasks ---------------------------------------------------

    def claim_renew(
        self,
        task_id: str,
        *,
        execution_id: UUID,
        claim_ttl_seconds: int | None = None,
    ) -> TransitionResult:
        return self.call(_renew_req(task_id, execution_id, claim_ttl_seconds))

    async def claim_renew_aio(
        self,
        task_id: str,
        *,
        execution_id: UUID,
        claim_ttl_seconds: int | None = None,
    ) -> TransitionResult:
        return await self.acall(_renew_req(task_id, execution_id, claim_ttl_seconds))

    def build_list_executions(
        self, build_id: UUID, *, not_in_current_plan: bool = False
    ) -> list[ExecutionInfo]:
        return self.call(_executions_req(build_id, not_in_current_plan))

    def execution_report_stopped(
        self, execution_id: UUID, *, outcome: StopOutcome = "stopped"
    ) -> TransitionResult:
        return self.call(_stopped_req(execution_id, outcome))

    def task_get(self, task_id: str) -> TaskInfo:
        return self.call(
            Request(
                "GET",
                f"/tasks/{task_id}",
                TaskInfo.model_validate,
                operation=f"Get task {task_id}",
            )
        )

    def task_list_artifacts(self, task_id: str) -> list[TaskArtifactInfo]:
        return self.call(_task_artifacts_req(task_id))

    def _artifacts_req(
        self,
        plan_id: UUID,
        task_id: str,
        artifacts: "Sequence[Artifact]",
        execution_id: UUID | None,
    ) -> Request[None]:
        return Request(
            "POST",
            f"/plans/{plan_id}/members/{task_id}/artifacts",
            lambda _payload: None,
            json={
                "execution_id": str(execution_id) if execution_id else None,
                "artifacts": _artifacts_body(artifacts),
            },
            operation=f"Upload artifacts of task {task_id}",
        )

    def task_upload_artifacts(
        self,
        plan_id: UUID,
        task_id: str,
        artifacts: "Sequence[Artifact]",
        *,
        execution_id: UUID | None = None,
    ) -> None:
        if artifacts:
            self.call(self._artifacts_req(plan_id, task_id, artifacts, execution_id))

    async def task_upload_artifacts_aio(
        self,
        plan_id: UUID,
        task_id: str,
        artifacts: "Sequence[Artifact]",
        *,
        execution_id: UUID | None = None,
    ) -> None:
        if artifacts:
            await self.acall(
                self._artifacts_req(plan_id, task_id, artifacts, execution_id)
            )

    # -- deployments and settings ---------------------------------------------------

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
        return self.call(
            _deployment_create_req(
                kind, code_id, deployment_id, app_name, image_id, modal_app_id
            )
        )

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
        return await self.acall(
            _deployment_create_req(
                kind, code_id, deployment_id, app_name, image_id, modal_app_id
            )
        )

    def deployment_activate(self, deployment_id: UUID) -> DeploymentInfo:
        return self.call(
            Request(
                "POST",
                f"/deployments/{deployment_id}/activate",
                DeploymentInfo.model_validate,
                operation=f"Activate deployment {deployment_id}",
            )
        )

    def deployment_list(
        self,
        *,
        kind: DeploymentKind | None = None,
        app_name: str | None = None,
        current: bool = False,
        limit: int = 100,
    ) -> list[DeploymentInfo]:
        return self.call(_deployment_list_req(kind, app_name, current, limit))

    async def deployment_list_aio(
        self,
        *,
        kind: DeploymentKind | None = None,
        app_name: str | None = None,
        current: bool = False,
        limit: int = 100,
    ) -> list[DeploymentInfo]:
        return await self.acall(_deployment_list_req(kind, app_name, current, limit))

    def settings_get(self, settings_hash: str) -> SettingsInfo:
        return self.call(_settings_req(settings_hash))

    async def settings_get_aio(self, settings_hash: str) -> SettingsInfo:
        return await self.acall(_settings_req(settings_hash))

    # -- reactive scheduling ---------------------------------------------------------

    def build_set_reactive_meta(
        self,
        build_id: UUID,
        *,
        app_name: str,
        tick_kwargs: dict[str, Any] | None = None,
    ) -> BuildInfo:
        return self.call(
            Request(
                "PUT",
                f"/builds/{build_id}/reactive-meta",
                BuildInfo.model_validate,
                json=_drop_none({"app_name": app_name, "tick_kwargs": tick_kwargs}),
                operation=f"Set reactive meta of build {build_id}",
            )
        )

    def build_notify(
        self, build_id: UUID, *, can_spawn: bool = True
    ) -> BuildNotifyResult:
        return self.call(
            _notify_req("POST", build_id, can_spawn=str(can_spawn).lower())
        )

    async def build_notify_aio(
        self, build_id: UUID, *, can_spawn: bool = True
    ) -> BuildNotifyResult:
        return await self.acall(
            _notify_req("POST", build_id, can_spawn=str(can_spawn).lower())
        )

    def build_get_notify(self, build_id: UUID) -> BuildNotifyResult:
        return self.call(_notify_req("GET", build_id))

    async def build_get_notify_aio(self, build_id: UUID) -> BuildNotifyResult:
        return await self.acall(_notify_req("GET", build_id))

    def build_clear_notify(self, build_id: UUID) -> None:
        self.call(_notify_req("DELETE", build_id))

    async def build_clear_notify_aio(self, build_id: UUID) -> None:
        await self.acall(_notify_req("DELETE", build_id))

    def build_wake_candidates(self, limit: int = 20) -> list[WakeCandidate]:
        return self.call(_wake_candidates_req(limit))

    async def build_wake_candidates_aio(self, limit: int = 20) -> list[WakeCandidate]:
        return await self.acall(_wake_candidates_req(limit))

    def scheduler_lease_acquire(
        self, build_id: UUID, *, owner_id: str, ttl_seconds: int
    ) -> SchedulerLeaseResult:
        return self.call(_lease_req("POST", build_id, owner_id, ttl_seconds))

    async def scheduler_lease_acquire_aio(
        self, build_id: UUID, *, owner_id: str, ttl_seconds: int
    ) -> SchedulerLeaseResult:
        return await self.acall(_lease_req("POST", build_id, owner_id, ttl_seconds))

    def scheduler_lease_renew(
        self, build_id: UUID, *, owner_id: str, ttl_seconds: int
    ) -> SchedulerLeaseResult:
        return self.call(_lease_req("PUT", build_id, owner_id, ttl_seconds))

    async def scheduler_lease_renew_aio(
        self, build_id: UUID, *, owner_id: str, ttl_seconds: int
    ) -> SchedulerLeaseResult:
        return await self.acall(_lease_req("PUT", build_id, owner_id, ttl_seconds))

    def scheduler_lease_release(self, build_id: UUID, *, owner_id: str) -> None:
        self.call(_lease_req("DELETE", build_id, owner_id))

    async def scheduler_lease_release_aio(
        self, build_id: UUID, *, owner_id: str
    ) -> None:
        await self.acall(_lease_req("DELETE", build_id, owner_id))

    def _tick_summary_req(
        self, build_id: UUID, summary: Mapping[str, Any]
    ) -> Request[None]:
        return Request(
            "POST",
            f"/builds/{build_id}/tick-summaries",
            lambda _payload: None,
            json=dict(summary),
            operation=f"Report tick summary of build {build_id}",
        )

    def build_report_tick_summary(
        self, build_id: UUID, summary: Mapping[str, Any]
    ) -> None:
        self.call(self._tick_summary_req(build_id, summary))

    async def build_report_tick_summary_aio(
        self, build_id: UUID, summary: Mapping[str, Any]
    ) -> None:
        await self.acall(self._tick_summary_req(build_id, summary))

    def build_list_tick_summaries(
        self, build_id: UUID, *, limit: int = 20
    ) -> list[TickSummaryRecord]:
        def parse(payload: Any) -> list[TickSummaryRecord]:
            return [
                TickSummaryRecord.model_validate(s)
                for s in (payload or {}).get("summaries", [])
            ]

        return self.call(
            Request(
                "GET",
                f"/builds/{build_id}/tick-summaries",
                parse,
                params={"limit": str(limit)},
                operation=f"List tick summaries of build {build_id}",
            )
        )
