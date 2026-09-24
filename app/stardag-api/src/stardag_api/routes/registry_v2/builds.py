"""``/api/v2`` routes of a build: create and read, the listing (paged),
its plans (create one, list them), the frontier, skip-blocked, the
lifecycle (complete, fail, cancel, exit-early, resume, delete), its event
log and its execution ledger.

Thin by rule: parse, resolve the environment from the credentials, call
one service, convert its result.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Response

from stardag_api.auth import SdkAuth
from stardag_api.models import BuildStatus
from stardag_api.routes.registry_v2._common import Auth, Cursor, Db, Limit, event_list
from stardag_api.schemas_v2 import (
    BuildCompleteRequest,
    BuildCreate,
    BuildFailRequest,
    BuildResponse,
    BuildResumeRequest,
    ExecutionListResponse,
    ExecutionResponse,
    FrontierResponse,
    PlanCreate,
    PlanResponse,
    ResumeResponse,
    SkipBlockedResponse,
)
from stardag_api.schemas_v2_reads import (
    BuildListResponse,
    EventListResponse,
    PlanDetailResponse,
    PlanListResponse,
)
from stardag_api.services import (
    builds,
    exclusion,
    executions,
    frontier,
    plan_reads,
    reads,
    registration,
)

router = APIRouter(tags=["registry-v2"])


def _triggered_by(auth: SdkAuth) -> str | None:
    """The user behind a manual status change; NULL for machine callers."""
    return auth.user.external_id if auth.user else None


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


@router.post("/builds/{build_id}/complete", response_model=BuildResponse)
async def complete_build(
    build_id: UUID, db: Db, auth: Auth, body: BuildCompleteRequest | None = None
):
    return await builds.complete_build(
        db,
        auth.environment_id,
        build_id,
        force=body.force if body else False,
        triggered_by=_triggered_by(auth),
    )


@router.post("/builds/{build_id}/fail", response_model=BuildResponse)
async def fail_build(
    build_id: UUID, db: Db, auth: Auth, body: BuildFailRequest | None = None
):
    return await builds.fail_build(
        db,
        auth.environment_id,
        build_id,
        error_message=body.error_message if body else None,
        triggered_by=_triggered_by(auth),
    )


@router.post("/builds/{build_id}/cancel", response_model=BuildResponse)
async def cancel_build(build_id: UUID, db: Db, auth: Auth):
    return await builds.cancel_build(
        db, auth.environment_id, build_id, triggered_by=_triggered_by(auth)
    )


@router.post("/builds/{build_id}/exit-early", response_model=BuildResponse)
async def exit_early(build_id: UUID, db: Db, auth: Auth):
    return await builds.exit_early(db, auth.environment_id, build_id)


@router.post("/builds/{build_id}/resume", response_model=ResumeResponse)
async def resume_build(
    build_id: UUID, db: Db, auth: Auth, body: BuildResumeRequest | None = None
):
    body = body or BuildResumeRequest()
    return await builds.resume_build(
        db,
        auth.environment_id,
        build_id,
        deployment_id=body.deployment_id,
        settings=body.settings,
        executor_metadata=body.executor_metadata,
    )


@router.delete("/builds/{build_id}", status_code=204)
async def delete_build(build_id: UUID, db: Db, auth: Auth):
    await builds.delete_build(db, auth.environment_id, build_id)
    return Response(status_code=204)


@router.get("/builds/{build_id}/executions", response_model=ExecutionListResponse)
async def list_executions(
    build_id: UUID,
    db: Db,
    auth: Auth,
    not_in_current_plan: bool = False,
    include_ended: bool = False,
):
    """The build's executions with no end reported (what ``builds stop``
    lists); ``not_in_current_plan`` keeps the orphans, ``include_ended``
    lists the whole ledger."""
    rows = await executions.list_executions(
        db,
        auth.environment_id,
        build_id,
        not_in_current_plan=not_in_current_plan,
        include_ended=include_ended,
    )
    return ExecutionListResponse(
        build_id=build_id,
        executions=[ExecutionResponse.model_validate(r) for r in rows],
    )


@router.get("/builds", response_model=BuildListResponse)
async def list_builds(
    db: Db,
    auth: Auth,
    status: BuildStatus | None = None,
    reactive_app_name: Annotated[str | None, Query(max_length=64)] = None,
    limit: Limit = 100,
    cursor: Cursor = None,
):
    page = await reads.list_builds(
        db,
        auth.environment_id,
        status=status,
        reactive_app_name=reactive_app_name,
        limit=limit,
        cursor=cursor,
    )
    return BuildListResponse(
        builds=[BuildResponse.model_validate(b) for b in page.builds],
        total=page.total,
        next_cursor=page.next_cursor,
    )


@router.get("/builds/{build_id}/plans", response_model=PlanListResponse)
async def build_plans(build_id: UUID, db: Db, auth: Auth):
    rows = await plan_reads.list_build_plans(db, auth.environment_id, build_id)
    return PlanListResponse(
        build_id=build_id,
        plans=[PlanDetailResponse.model_validate(p) for p in rows],
    )


@router.get("/builds/{build_id}/events", response_model=EventListResponse)
async def build_events(build_id: UUID, db: Db, auth: Auth, limit: Limit = 500):
    return event_list(
        await reads.list_events(db, auth.environment_id, build_id=build_id, limit=limit)
    )
