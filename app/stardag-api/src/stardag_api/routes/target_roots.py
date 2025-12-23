"""Target root routes - SDK endpoint for syncing target roots."""

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.auth import SdkAuth, require_sdk_auth
from stardag_api.db import get_db
from stardag_api.models import TargetRoot

router = APIRouter(prefix="/target-roots", tags=["target-roots"])


class TargetRootSyncResponse(BaseModel):
    """Target root for SDK sync."""

    model_config = ConfigDict(from_attributes=True)

    name: str
    uri_prefix: str


@router.get("", response_model=list[TargetRootSyncResponse])
async def list_target_roots(
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[SdkAuth, Depends(require_sdk_auth)],
):
    """List target roots for the authenticated workspace.

    This endpoint is used by the SDK/CLI to sync target root configuration.
    Authentication via API key or JWT token determines the workspace.
    """
    result = await db.execute(
        select(TargetRoot).where(TargetRoot.workspace_id == auth.workspace_id)
    )
    roots = result.scalars().all()

    return [
        TargetRootSyncResponse(
            name=root.name,
            uri_prefix=root.uri_prefix,
        )
        for root in roots
    ]
