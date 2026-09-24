"""The v2 registry's 24-hour creation quota.

design.md, "Peripheral tables, re-pointed": the quota counts
``task_instance`` rows — the table a non-significant field can inflate
(one row per construction per scope). It is **per environment**, and a
chunk is charged only for the rows it actually inserted (``RETURNING``): a
re-delivered chunk inserts nothing and is never refused for a quota its
first delivery already paid. Disabled unless
``LIMITS_MAX_TASK_INSTANCES_PER_ENVIRONMENT_24H`` is set.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.config import limits_settings
from stardag_api.limits import CONTACT_SUFFIX
from stardag_api.models import TaskInstance
from stardag_api.services.errors import TooManyRequests

QUOTA_WINDOW = timedelta(hours=24)


async def charge_instances(
    session: AsyncSession, environment_id: UUID, inserted: int, *, now: datetime
) -> None:
    """Refuse (429 ``creation_quota_exceeded``, rolling the chunk back) when
    the ``inserted`` rows this transaction just added take the
    environment's last-24-hours count over the quota. The count is read
    after the insert, so it includes them."""
    limit = limits_settings.max_task_instances_per_environment_24h
    if limit is None or inserted == 0:
        return
    # Serialise count-and-commit per environment: two chunks inserting at
    # once would otherwise each count only their own uncommitted rows and
    # both pass. The lock is taken after this chunk's insert and held to its
    # commit, so a waiting chunk's count (a fresh READ COMMITTED snapshot)
    # includes the rows of the one it waited for. A chunk waiting here has
    # only inserted rows (tasks, instances), and a conflict on those is met
    # before this point, so the holder does not wait on the waiter.
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"quota:task_instance:{environment_id}"},
    )
    count = await session.scalar(
        select(func.count())
        .select_from(TaskInstance)
        .where(
            TaskInstance.environment_id == environment_id,
            TaskInstance.created_at >= now - QUOTA_WINDOW,
        )
    )
    if (count or 0) > limit:
        raise TooManyRequests(
            "creation_quota_exceeded",
            f"24-hour task-instance creation quota exceeded ({limit} per"
            f" environment).{CONTACT_SUFFIX}",
            limit=limit,
            current=(count or 0) - inserted,
            requested=inserted,
        )
