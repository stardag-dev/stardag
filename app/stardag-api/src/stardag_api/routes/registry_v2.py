"""The ``/api/v2`` registry routes of the static path.

Thin by rule (engineering rule 1): parse, resolve the environment from the
caller's credentials, call one service, convert its result. Service
refusals (:class:`stardag_api.services.errors.RegistryError`) are mapped to
their status codes by the handler in ``main.py``, with the service's
``code`` as ``detail.code``.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.auth import SdkAuth, require_sdk_auth
from stardag_api.db import get_db
from stardag_api.models import ExclusionReason
from stardag_api.schemas_v2 import (
    BuildCreate,
    BuildResponse,
    DiscoveryFailedRequest,
    ExcludeRequest,
    ExclusionResponse,
    SkipBlockedResponse,
    FailRequest,
    FrontierResponse,
    MembersRequest,
    MembersResponse,
    PlanCreate,
    PlanResponse,
    RenewRequest,
    ReportRequest,
    StartRequest,
    TransitionResponse,
    YieldRequest,
    YieldResponse,
)
from stardag_api.routes.registry_v2_builds import router as builds_router
from stardag_api.routes.registry_v2_executions import router as executions_router
from stardag_api.routes.registry_v2_guard import v2_write_guard
from stardag_api.routes.registry_v2_reads import router as reads_router
from stardag_api.routes.registry_v2_scope import router as scope_router
from stardag_api.routes.registry_v2_wakeups import router as wakeups_router
from stardag_api.services import (
    builds,
    exclusion,
    frontier,
    plans,
    registration,
    transitions,
    yields,
)
from stardag_api.services.transitions import Transition

# The rate limit applies to every write route, the sub-routers' included.
router = APIRouter(tags=["registry-v2"], dependencies=[Depends(v2_write_guard)])

Db = Annotated[AsyncSession, Depends(get_db)]
Auth = Annotated[SdkAuth, Depends(require_sdk_auth)]


# -- builds ---------------------------------------------------------------------


@router.post("/builds", response_model=BuildResponse)
async def create_build(body: BuildCreate, db: Db, auth: Auth):
    return await builds.create_build(
        db,
        auth.environment_id,
        build_id=body.id,
        name=body.name,
        description=body.description,
        root_task_ids=body.root_task_ids,
        user_id=auth.user.id if auth.user else None,
        executor_metadata=body.executor_metadata,
    )


@router.get("/builds/{build_id}", response_model=BuildResponse)
async def get_build(build_id: UUID, db: Db, auth: Auth):
    return await builds.get_build(db, auth.environment_id, build_id)


@router.post("/builds/{build_id}/plans", response_model=PlanResponse)
async def create_plan(build_id: UUID, body: PlanCreate, db: Db, auth: Auth):
    return await registration.create_plan(
        db,
        auth.environment_id,
        build_id=build_id,
        plan_id=body.plan_id,
        deployment_id=body.deployment_id,
        settings_body=body.settings,
        roots=body.roots,
    )


@router.get("/builds/{build_id}/frontier", response_model=FrontierResponse)
async def get_frontier(build_id: UUID, db: Db, auth: Auth):
    return await frontier.get_frontier(db, auth.environment_id, build_id)


@router.post("/builds/{build_id}/skip-blocked", response_model=SkipBlockedResponse)
async def skip_blocked(build_id: UUID, db: Db, auth: Auth):
    return await exclusion.skip_blocked(db, auth.environment_id, build_id)


# -- plans ------------------------------------------------------------------------


@router.post("/plans/{plan_id}/members", response_model=MembersResponse)
async def register_members(plan_id: UUID, body: MembersRequest, db: Db, auth: Auth):
    return await registration.register_members(
        db, auth.environment_id, plan_id, body.items
    )


@router.post("/plans/{plan_id}/seal", response_model=PlanResponse)
async def seal_plan(plan_id: UUID, db: Db, auth: Auth):
    return await plans.seal_plan(db, auth.environment_id, plan_id)


# -- member transitions -------------------------------------------------------------


async def _transition(
    db: AsyncSession, auth: SdkAuth, plan_id: UUID, task_id: str, t: Transition
) -> transitions.TransitionOutcome:
    return await transitions.apply_member_transition(
        db, auth.environment_id, plan_id=plan_id, task_id=task_id, transition=t
    )


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


# -- claims -------------------------------------------------------------------------


@router.post("/tasks/{task_id}/claim/renew", response_model=TransitionResponse)
async def renew_claim(task_id: str, body: RenewRequest, db: Db, auth: Auth):
    return await transitions.renew_claim(
        db,
        auth.environment_id,
        task_id=task_id,
        execution_id=body.execution_id,
        claim_ttl_seconds=body.claim_ttl_seconds,
    )


# -- sub-routers ----------------------------------------------------------------

router.include_router(builds_router)
router.include_router(executions_router)
router.include_router(reads_router)
router.include_router(scope_router)
router.include_router(wakeups_router)
