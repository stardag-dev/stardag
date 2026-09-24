"""``/api/v2`` reads beyond the frontier — builds (paged), plans (one, a
build's, a plan's roots and graph), tasks (one with its instances, paged by
status), the event log of a task or a build — and the task-artifact routes.

Thin by rule: parse, resolve the environment from the credentials, call
one service in ``services/reads.py``, ``services/plan_reads.py`` or
``services/artifacts.py``.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.auth import SdkAuth, require_sdk_auth
from stardag_api.db import get_db
from stardag_api.models import BuildStatus, TaskStatus
from stardag_api.schemas_v2 import BuildResponse
from stardag_api.schemas_v2_reads import (
    ArtifactUploadRequest,
    BuildListResponse,
    EventListResponse,
    EventResponse,
    PlanDetailResponse,
    PlanGraphResponse,
    PlanListResponse,
    PlanRootsResponse,
    TaskArtifactListResponse,
    TaskArtifactResponse,
    TaskListResponse,
    TaskResponse,
    TaskSummaryResponse,
)
from stardag_api.services import artifacts, plan_reads, reads

router = APIRouter(tags=["registry-v2"])

Db = Annotated[AsyncSession, Depends(get_db)]
Auth = Annotated[SdkAuth, Depends(require_sdk_auth)]
Limit = Annotated[int, Query(ge=1, le=reads.MAX_LIST_LIMIT)]
Cursor = Annotated[str | None, Query(max_length=512)]


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


@router.get("/plans/{plan_id}", response_model=PlanDetailResponse)
async def get_plan(plan_id: UUID, db: Db, auth: Auth):
    return await plan_reads.get_plan_detail(db, auth.environment_id, plan_id)


@router.get("/plans/{plan_id}/roots", response_model=PlanRootsResponse)
async def plan_roots(plan_id: UUID, db: Db, auth: Auth):
    return await plan_reads.plan_roots(db, auth.environment_id, plan_id)


@router.get("/plans/{plan_id}/graph", response_model=PlanGraphResponse)
async def plan_graph(plan_id: UUID, db: Db, auth: Auth):
    return await plan_reads.plan_graph(db, auth.environment_id, plan_id)


@router.get("/tasks", response_model=TaskListResponse)
async def list_tasks(
    db: Db,
    auth: Auth,
    status: TaskStatus | None = None,
    limit: Limit = 100,
    cursor: Cursor = None,
):
    page = await reads.list_tasks(
        db, auth.environment_id, status=status, limit=limit, cursor=cursor
    )
    return TaskListResponse(
        tasks=[TaskSummaryResponse.model_validate(t) for t in page.tasks],
        next_cursor=page.next_cursor,
    )


@router.get("/tasks/{task_id}", response_model=TaskResponse)
async def get_task(task_id: str, db: Db, auth: Auth, limit: Limit = 50):
    return await reads.get_task(db, auth.environment_id, task_id, limit=limit)


def _artifact_list(rows: list[artifacts.ArtifactView]) -> TaskArtifactListResponse:
    return TaskArtifactListResponse(
        artifacts=[TaskArtifactResponse.model_validate(r) for r in rows]
    )


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
    return _artifact_list(rows)


@router.get("/tasks/{task_id}/artifacts", response_model=TaskArtifactListResponse)
async def list_artifacts(task_id: str, db: Db, auth: Auth):
    return _artifact_list(
        await artifacts.list_artifacts(db, auth.environment_id, task_id)
    )


def _event_list(rows: list[reads.EventView]) -> EventListResponse:
    return EventListResponse(events=[EventResponse.model_validate(r) for r in rows])


@router.get("/tasks/{task_id}/events", response_model=EventListResponse)
async def task_events(task_id: str, db: Db, auth: Auth, limit: Limit = 500):
    return _event_list(
        await reads.list_events(db, auth.environment_id, task_id=task_id, limit=limit)
    )


@router.get("/builds/{build_id}/events", response_model=EventListResponse)
async def build_events(build_id: UUID, db: Db, auth: Auth, limit: Limit = 500):
    return _event_list(
        await reads.list_events(db, auth.environment_id, build_id=build_id, limit=limit)
    )
