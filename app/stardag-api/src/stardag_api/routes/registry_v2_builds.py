"""``/api/v2`` build lifecycle routes: complete, fail, cancel, exit-early,
resume, delete.

Thin by rule: parse, resolve the environment from the credentials, call
one service in ``services/builds.py``.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.auth import SdkAuth, require_sdk_auth
from stardag_api.db import get_db
from stardag_api.schemas_v2 import (
    BuildCompleteRequest,
    BuildFailRequest,
    BuildResponse,
    BuildResumeRequest,
    ResumeResponse,
)
from stardag_api.services import builds

router = APIRouter(tags=["registry-v2"])

Db = Annotated[AsyncSession, Depends(get_db)]
Auth = Annotated[SdkAuth, Depends(require_sdk_auth)]


def _triggered_by(auth: SdkAuth) -> str | None:
    """The user behind a manual status change; NULL for machine callers."""
    return auth.user.external_id if auth.user else None


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
