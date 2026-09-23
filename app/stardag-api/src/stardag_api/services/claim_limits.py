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

from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.models import TaskLimitKey
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
