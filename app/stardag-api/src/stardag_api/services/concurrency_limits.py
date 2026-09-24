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

``list_limits`` also answers "why is this key full?": ``in_use`` is always
computed (one grouped query, same live-claim definition
``claim_limits.full_limits`` enforces against), and ``include_holders``
adds the occupying tasks themselves — an operator's admin view, not
enforcement, so it is a plain read with no locking.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.models import EnvironmentConcurrencyLimit, Plan, Task, TaskLimitKey
from stardag_api.models.base import generate_uuid7, utc_now
from stardag_api.models.enums import TaskStatus
from stardag_api.services.errors import NotFound
from stardag_api.services.tx import transaction


@dataclass(frozen=True)
class HolderView:
    task_id: str
    task_name: str
    build_id: UUID
    plan_id: UUID
    execution_id: UUID | None
    started_at: datetime | None


@dataclass(frozen=True)
class LimitView:
    key: str
    max_concurrent: int
    in_use: int = 0
    holders: list[HolderView] | None = None


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


def _live_claim(now: datetime):
    """The same "live claim" definition ``claim_limits.full_limits`` counts
    against a cap: RUNNING with an unexpired claim."""
    return (Task.status == TaskStatus.RUNNING, Task.claim_expires_at > now)


async def list_limits(
    session: AsyncSession,
    environment_id: UUID,
    *,
    include_holders: bool = False,
) -> list[LimitView]:
    now = utc_now()
    limits = (
        await session.scalars(
            select(EnvironmentConcurrencyLimit)
            .where(EnvironmentConcurrencyLimit.environment_id == environment_id)
            .order_by(EnvironmentConcurrencyLimit.key)
        )
    ).all()
    if not limits:
        return []

    in_use_by_key: dict[str, int] = {
        key: count
        for key, count in (
            await session.execute(
                select(TaskLimitKey.key, func.count(func.distinct(Task.id)))
                .join(Task, Task.id == TaskLimitKey.task_pk)
                .where(
                    TaskLimitKey.environment_id == environment_id,
                    *_live_claim(now),
                )
                .group_by(TaskLimitKey.key)
            )
        ).all()
    }

    holders_by_key: dict[str, list[HolderView]] = {}
    if include_holders:
        rows = (
            await session.execute(
                select(
                    TaskLimitKey.key,
                    Task.task_id,
                    Task.task_name,
                    Plan.build_id,
                    Task.claim_plan_id,
                    Task.execution_id,
                    Task.started_at,
                )
                .select_from(TaskLimitKey)
                .join(Task, Task.id == TaskLimitKey.task_pk)
                .join(
                    Plan,
                    (Plan.id == Task.claim_plan_id)
                    & (Plan.environment_id == Task.environment_id),
                )
                .where(
                    TaskLimitKey.environment_id == environment_id,
                    *_live_claim(now),
                )
                .order_by(Task.started_at.asc().nulls_last(), Task.task_id.asc())
            )
        ).all()
        for (
            key,
            task_id,
            task_name,
            build_id,
            plan_id,
            execution_id,
            started_at,
        ) in rows:
            holders_by_key.setdefault(key, []).append(
                HolderView(
                    task_id=task_id,
                    task_name=task_name,
                    build_id=build_id,
                    plan_id=plan_id,
                    execution_id=execution_id,
                    started_at=started_at,
                )
            )

    return [
        LimitView(
            key=limit.key,
            max_concurrent=limit.max_concurrent,
            in_use=in_use_by_key.get(limit.key, 0),
            holders=(holders_by_key.get(limit.key, []) if include_holders else None),
        )
        for limit in limits
    ]


__all__ = ["HolderView", "LimitView", "delete_limit", "list_limits", "set_limit"]
