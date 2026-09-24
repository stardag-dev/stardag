"""What the ``/api/v2`` route modules share: the dependency aliases and the
two list mappings more than one resource returns."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.auth import SdkAuth, require_sdk_auth
from stardag_api.db import get_db
from stardag_api.schemas_v2_reads import (
    EventListResponse,
    EventResponse,
    TaskArtifactListResponse,
    TaskArtifactResponse,
)
from stardag_api.services import artifacts, reads

Db = Annotated[AsyncSession, Depends(get_db)]
Auth = Annotated[SdkAuth, Depends(require_sdk_auth)]
Limit = Annotated[int, Query(ge=1, le=reads.MAX_LIST_LIMIT)]
Cursor = Annotated[str | None, Query(max_length=512)]


def artifact_list(rows: list[artifacts.ArtifactView]) -> TaskArtifactListResponse:
    return TaskArtifactListResponse(
        artifacts=[TaskArtifactResponse.model_validate(r) for r in rows]
    )


def event_list(rows: list[reads.EventView]) -> EventListResponse:
    return EventListResponse(events=[EventResponse.model_validate(r) for r in rows])
