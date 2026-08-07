"""Reactive scheduler tick summary routes.

A reactive build is driven by many short-lived scheduler ticks, each in
its own container. The per-tick ``TickSummary`` the SDK computes is the
scheduler's own account of what it did and why — and it used to reach
nobody but that container's log. These endpoints keep the last N per
build so "why is this build not progressing?" is one request.
"""

import json
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.auth import SdkAuth, require_sdk_auth
from stardag_api.config import limits_settings, settings
from stardag_api.db import get_db
from stardag_api.limits import check_rate_limit
from stardag_api.models import BuildTickSummary

# Reused rather than reimplemented: _get_build_checked is the
# build-belongs-to-this-environment check every build sub-resource must
# apply identically (a second copy is a second thing to get wrong), and
# _raise_if_limit_exceeded defines the 429 body/Retry-After contract.
from stardag_api.routes.builds import _get_build_checked, _raise_if_limit_exceeded
from stardag_api.schemas import (
    BuildTickSummaryCreate,
    BuildTickSummaryListResponse,
    BuildTickSummaryResponse,
)

# Same prefix/tag as the builds router: these are build sub-resources and
# belong next to the rest in the OpenAPI schema. They live in their own
# module only because routes/builds.py is already 2.8k lines.
router = APIRouter(prefix="/builds", tags=["builds"])


# Size cap on a single tick summary (compact-JSON byte size), mirroring
# _MAX_REACTIVE_TICK_KWARGS_BYTES / _MAX_EXECUTOR_METADATA_BYTES in
# routes/builds.py. Roomier than those two because the summary is
# growing towards per-blocker detail (task ids of what a tick was
# waiting on) rather than a handful of scalars. Worst case retained per
# build is this times the retention window (~400 KB at the defaults),
# and a tick that would exceed it is reporting something pathological.
_MAX_TICK_SUMMARY_BYTES = 8192


@router.post(
    "/{build_id}/tick-summaries",
    response_model=BuildTickSummaryResponse,
    status_code=201,
)
async def create_build_tick_summary(
    build_id: UUID,
    payload: BuildTickSummaryCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[SdkAuth, Depends(require_sdk_auth)],
):
    """Record one reactive scheduler tick's summary against a build.

    Called at the end of every tick, so it sits on a hot path and is
    best-effort by contract: callers report and move on, and must not
    fail a tick because this failed. Kept to one insert plus one bounded
    delete for that reason.

    The body is stored verbatim. ``outcome`` is required (and promoted
    to a column); every other key — including ones this server has never
    seen — is preserved, which is what lets the SDK grow the summary
    without a server release.
    """
    _raise_if_limit_exceeded(check_rate_limit(auth.workspace_id, limits_settings))

    summary = payload.model_dump(mode="json")
    encoded = json.dumps(summary, separators=(",", ":")).encode("utf-8")
    if len(encoded) > _MAX_TICK_SUMMARY_BYTES:
        raise HTTPException(
            status_code=422,
            detail=(
                f"tick summary must be at most {_MAX_TICK_SUMMARY_BYTES} bytes "
                f"as compact JSON (got {len(encoded)})"
            ),
        )

    build = await _get_build_checked(build_id, db, auth)

    row = BuildTickSummary(
        build_id=build.id,
        outcome=payload.outcome,
        summary=summary,
    )
    db.add(row)
    # Flush so the new row is visible to (and counted by) the prune below.
    await db.flush()

    # Retention is enforced on insert rather than by a background job:
    # this service has no scheduler, and the property we actually need is
    # "a build's trail is bounded", which insert-time pruning gives
    # exactly and immediately. The delete is bounded by construction —
    # steady state removes one row per insert — and rides the same
    # transaction, so a build never observes an over-long trail.
    retained_ids = (
        select(BuildTickSummary.id)
        .where(BuildTickSummary.build_id == build.id)
        # id (UUID7, so insertion-ordered) breaks created_at ties, and
        # must agree with the read endpoint's ordering — otherwise "keep
        # the newest" and "list the newest" could disagree about which
        # of two same-instant rows survives.
        .order_by(BuildTickSummary.created_at.desc(), BuildTickSummary.id.desc())
        .limit(settings.max_tick_summaries_per_build)
        .scalar_subquery()
    )
    await db.execute(
        delete(BuildTickSummary)
        .where(BuildTickSummary.build_id == build.id)
        .where(BuildTickSummary.id.not_in(retained_ids))
        .execution_options(synchronize_session=False)
    )
    await db.commit()

    return BuildTickSummaryResponse(
        id=row.id,
        build_id=row.build_id,
        outcome=row.outcome,
        summary=row.summary,
        created_at=row.created_at,
    )


@router.get(
    "/{build_id}/tick-summaries",
    response_model=BuildTickSummaryListResponse,
)
async def list_build_tick_summaries(
    build_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[SdkAuth, Depends(require_sdk_auth)],
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
):
    """List a build's retained tick summaries, newest first.

    The default window is what a "why is this build not progressing?"
    panel wants: the recent past, where a stalled build is repeating the
    same outcome. ``limit`` is capped above the default retention window
    so asking for everything retained is always possible.
    """
    build = await _get_build_checked(build_id, db, auth)

    result = await db.execute(
        select(BuildTickSummary)
        .where(BuildTickSummary.build_id == build.id)
        .order_by(BuildTickSummary.created_at.desc(), BuildTickSummary.id.desc())
        .limit(limit)
    )
    return BuildTickSummaryListResponse(
        build_id=build.id,
        summaries=[
            BuildTickSummaryResponse(
                id=row.id,
                build_id=row.build_id,
                outcome=row.outcome,
                summary=row.summary,
                created_at=row.created_at,
            )
            for row in result.scalars()
        ],
    )
