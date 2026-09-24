"""``/api/v2`` routes of a plan: registration (members, seal), the reads
(the plan, its roots, its graph), and every transition of a member — the
execution reports, the scheduling decisions, ``/yield``, exclusion and
artifact upload.

Thin by rule: parse, resolve the environment from the credentials, call
one service, convert its result.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.auth import SdkAuth
from stardag_api.models import ExclusionReason
from stardag_api.routes.registry_v2._common import Auth, Db, artifact_list
from stardag_api.schemas_v2 import (
    DiscoveryFailedRequest,
    ExcludeRequest,
    ExclusionResponse,
    FailRequest,
    MembersRequest,
    MembersResponse,
    PlanResponse,
    ReportRequest,
    StartRequest,
    TransitionResponse,
    YieldRequest,
    YieldResponse,
)
from stardag_api.schemas_v2_reads import (
    ArtifactUploadRequest,
    PlanDetailResponse,
    PlanGraphResponse,
    PlanRootsResponse,
    TaskArtifactListResponse,
)
from stardag_api.services import (
    artifacts,
    exclusion,
    plan_reads,
    plans,
    registration,
    transitions,
    yields,
)
from stardag_api.services.transitions import Transition

router = APIRouter(tags=["registry-v2"])


async def _transition(
    db: AsyncSession, auth: SdkAuth, plan_id: UUID, task_id: str, t: Transition
) -> transitions.TransitionOutcome:
    return await transitions.apply_member_transition(
        db, auth.environment_id, plan_id=plan_id, task_id=task_id, transition=t
    )


@router.post("/plans/{plan_id}/members", response_model=MembersResponse)
async def register_members(plan_id: UUID, body: MembersRequest, db: Db, auth: Auth):
    return await registration.register_members(
        db, auth.environment_id, plan_id, body.items
    )


@router.post("/plans/{plan_id}/seal", response_model=PlanResponse)
async def seal_plan(plan_id: UUID, db: Db, auth: Auth):
    return await plans.seal_plan(db, auth.environment_id, plan_id)


@router.post(
    "/plans/{plan_id}/members/{task_id}/start", response_model=TransitionResponse
)
async def start(plan_id: UUID, task_id: str, body: StartRequest, db: Db, auth: Auth):
    return await _transition(
        db,
        auth,
        plan_id,
        task_id,
        Transition.start(
            body.execution_id,
            claim=body.claim,
            claim_ttl_seconds=body.claim_ttl_seconds,
            executor=body.executor,
            executor_ref=body.executor_ref,
            executor_metadata=body.executor_metadata,
            limit_keys=body.limit_keys,
        ),
    )


@router.post(
    "/plans/{plan_id}/members/{task_id}/complete", response_model=TransitionResponse
)
async def complete(
    plan_id: UUID, task_id: str, body: ReportRequest, db: Db, auth: Auth
):
    return await _transition(
        db, auth, plan_id, task_id, Transition.complete(body.execution_id)
    )


@router.post(
    "/plans/{plan_id}/members/{task_id}/fail", response_model=TransitionResponse
)
async def fail(plan_id: UUID, task_id: str, body: FailRequest, db: Db, auth: Auth):
    return await _transition(
        db,
        auth,
        plan_id,
        task_id,
        Transition.fail(body.execution_id, body.error_message),
    )


@router.post(
    "/plans/{plan_id}/members/{task_id}/suspend", response_model=TransitionResponse
)
async def suspend(plan_id: UUID, task_id: str, body: ReportRequest, db: Db, auth: Auth):
    return await _transition(
        db, auth, plan_id, task_id, Transition.suspend(body.execution_id)
    )


@router.post(
    "/plans/{plan_id}/members/{task_id}/retry", response_model=TransitionResponse
)
async def retry(plan_id: UUID, task_id: str, db: Db, auth: Auth):
    return await _transition(db, auth, plan_id, task_id, Transition.retry())


@router.post(
    "/plans/{plan_id}/members/{task_id}/interrupt", response_model=TransitionResponse
)
async def interrupt(plan_id: UUID, task_id: str, body: FailRequest, db: Db, auth: Auth):
    return await _transition(
        db,
        auth,
        plan_id,
        task_id,
        Transition.interrupt(body.execution_id, body.error_message),
    )


@router.post(
    "/plans/{plan_id}/members/{task_id}/preempt", response_model=TransitionResponse
)
async def preempt(plan_id: UUID, task_id: str, body: ReportRequest, db: Db, auth: Auth):
    return await _transition(
        db, auth, plan_id, task_id, Transition.preempt(body.execution_id)
    )


@router.post(
    "/plans/{plan_id}/members/{task_id}/skip", response_model=TransitionResponse
)
async def skip(plan_id: UUID, task_id: str, db: Db, auth: Auth):
    return await _transition(db, auth, plan_id, task_id, Transition.skip())


@router.post(
    "/plans/{plan_id}/members/{task_id}/cancel", response_model=TransitionResponse
)
async def cancel(plan_id: UUID, task_id: str, db: Db, auth: Auth):
    """A single task's cancel, by the build holding its claim (via one of its
    plans); 409 ``not_claim_holder`` otherwise."""
    return await _transition(db, auth, plan_id, task_id, Transition.cancel())


@router.post("/plans/{plan_id}/members/{task_id}/yield", response_model=YieldResponse)
async def yield_batch(
    plan_id: UUID, task_id: str, body: YieldRequest, db: Db, auth: Auth
):
    return await yields.yield_batch(
        db,
        auth.environment_id,
        plan_id=plan_id,
        task_id=task_id,
        execution_id=body.execution_id,
        deployment_id=body.deployment_id,
        batch_id=body.batch_id,
        items=body.items,
        yielded=body.yielded,
        suspend=body.suspend,
    )


@router.post(
    "/plans/{plan_id}/members/{task_id}/exclude", response_model=ExclusionResponse
)
async def exclude(
    plan_id: UUID,
    task_id: str,
    db: Db,
    auth: Auth,
    body: ExcludeRequest | None = None,
):
    return await exclusion.exclude_member(
        db,
        auth.environment_id,
        plan_id=plan_id,
        task_id=task_id,
        reason=ExclusionReason.OPERATOR,
        note=body.reason if body else None,
    )


@router.post(
    "/plans/{plan_id}/members/{task_id}/discovery-failed",
    response_model=ExclusionResponse,
)
async def discovery_failed(
    plan_id: UUID, task_id: str, body: DiscoveryFailedRequest, db: Db, auth: Auth
):
    return await exclusion.exclude_member(
        db,
        auth.environment_id,
        plan_id=plan_id,
        task_id=task_id,
        reason=ExclusionReason.DISCOVERY_FAILED,
        error_message=body.error,
    )


@router.get("/plans/{plan_id}", response_model=PlanDetailResponse)
async def get_plan(plan_id: UUID, db: Db, auth: Auth):
    return await plan_reads.get_plan_detail(db, auth.environment_id, plan_id)


@router.get("/plans/{plan_id}/roots", response_model=PlanRootsResponse)
async def plan_roots(plan_id: UUID, db: Db, auth: Auth):
    return await plan_reads.plan_roots(db, auth.environment_id, plan_id)


@router.get("/plans/{plan_id}/graph", response_model=PlanGraphResponse)
async def plan_graph(plan_id: UUID, db: Db, auth: Auth):
    return await plan_reads.plan_graph(db, auth.environment_id, plan_id)


@router.post(
    "/plans/{plan_id}/members/{task_id}/artifacts",
    response_model=TaskArtifactListResponse,
)
async def upload_artifacts(
    plan_id: UUID, task_id: str, body: ArtifactUploadRequest, db: Db, auth: Auth
):
    rows = await artifacts.upload_artifacts(
        db,
        auth.environment_id,
        plan_id=plan_id,
        task_id=task_id,
        artifacts=[
            artifacts.ArtifactIn(artifact_type=a.type, name=a.name, body=a.body)
            for a in body.artifacts
        ],
    )
    return artifact_list(rows)
