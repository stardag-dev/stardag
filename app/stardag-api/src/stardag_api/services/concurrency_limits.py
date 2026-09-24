"""Configuring named concurrency limits (``environment_concurrency_limit``).

A limit caps how many tasks carrying a key may hold a **live claim** at
once in an environment; the claiming start enforces it
(:mod:`stardag_api.services.claim_limits`). This module only sets, lists
and removes the caps — an operator's configuration, not a task event, so
it writes no event and flags no build. Lowering a cap below the live
holders ends nobody's claim: it refuses the next claim until enough have
released. Removing one frees every claim queued on it from the next claim
on; a build refused ``concurrency_limit_reached`` is woken by the next
slot release, as before.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.models import EnvironmentConcurrencyLimit
from stardag_api.models.base import generate_uuid7
from stardag_api.services.errors import NotFound
from stardag_api.services.tx import transaction


@dataclass(frozen=True)
class LimitView:
    key: str
    max_concurrent: int


async def set_limit(
    session: AsyncSession, environment_id: UUID, key: str, max_concurrent: int
) -> LimitView:
    """Create or replace the cap on ``key`` (one row per environment and
    key; the unique constraint makes a concurrent set an upsert, never a
    duplicate)."""
    async with transaction(session):
        stmt = pg_insert(EnvironmentConcurrencyLimit).values(
            id=generate_uuid7(),
            environment_id=environment_id,
            key=key,
            max_concurrent=max_concurrent,
        )
        await session.execute(
            stmt.on_conflict_do_update(
                constraint="uq_environment_concurrency_limit_key",
                set_={"max_concurrent": stmt.excluded.max_concurrent},
            )
        )
    return LimitView(key=key, max_concurrent=max_concurrent)


async def delete_limit(session: AsyncSession, environment_id: UUID, key: str) -> None:
    """Remove the cap on ``key``; 404 ``unknown_limit`` when there is none."""
    async with transaction(session):
        deleted = await session.execute(
            delete(EnvironmentConcurrencyLimit)
            .where(
                EnvironmentConcurrencyLimit.environment_id == environment_id,
                EnvironmentConcurrencyLimit.key == key,
            )
            .returning(EnvironmentConcurrencyLimit.id)
        )
        if deleted.first() is None:
            raise NotFound("unknown_limit", f"no concurrency limit {key!r}", key=key)


async def list_limits(session: AsyncSession, environment_id: UUID) -> list[LimitView]:
    rows = await session.scalars(
        select(EnvironmentConcurrencyLimit)
        .where(EnvironmentConcurrencyLimit.environment_id == environment_id)
        .order_by(EnvironmentConcurrencyLimit.key)
    )
    return [LimitView(key=r.key, max_concurrent=r.max_concurrent) for r in rows]


__all__ = ["LimitView", "delete_limit", "list_limits", "set_limit"]
