"""``/api/v2`` routes for an environment's configuration: deployments and
settings (the deterministic scope), and named concurrency limits.

Thin by rule: parse, resolve the environment from the credentials, call
one service in ``services/deployments.py`` or
``services/concurrency_limits.py``.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Path, Query

from stardag_api.models import DeploymentKind
from stardag_api.routes.registry_v2._common import Auth, Db
from stardag_api.schemas_v2 import (
    ConcurrencyLimitInfo,
    ConcurrencyLimitListResponse,
    ConcurrencyLimitResponse,
    ConcurrencyLimitSet,
    DeploymentActivate,
    DeploymentCreate,
    DeploymentInfo,
    DeploymentListResponse,
    DeploymentResponse,
    SettingsResponse,
)
from stardag_api.services import concurrency_limits, deployments

router = APIRouter(tags=["registry-v2"])

LimitKey = Annotated[str, Path(min_length=1, max_length=255)]


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
async def activate_deployment(
    deployment_id: UUID, db: Db, auth: Auth, body: DeploymentActivate | None = None
):
    body = body or DeploymentActivate()
    return await deployments.activate_deployment(
        db,
        auth.environment_id,
        deployment_id,
        modal_app_id=body.modal_app_id,
        image_id=body.image_id,
    )


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
        deployments=[DeploymentInfo.model_validate(r) for r in rows]
    )


@router.get("/deployments/{deployment_id}", response_model=DeploymentInfo)
async def get_deployment(deployment_id: UUID, db: Db, auth: Auth):
    return await deployments.get_deployment(db, auth.environment_id, deployment_id)


@router.get("/settings/{settings_hash}", response_model=SettingsResponse)
async def get_settings(settings_hash: UUID, db: Db, auth: Auth):
    return await deployments.get_settings(db, auth.environment_id, settings_hash)


@router.put("/concurrency-limits/{key}", response_model=ConcurrencyLimitResponse)
async def set_concurrency_limit(
    key: LimitKey, body: ConcurrencyLimitSet, db: Db, auth: Auth
):
    return await concurrency_limits.set_limit(
        db, auth.environment_id, key, body.max_concurrent
    )


@router.delete("/concurrency-limits/{key}", status_code=204)
async def delete_concurrency_limit(key: LimitKey, db: Db, auth: Auth) -> None:
    await concurrency_limits.delete_limit(db, auth.environment_id, key)


@router.get("/concurrency-limits", response_model=ConcurrencyLimitListResponse)
async def list_concurrency_limits(db: Db, auth: Auth, include_holders: bool = False):
    rows = await concurrency_limits.list_limits(
        db, auth.environment_id, include_holders=include_holders
    )
    return ConcurrencyLimitListResponse(
        limits=[ConcurrencyLimitInfo.model_validate(r) for r in rows]
    )
