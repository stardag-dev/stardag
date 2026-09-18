"""Deployment records: which code version is deployed under which handle.

See ``models/deployment.py`` for what a deployment is and why it exists.
This module is its registry surface: the deploy CLI records one, the SDK's
trigger resolves the newest live one for a family, and the garbage
collector asks which ones no running build still needs.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.auth import SdkAuth, require_sdk_auth
from stardag_api.config import limits_settings
from stardag_api.db import get_db
from stardag_api.limits import LimitExceededError, check_rate_limit
from stardag_api.models import Build, BuildStatus, Deployment, WorkspaceRole
from stardag_api.models.base import utc_now
from stardag_api.routes.workspaces import require_workspace_access
from stardag_api.schemas import (
    DeploymentListResponse,
    DeploymentResponse,
    DeploymentUpsert,
)

router = APIRouter(prefix="/deployments", tags=["deployments"])


def _raise_if_limit_exceeded(error: LimitExceededError | None) -> None:
    if error is None:
        return
    headers = {}
    if error.retry_after is not None:
        headers["Retry-After"] = str(error.retry_after)
    raise HTTPException(
        status_code=429,
        detail=error.model_dump(exclude_none=True),
        headers=headers or None,
    )


async def _require_admin_for_user_auth(db: AsyncSession, auth: SdkAuth) -> None:
    """Write endpoints: workspace admins on the JWT path, any API key."""
    if auth.user is None:
        return
    await require_workspace_access(
        db, auth.user.id, auth.workspace_id, min_role=WorkspaceRole.ADMIN
    )


async def _running_builds_by_handle(
    db: AsyncSession, environment_id: UUID, handles: list[str]
) -> dict[str, int]:
    """RUNNING reactive builds per app handle — what makes a deployment live."""
    if not handles:
        return {}
    rows = (
        await db.execute(
            select(Build.reactive_app_name, func.count())
            .where(
                Build.environment_id == environment_id,
                Build.latest_status == BuildStatus.RUNNING,
                Build.reactive_app_name.in_(handles),
            )
            .group_by(Build.reactive_app_name)
        )
    ).all()
    return {handle: count for handle, count in rows if handle is not None}


def _response(row: Deployment, running: int) -> DeploymentResponse:
    return DeploymentResponse(
        id=row.id,
        environment_id=row.environment_id,
        family=row.family,
        handle=row.handle,
        code_id=row.code_id,
        created_at=row.created_at,
        retired_at=row.retired_at,
        running_builds=running,
    )


@router.get("", response_model=DeploymentListResponse)
async def list_deployments(
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[SdkAuth, Depends(require_sdk_auth)],
    family: Annotated[str | None, Query(max_length=64)] = None,
    include_retired: bool = False,
):
    """List the environment's deployments, newest first.

    ``family`` narrows to one app family; ``include_retired`` adds the ones
    already retired. Each row carries its count of RUNNING builds, which is
    what decides whether it may be retired.
    """
    _raise_if_limit_exceeded(check_rate_limit(auth.workspace_id, limits_settings))
    query = select(Deployment).where(Deployment.environment_id == auth.environment_id)
    if family is not None:
        query = query.where(Deployment.family == family)
    if not include_retired:
        query = query.where(Deployment.retired_at.is_(None))
    rows = (
        (await db.execute(query.order_by(Deployment.created_at.desc()))).scalars().all()
    )
    running = await _running_builds_by_handle(
        db, auth.environment_id, [r.handle for r in rows]
    )
    return DeploymentListResponse(
        deployments=[_response(r, running.get(r.handle, 0)) for r in rows]
    )


@router.post("", response_model=DeploymentResponse, status_code=201)
async def record_deployment(
    payload: DeploymentUpsert,
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[SdkAuth, Depends(require_sdk_auth)],
):
    """Record a deployed code version. Idempotent on ``(environment, handle)``.

    Re-recording an existing handle with the same code id returns the
    existing row and un-retires it (the app was deployed again). A
    different code id for the same handle is a 409: the handle is derived
    from the code id, so this would be a resolver bug, not a new version.
    """
    _raise_if_limit_exceeded(check_rate_limit(auth.workspace_id, limits_settings))
    await _require_admin_for_user_auth(db, auth)
    existing = (
        await db.execute(
            select(Deployment)
            .where(
                Deployment.environment_id == auth.environment_id,
                Deployment.handle == payload.handle,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.code_id != payload.code_id:
            raise HTTPException(
                status_code=409,
                detail={
                    "error_code": "deployment_handle_taken",
                    "handle": payload.handle,
                    "code_id": existing.code_id,
                    "requested_code_id": payload.code_id,
                    "message": (
                        f"Deployment handle {payload.handle!r} already records "
                        f"code id {existing.code_id!r}; it cannot be re-recorded "
                        f"as {payload.code_id!r}. A handle names exactly one "
                        "code version."
                    ),
                },
            )
        if existing.family != payload.family:
            existing.family = payload.family
        existing.retired_at = None
        await db.commit()
        running = await _running_builds_by_handle(
            db, auth.environment_id, [existing.handle]
        )
        return _response(existing, running.get(existing.handle, 0))
    row = Deployment(
        environment_id=auth.environment_id,
        family=payload.family,
        handle=payload.handle,
        code_id=payload.code_id,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return _response(row, 0)


@router.post("/{deployment_id}/retire", response_model=DeploymentResponse)
async def retire_deployment(
    deployment_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[SdkAuth, Depends(require_sdk_auth)],
    force: bool = False,
):
    """Mark a deployment retired, so the resolver stops handing it out.

    Refused with 409 while a RUNNING build still references its handle,
    unless ``force`` — the caller is then saying the builds are theirs to
    strand, which is what stopping the app would do anyway. Retiring
    records nothing about the app itself; stopping it is the caller's
    (the CLI's) job, and this is the bookkeeping that goes with it.
    """
    _raise_if_limit_exceeded(check_rate_limit(auth.workspace_id, limits_settings))
    await _require_admin_for_user_auth(db, auth)
    row = await db.get(Deployment, deployment_id)
    if row is None or row.environment_id != auth.environment_id:
        raise HTTPException(status_code=404, detail="Deployment not found")
    running = (
        await _running_builds_by_handle(db, auth.environment_id, [row.handle])
    ).get(row.handle, 0)
    if running and not force:
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": "deployment_in_use",
                "handle": row.handle,
                "running_builds": running,
                "message": (
                    f"Deployment {row.handle!r} still drives {running} running "
                    "build(s). Wait for them, cancel them, or retire with force."
                ),
            },
        )
    if row.retired_at is None:
        row.retired_at = utc_now()
        await db.commit()
    return _response(row, running)
