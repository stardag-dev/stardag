"""Request builders of the v2 registry routes, one per route (see
:mod:`stardag.registry._api_http`). :class:`~stardag.registry.APIRegistry`
sends each through its sync and async transport, so the two methods of one
route cannot drift apart.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any
from uuid import UUID

from stardag.registry._api_http import Request
from stardag.registry._models import (
    BuildFrontier,
    BuildInfo,
    BuildNotifyResult,
    DeploymentInfo,
    DeploymentKind,
    ExclusionResult,
    ExecutionInfo,
    MembersResult,
    PlanInfo,
    PlanRoots,
    RegistrationItem,
    ResumeResult,
    SchedulerLeaseResult,
    SettingsInfo,
    StopOutcome,
    TaskArtifactInfo,
    TransitionResult,
    WakeCandidate,
    YieldResult,
)

if TYPE_CHECKING:
    from stardag.artifact import Artifact


def _drop_none(body: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in body.items() if v is not None}


def _items(items: Sequence[RegistrationItem]) -> list[dict[str, Any]]:
    return [item.wire() for item in items]


def _artifacts_body(artifacts: "Sequence[Artifact]") -> list[dict[str, Any]]:
    body: list[dict[str, Any]] = []
    for artifact in artifacts:
        data = artifact.model_dump(mode="json")
        if artifact.type == "markdown":
            data["body"] = {"content": data["body"]}
        body.append(data)
    return body


# -----------------------------------------------------------------------------
# Request builders, one per route
# -----------------------------------------------------------------------------


def _build_create_req(
    root_task_ids: Sequence[str],
    build_id: UUID | None,
    name: str | None,
    description: str | None,
    executor_metadata: dict[str, Any] | None,
) -> Request[BuildInfo]:
    return Request(
        "POST",
        "/builds",
        BuildInfo.model_validate,
        json=_drop_none(
            {
                "id": str(build_id) if build_id else None,
                "name": name,
                "description": description,
                "root_task_ids": sorted(set(root_task_ids)),
                "executor_metadata": executor_metadata,
            }
        ),
        operation="Create build",
    )


def _build_get_req(build_id: UUID) -> Request[BuildInfo]:
    return Request(
        "GET", f"/builds/{build_id}", BuildInfo.model_validate, operation="Get build"
    )


def _build_resume_req(
    build_id: UUID,
    deployment_id: UUID | None,
    settings: Mapping[str, str] | None,
    executor_metadata: dict[str, Any] | None,
) -> Request[ResumeResult]:
    return Request(
        "POST",
        f"/builds/{build_id}/resume",
        ResumeResult.model_validate,
        json=_drop_none(
            {
                "deployment_id": str(deployment_id) if deployment_id else None,
                "settings": dict(settings or {}),
                "executor_metadata": executor_metadata,
            }
        ),
        operation=f"Resume build {build_id}",
    )


def _build_transition_req(
    build_id: UUID, action: str, body: dict[str, Any] | None = None
) -> Request[BuildInfo]:
    return Request(
        "POST",
        f"/builds/{build_id}/{action}",
        BuildInfo.model_validate,
        json=body,
        operation=f"{action} build {build_id}",
    )


def _frontier_req(build_id: UUID) -> Request[BuildFrontier]:
    return Request(
        "GET",
        f"/builds/{build_id}/frontier",
        BuildFrontier.model_validate,
        operation=f"Get frontier of build {build_id}",
    )


def _plan_create_req(
    build_id: UUID,
    plan_id: UUID,
    deployment_id: UUID,
    settings: Mapping[str, str],
    roots: Sequence[RegistrationItem],
) -> Request[PlanInfo]:
    return Request(
        "POST",
        f"/builds/{build_id}/plans",
        PlanInfo.model_validate,
        json={
            "plan_id": str(plan_id),
            "deployment_id": str(deployment_id),
            "settings": dict(settings),
            "roots": _items(roots),
        },
        operation=f"Create plan for build {build_id}",
    )


def _members_req(
    plan_id: UUID, items: Sequence[RegistrationItem]
) -> Request[MembersResult]:
    return Request(
        "POST",
        f"/plans/{plan_id}/members",
        MembersResult.model_validate,
        json={"items": _items(items)},
        operation=f"Register {len(items)} member(s) of plan {plan_id}",
    )


def _seal_req(plan_id: UUID) -> Request[PlanInfo]:
    return Request(
        "POST",
        f"/plans/{plan_id}/seal",
        PlanInfo.model_validate,
        operation=f"Seal plan {plan_id}",
    )


def _plan_roots_req(plan_id: UUID) -> Request[PlanRoots]:
    return Request(
        "GET",
        f"/plans/{plan_id}/roots",
        PlanRoots.model_validate,
        operation=f"Roots of plan {plan_id}",
    )


def _skip_blocked_req(build_id: UUID) -> Request[list[str]]:
    def parse(payload: Any) -> list[str]:
        return [str(t) for t in (payload or {}).get("skipped", [])]

    return Request(
        "POST",
        f"/builds/{build_id}/skip-blocked",
        parse,
        operation=f"Skip blocked members of build {build_id}",
    )


def _exclude_req(
    plan_id: UUID, task_id: str, reason: str | None
) -> Request[ExclusionResult]:
    return Request(
        "POST",
        f"/plans/{plan_id}/members/{task_id}/exclude",
        ExclusionResult.model_validate,
        json=_drop_none({"reason": reason}),
        operation=f"Exclude task {task_id}",
    )


def _discovery_failed_req(
    plan_id: UUID, task_id: str, error: str
) -> Request[ExclusionResult]:
    return Request(
        "POST",
        f"/plans/{plan_id}/members/{task_id}/discovery-failed",
        ExclusionResult.model_validate,
        json={"error": error or "discovery failed"},
        operation=f"Report discovery failure of task {task_id}",
    )


def _executions_req(
    build_id: UUID, not_in_current_plan: bool, include_ended: bool = False
) -> Request[list[ExecutionInfo]]:
    def parse(payload: Any) -> list[ExecutionInfo]:
        return [
            ExecutionInfo.model_validate(e)
            for e in (payload or {}).get("executions", [])
        ]

    return Request(
        "GET",
        f"/builds/{build_id}/executions",
        parse,
        params={
            **({"not_in_current_plan": "true"} if not_in_current_plan else {}),
            **({"include_ended": "true"} if include_ended else {}),
        },
        operation=f"List executions of build {build_id}",
    )


def _task_artifacts_req(task_id: str) -> Request[list[TaskArtifactInfo]]:
    def parse(payload: Any) -> list[TaskArtifactInfo]:
        return [
            TaskArtifactInfo.model_validate(a)
            for a in (payload or {}).get("artifacts", [])
        ]

    return Request(
        "GET",
        f"/tasks/{task_id}/artifacts",
        parse,
        operation=f"List artifacts of task {task_id}",
    )


def _stopped_req(execution_id: UUID, outcome: StopOutcome) -> Request[TransitionResult]:
    return Request(
        "POST",
        f"/executions/{execution_id}/stopped",
        TransitionResult.model_validate,
        json={"outcome": outcome},
        operation=f"Report execution {execution_id} {outcome}",
    )


def _member_req(
    plan_id: UUID, task_id: str, action: str, body: dict[str, Any] | None = None
) -> Request[TransitionResult]:
    return Request(
        "POST",
        f"/plans/{plan_id}/members/{task_id}/{action}",
        TransitionResult.model_validate,
        json=body,
        operation=f"{action} task {task_id}",
    )


def _start_req(
    plan_id: UUID,
    task_id: str,
    execution_id: UUID,
    claim: bool,
    claim_ttl_seconds: int | None,
    executor: str | None,
    executor_ref: str | None,
    executor_metadata: dict[str, Any] | None,
    limit_keys: Sequence[str],
) -> Request[TransitionResult]:
    return _member_req(
        plan_id,
        task_id,
        "start",
        _drop_none(
            {
                "execution_id": str(execution_id),
                "claim": claim,
                "claim_ttl_seconds": claim_ttl_seconds,
                "executor": executor,
                "executor_ref": executor_ref,
                "executor_metadata": executor_metadata,
                "limit_keys": sorted(set(limit_keys)) if limit_keys else None,
            }
        ),
    )


def _yield_req(
    plan_id: UUID,
    task_id: str,
    execution_id: UUID,
    deployment_id: UUID,
    batch_id: UUID,
    items: Sequence[RegistrationItem],
    yielded: Sequence[str],
    suspend: bool,
) -> Request[YieldResult]:
    return Request(
        "POST",
        f"/plans/{plan_id}/members/{task_id}/yield",
        YieldResult.model_validate,
        json={
            "execution_id": str(execution_id),
            "deployment_id": str(deployment_id),
            "batch_id": str(batch_id),
            "items": _items(items),
            "yielded": list(yielded),
            "suspend": suspend,
        },
        operation=f"Yield from task {task_id}",
    )


def _renew_req(
    task_id: str, execution_id: UUID, claim_ttl_seconds: int | None
) -> Request[TransitionResult]:
    return Request(
        "POST",
        f"/tasks/{task_id}/claim/renew",
        TransitionResult.model_validate,
        json=_drop_none(
            {
                "execution_id": str(execution_id),
                "claim_ttl_seconds": claim_ttl_seconds,
            }
        ),
        operation=f"Renew claim on task {task_id}",
    )


def _deployment_create_req(
    kind: DeploymentKind,
    code_id: str,
    deployment_id: UUID | None,
    app_name: str | None,
    image_id: str | None,
    modal_app_id: str | None,
) -> Request[DeploymentInfo]:
    return Request(
        "POST",
        "/deployments",
        DeploymentInfo.model_validate,
        json=_drop_none(
            {
                "id": str(deployment_id) if deployment_id else None,
                "kind": kind,
                "app_name": app_name,
                "code_id": code_id,
                "image_id": image_id,
                "modal_app_id": modal_app_id,
            }
        ),
        operation="Create deployment",
    )


def _deployment_list_req(
    kind: DeploymentKind | None, app_name: str | None, current: bool, limit: int
) -> Request[list[DeploymentInfo]]:
    params: dict[str, str] = {"limit": str(limit)}
    if kind is not None:
        params["kind"] = kind
    if app_name is not None:
        params["app_name"] = app_name
    if current:
        params["current"] = "true"

    def parse(payload: Any) -> list[DeploymentInfo]:
        return [
            DeploymentInfo.model_validate(d)
            for d in (payload or {}).get("deployments", [])
        ]

    return Request(
        "GET", "/deployments", parse, params=params, operation="List deployments"
    )


def _settings_req(settings_hash: str) -> Request[SettingsInfo]:
    return Request(
        "GET",
        f"/settings/{settings_hash}",
        SettingsInfo.model_validate,
        operation=f"Get settings {settings_hash}",
    )


def _notify_req(
    method: str, build_id: UUID, **params: str
) -> Request[BuildNotifyResult]:
    return Request(
        method,
        f"/builds/{build_id}/notify",
        BuildNotifyResult.model_validate,
        params=dict(params),
        operation=f"{method} notify of build {build_id}",
    )


def _wake_candidates_req(limit: int) -> Request[list[WakeCandidate]]:
    def parse(payload: Any) -> list[WakeCandidate]:
        return [
            WakeCandidate.model_validate(b) for b in (payload or {}).get("builds", [])
        ]

    return Request(
        "POST",
        "/builds/wake-candidates",
        parse,
        params={"limit": str(limit)},
        operation="Wake candidates",
    )


def _lease_req(
    method: str, build_id: UUID, owner_id: str, ttl_seconds: int | None = None
) -> Request[SchedulerLeaseResult]:
    params = {"owner_id": owner_id}
    if ttl_seconds is not None:
        params["ttl_seconds"] = str(ttl_seconds)
    return Request(
        method,
        f"/builds/{build_id}/scheduler-lease",
        SchedulerLeaseResult.model_validate,
        params=params,
        operation=f"{method} scheduler lease of build {build_id}",
    )
