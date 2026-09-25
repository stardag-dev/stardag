"""``/api/v2`` routes for reactive scheduling: notify, wake-candidates, the
scheduler lease, reactive meta and tick summaries.

Thin by rule: parse, resolve the environment from the credentials, call
one service in ``services/wakeups.py`` or ``services/reactive.py``.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query

from stardag_api.routes.registry_v2._common import Auth, Db
from stardag_api.schemas_v2 import (
    BuildResponse,
    LeaseResponse,
    NotifyResponse,
    ReactiveMetaRequest,
    TickSummaryCreate,
    TickSummaryListResponse,
    TickSummaryResponse,
    WakeCandidateResponse,
    WakeCandidatesResponse,
)
from stardag_api.services import reactive, wakeups

router = APIRouter(tags=["registry-v2"])

Owner = Annotated[str, Query(min_length=1, max_length=64)]
LeaseTtl = Annotated[
    int,
    Query(ge=wakeups.MIN_LEASE_TTL_SECONDS, le=wakeups.MAX_LEASE_TTL_SECONDS),
]


# -- wake-ups -----------------------------------------------------------------------


@router.post("/builds/wake-candidates", response_model=WakeCandidatesResponse)
async def wake_candidates(
    db: Db,
    auth: Auth,
    limit: Annotated[
        int, Query(ge=1, le=wakeups.MAX_WAKE_CANDIDATES)
    ] = wakeups.MAX_WAKE_CANDIDATES,
):
    chosen = await wakeups.wake_candidates(db, auth.environment_id, limit=limit)
    return WakeCandidatesResponse(
        builds=[WakeCandidateResponse.model_validate(c) for c in chosen]
    )


@router.post("/builds/{build_id}/notify", response_model=NotifyResponse)
async def notify(build_id: UUID, db: Db, auth: Auth, can_spawn: bool = True):
    return await wakeups.notify(db, auth.environment_id, build_id, can_spawn=can_spawn)


@router.get("/builds/{build_id}/notify", response_model=NotifyResponse)
async def read_notify(build_id: UUID, db: Db, auth: Auth):
    return await wakeups.read_notify(db, auth.environment_id, build_id)


@router.delete("/builds/{build_id}/notify", response_model=NotifyResponse)
async def clear_notify(build_id: UUID, db: Db, auth: Auth):
    return await wakeups.clear_notify(db, auth.environment_id, build_id)


# -- the scheduler lease --------------------------------------------------------------


@router.post("/builds/{build_id}/scheduler-lease", response_model=LeaseResponse)
async def acquire_lease(
    build_id: UUID, db: Db, auth: Auth, owner_id: Owner, ttl_seconds: LeaseTtl = 60
):
    return await wakeups.acquire_lease(
        db, auth.environment_id, build_id, owner_id=owner_id, ttl_seconds=ttl_seconds
    )


@router.put("/builds/{build_id}/scheduler-lease", response_model=LeaseResponse)
async def renew_lease(
    build_id: UUID, db: Db, auth: Auth, owner_id: Owner, ttl_seconds: LeaseTtl = 60
):
    return await wakeups.renew_lease(
        db, auth.environment_id, build_id, owner_id=owner_id, ttl_seconds=ttl_seconds
    )


@router.delete("/builds/{build_id}/scheduler-lease", response_model=LeaseResponse)
async def release_lease(build_id: UUID, db: Db, auth: Auth, owner_id: Owner):
    return await wakeups.release_lease(
        db, auth.environment_id, build_id, owner_id=owner_id
    )


# -- reactive meta and tick summaries ---------------------------------------------------


@router.put("/builds/{build_id}/reactive-meta", response_model=BuildResponse)
async def set_reactive_meta(
    build_id: UUID, body: ReactiveMetaRequest, db: Db, auth: Auth
):
    return await reactive.set_reactive_meta(
        db,
        auth.environment_id,
        build_id,
        app_name=body.app_name,
        tick_kwargs=body.tick_kwargs,
    )


@router.post(
    "/builds/{build_id}/tick-summaries",
    response_model=TickSummaryResponse,
    status_code=201,
)
async def add_tick_summary(build_id: UUID, body: TickSummaryCreate, db: Db, auth: Auth):
    return await reactive.add_tick_summary(
        db, auth.environment_id, build_id, body.model_dump(mode="json")
    )


@router.get("/builds/{build_id}/tick-summaries", response_model=TickSummaryListResponse)
async def list_tick_summaries(
    build_id: UUID,
    db: Db,
    auth: Auth,
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
):
    rows = await reactive.list_tick_summaries(
        db, auth.environment_id, build_id, limit=limit
    )
    return TickSummaryListResponse(
        build_id=build_id,
        summaries=[TickSummaryResponse.model_validate(r) for r in rows],
    )
