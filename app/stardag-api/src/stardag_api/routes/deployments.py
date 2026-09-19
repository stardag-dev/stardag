"""Deployment records: which code versions of an app have been deployed.

See ``models/deployment.py`` for what a deployment is. This module is its
registry surface: the deploy CLI records one, and the listing answers
"which code is current for this app, and what ran before it".
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.auth import SdkAuth, require_sdk_auth
from stardag_api.config import limits_settings
from stardag_api.db import get_db
from stardag_api.limits import LimitExceededError, check_rate_limit
from stardag_api.models import Deployment, WorkspaceRole
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


def _response(row: Deployment, *, current: bool) -> DeploymentResponse:
    return DeploymentResponse(
        id=row.id,
        environment_id=row.environment_id,
        app_name=row.app_name,
        code_id=row.code_id,
        deployed_at=row.deployed_at,
        modal_app_id=row.modal_app_id,
        current=current,
    )


def _mark_current(rows: list[Deployment]) -> list[DeploymentResponse]:
    """``rows`` newest-first; the first row seen per app is its current one."""
    seen: set[str] = set()
    out: list[DeploymentResponse] = []
    for row in rows:
        current = row.app_name not in seen
        seen.add(row.app_name)
        out.append(_response(row, current=current))
    return out


@router.get("", response_model=DeploymentListResponse)
async def list_deployments(
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[SdkAuth, Depends(require_sdk_auth)],
    app_name: Annotated[str | None, Query(max_length=64)] = None,
):
    """List the environment's deployments, newest ``deployed_at`` first.

    ``app_name`` narrows to one app. The newest row of an app is marked
    ``current``: the code the backend runs now, and the code a running
    build re-plans under at its next scheduler pass.
    """
    _raise_if_limit_exceeded(check_rate_limit(auth.workspace_id, limits_settings))
    query = select(Deployment).where(Deployment.environment_id == auth.environment_id)
    if app_name is not None:
        query = query.where(Deployment.app_name == app_name)
    rows = list(
        (
            await db.execute(
                query.order_by(Deployment.deployed_at.desc(), Deployment.id.desc())
            )
        )
        .scalars()
        .all()
    )
    return DeploymentListResponse(deployments=_mark_current(rows))


@router.post("", response_model=DeploymentResponse, status_code=201)
async def record_deployment(
    payload: DeploymentUpsert,
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[SdkAuth, Depends(require_sdk_auth)],
):
    """Record a deployed code version of an app.

    Idempotent on ``(environment, app_name, code_id)``: deploying the same
    code again refreshes ``deployed_at`` — it *is* the current deployment
    again, whatever was deployed in between — and returns the existing row,
    with ``modal_app_id`` replaced when one is supplied.
    """
    _raise_if_limit_exceeded(check_rate_limit(auth.workspace_id, limits_settings))
    await _require_admin_for_user_auth(db, auth)
    existing = (
        await db.execute(
            select(Deployment)
            .where(
                Deployment.environment_id == auth.environment_id,
                Deployment.app_name == payload.app_name,
                Deployment.code_id == payload.code_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.deployed_at = utc_now()
        if payload.modal_app_id is not None:
            existing.modal_app_id = payload.modal_app_id
        await db.commit()
        await db.refresh(existing)
        return _response(existing, current=True)
    row = Deployment(
        environment_id=auth.environment_id,
        app_name=payload.app_name,
        code_id=payload.code_id,
        deployed_at=utc_now(),
        modal_app_id=payload.modal_app_id,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return _response(row, current=True)
