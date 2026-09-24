"""Concurrency-limit keys of a claim (``task_limit_key``).

Limit-key selection may read non-significant fields, so the keys are per
**instance**: the tick computes them from the instance body it is about to
run and sends them with the claiming start. They are written to
``task_limit_key`` at claim time and **replaced on every claim**; a slot is
a limit key joined to a live claim on its task. See design.md, "Peripheral
tables, re-pointed" and "Wake-ups, limits, locks".

Called only from ``transition_task()``, on the task row it has locked.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.models import (
    EnvironmentConcurrencyLimit,
    Task,
    TaskLimitKey,
    TaskStatus,
)
from stardag_api.models.base import generate_uuid7


async def replace_limit_keys(
    session: AsyncSession,
    environment_id: UUID,
    task_pk: UUID,
    keys: Sequence[str],
    *,
    now: datetime,
) -> None:
    """Make ``keys`` the task's limit keys (none for an empty list)."""
    await session.execute(delete(TaskLimitKey).where(TaskLimitKey.task_pk == task_pk))
    unique = sorted(set(keys))
    if unique:
        await session.execute(
            pg_insert(TaskLimitKey).values(
                [
                    {
                        "id": generate_uuid7(),
                        "environment_id": environment_id,
                        "task_pk": task_pk,
                        "key": key,
                        "created_at": now,
                    }
                    for key in unique
                ]
            )
        )


async def full_limits(
    session: AsyncSession,
    environment_id: UUID,
    task_pk: UUID,
    keys: Sequence[str],
    *,
    now: datetime,
) -> list[str]:
    """The keys among ``keys`` whose limit is full without this task.

    The limit rows are locked ``FOR UPDATE`` in key order, so two claims on
    one key serialise here and the second counts the first's slot. A slot
    is a limit key joined to a **live** claim on its task; a key with no
    limit row is unlimited.
    """
    if not keys:
        return []
    limits = (
        await session.scalars(
            select(EnvironmentConcurrencyLimit)
            .where(
                EnvironmentConcurrencyLimit.environment_id == environment_id,
                EnvironmentConcurrencyLimit.key.in_(sorted(set(keys))),
            )
            .order_by(EnvironmentConcurrencyLimit.key)
            .with_for_update()
        )
    ).all()
    full = []
    for limit in limits:
        holders = await session.scalar(
            select(func.count(func.distinct(TaskLimitKey.task_pk)))
            .join(Task, Task.id == TaskLimitKey.task_pk)
            .where(
                TaskLimitKey.environment_id == environment_id,
                TaskLimitKey.key == limit.key,
                TaskLimitKey.task_pk != task_pk,
                Task.status == TaskStatus.RUNNING,
                Task.claim_expires_at > now,
            )
        )
        if (holders or 0) >= limit.max_concurrent:
            full.append(limit.key)
    return full
