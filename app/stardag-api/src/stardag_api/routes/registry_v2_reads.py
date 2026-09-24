"""``/api/v2`` reads the SDK client calls beyond the frontier — the
watchdog's build listing, a plan's roots, a task with its instances, the
event log of a task or a build — and the task-artifact routes.

Thin by rule: parse, resolve the environment from the credentials, call
one service in ``services/reads.py`` or ``services/artifacts.py``.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.auth import SdkAuth, require_sdk_auth
from stardag_api.db import get_db
from stardag_api.models import BuildStatus
from stardag_api.schemas_v2 import (
    EventListResponse,
    EventResponse,
    ArtifactUploadRequest,
    BuildListResponse,
    BuildResponse,
    PlanRootsResponse,
    TaskArtifactListResponse,
    TaskArtifactResponse,
    TaskResponse,
)
from stardag_api.services import artifacts, reads

router = APIRouter(tags=["registry-v2"])

Db = Annotated[AsyncSession, Depends(get_db)]
Auth = Annotated[SdkAuth, Depends(require_sdk_auth)]
Limit = Annotated[int, Query(ge=1, le=reads.MAX_LIST_LIMIT)]


@router.get("/builds", response_model=BuildListResponse)
async def list_builds(
    db: Db,
    auth: Auth,
    status: BuildStatus | None = None,
    reactive_app_name: Annotated[str | None, Query(max_length=64)] = None,
    limit: Limit = 100,
):
    rows = await reads.list_builds(
        db,
        auth.environment_id,
        status=status,
        reactive_app_name=reactive_app_name,
        limit=limit,
    )
    return BuildListResponse(builds=[BuildResponse.model_validate(b) for b in rows])


@router.get("/plans/{plan_id}/roots", response_model=PlanRootsResponse)
async def plan_roots(plan_id: UUID, db: Db, auth: Auth):
    return await reads.plan_roots(db, auth.environment_id, plan_id)


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
