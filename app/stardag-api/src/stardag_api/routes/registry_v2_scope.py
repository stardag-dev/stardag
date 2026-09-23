"""``/api/v2`` routes for the deterministic scope: deployments and settings.

Thin by rule: parse, resolve the environment from the credentials, call
one service in ``services/deployments.py``.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.auth import SdkAuth, require_sdk_auth
from stardag_api.db import get_db
from stardag_api.models import DeploymentKind
from stardag_api.schemas_v2 import (
    DeploymentCreate,
    DeploymentListResponse,
    DeploymentResponse,
    SettingsResponse,
)
from stardag_api.services import deployments

router = APIRouter(tags=["registry-v2"])

Db = Annotated[AsyncSession, Depends(get_db)]
Auth = Annotated[SdkAuth, Depends(require_sdk_auth)]


@router.post("/deployments", response_model=DeploymentResponse)
async def create_deployment(body: DeploymentCreate, db: Db, auth: Auth):
    return await deployments.create_deployment(
        db,
        auth.environment_id,
        deployment_id=body.id,
        kind=body.kind,
        app_name=body.app_name,
        code_id=body.code_id,
        image_id=body.image_id,
        modal_app_id=body.modal_app_id,
    )


@router.post("/deployments/{deployment_id}/activate", response_model=DeploymentResponse)
async def activate_deployment(deployment_id: UUID, db: Db, auth: Auth):
    return await deployments.activate_deployment(db, auth.environment_id, deployment_id)


@router.get("/deployments", response_model=DeploymentListResponse)
async def list_deployments(
    db: Db,
    auth: Auth,
    kind: DeploymentKind | None = None,
    app_name: str | None = None,
    current: bool = False,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
):
    rows = await deployments.list_deployments(
        db,
        auth.environment_id,
        kind=kind,
        app_name=app_name,
        current_only=current,
        limit=limit,
    )
    return DeploymentListResponse(
        deployments=[DeploymentResponse.model_validate(r) for r in rows]
    )


@router.get("/settings/{settings_hash}", response_model=SettingsResponse)
async def get_settings(settings_hash: str, db: Db, auth: Auth):
    return await deployments.get_settings(db, auth.environment_id, settings_hash)
