"""CRUD for named environment concurrency limits.

See ``models/concurrency_limit.py`` for the model and semantics; the
enforcement itself happens in the task-start endpoint
(``routes/builds.py::start_task`` with ``enforce_limits=true``).
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.auth import SdkAuth, require_sdk_auth
from stardag_api.db import get_db
from stardag_api.models import EnvironmentConcurrencyLimit
from stardag_api.schemas import (
    ConcurrencyLimitList,
    ConcurrencyLimitResponse,
    ConcurrencyLimitUpsert,
)

router = APIRouter(prefix="/concurrency-limits", tags=["concurrency-limits"])


@router.get("", response_model=ConcurrencyLimitList)
async def list_concurrency_limits(
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[SdkAuth, Depends(require_sdk_auth)],
):
    """List the environment's named concurrency limits."""
    rows = (
        (
            await db.execute(
                select(EnvironmentConcurrencyLimit)
                .where(
                    EnvironmentConcurrencyLimit.environment_id == auth.environment_id
                )
                .order_by(EnvironmentConcurrencyLimit.key)
            )
        )
        .scalars()
        .all()
    )
    return ConcurrencyLimitList(
        limits=[
            ConcurrencyLimitResponse(key=row.key, max_concurrent=row.max_concurrent)
            for row in rows
        ]
    )


@router.put("/{key}", response_model=ConcurrencyLimitResponse)
async def upsert_concurrency_limit(
    key: str,
    payload: ConcurrencyLimitUpsert,
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[SdkAuth, Depends(require_sdk_auth)],
):
    """Create or update a named concurrency limit for the environment."""
    if payload.max_concurrent < 1:
        raise HTTPException(status_code=422, detail="max_concurrent must be at least 1")

    async def _select_row() -> EnvironmentConcurrencyLimit | None:
        return (
            await db.execute(
                select(EnvironmentConcurrencyLimit).where(
                    EnvironmentConcurrencyLimit.environment_id == auth.environment_id,
                    EnvironmentConcurrencyLimit.key == key,
                )
            )
        ).scalar_one_or_none()

    row = await _select_row()
    if row is None:
        row = EnvironmentConcurrencyLimit(
            environment_id=auth.environment_id,
            key=key,
            max_concurrent=payload.max_concurrent,
        )
        db.add(row)
        try:
            await db.commit()
        except IntegrityError:
            # Lost a create race against a concurrent PUT for the same
            # (environment, key): the unique constraint fired — retry as an
            # update of the row the winner inserted.
            await db.rollback()
            row = await _select_row()
            if row is None:  # pragma: no cover - winner deleted in between
                raise HTTPException(
                    status_code=409, detail="Concurrent limit modification"
                )
            row.max_concurrent = payload.max_concurrent
            await db.commit()
    else:
        row.max_concurrent = payload.max_concurrent
        await db.commit()
    return ConcurrencyLimitResponse(key=key, max_concurrent=payload.max_concurrent)


@router.delete("/{key}", status_code=204)
async def delete_concurrency_limit(
    key: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[SdkAuth, Depends(require_sdk_auth)],
):
    """Remove a named concurrency limit (the key becomes unlimited)."""
    row = (
        await db.execute(
            select(EnvironmentConcurrencyLimit).where(
                EnvironmentConcurrencyLimit.environment_id == auth.environment_id,
                EnvironmentConcurrencyLimit.key == key,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Concurrency limit not found")
    await db.delete(row)
    await db.commit()
