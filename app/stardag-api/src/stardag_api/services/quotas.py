"""The v2 registry's 24-hour creation quotas.

design.md, "Peripheral tables, re-pointed". Two tables are bounded:

- ``task_instance`` rows — the table a non-significant field can inflate
  (one row per construction per scope);
  ``LIMITS_MAX_TASK_INSTANCES_PER_ENVIRONMENT_24H``, 429
  ``creation_quota_exceeded``.
- ``task_artifact`` rows — v1's per-workspace artifact count, carried over
  per environment; ``LIMITS_MAX_ARTIFACTS_PER_ENVIRONMENT_24H``, 429
  ``artifact_creation_limit``.

Both are **per environment**, and a write is charged only for the rows it
actually inserted (``RETURNING``): a re-delivered chunk, or a re-upload that
replaces an artifact's body, inserts nothing and is never refused for a
quota its first delivery already paid. Each is disabled unless its setting
is set.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.config import limits_settings
from stardag_api.limits import CONTACT_SUFFIX
from stardag_api.models import TaskArtifact, TaskInstance
from stardag_api.services.errors import TooManyRequests

QUOTA_WINDOW = timedelta(hours=24)


async def _count_after_insert(
    session: AsyncSession,
    environment_id: UUID,
    model: type[TaskInstance] | type[TaskArtifact],
    *,
    now: datetime,
) -> int:
    """The environment's rows of ``model`` created in the window, this
    transaction's included, counted under the quota's advisory lock.

    Serialises count-and-commit per environment and table: two writes
    inserting at once would otherwise each count only their own uncommitted
    rows and both pass. The lock is taken after the caller's insert and held
    to its commit, so a waiting write's count (a fresh READ COMMITTED
    snapshot) includes the rows of the one it waited for. A write waiting
    here holds only rows it inserted (and, for an upload, its task row,
    which nobody holding this lock waits for), so the holder does not wait
    on the waiter."""
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"quota:{model.__tablename__}:{environment_id}"},
    )
    count = await session.scalar(
        select(func.count())
        .select_from(model)
        .where(
            model.environment_id == environment_id,
            model.created_at >= now - QUOTA_WINDOW,
        )
    )
    return count or 0


async def charge_instances(
    session: AsyncSession, environment_id: UUID, inserted: int, *, now: datetime
) -> None:
    """Refuse (429 ``creation_quota_exceeded``, rolling the chunk back) when
    the ``inserted`` rows this transaction just added take the
    environment's last-24-hours count over the quota."""
    limit = limits_settings.max_task_instances_per_environment_24h
    if limit is None or inserted == 0:
        return
    count = await _count_after_insert(session, environment_id, TaskInstance, now=now)
    if count > limit:
        raise TooManyRequests(
            "creation_quota_exceeded",
            f"24-hour task-instance creation quota exceeded ({limit} per"
            f" environment).{CONTACT_SUFFIX}",
            limit=limit,
            current=count - inserted,
            requested=inserted,
        )


async def charge_artifacts(
    session: AsyncSession, environment_id: UUID, inserted: int, *, now: datetime
) -> None:
    """Refuse (429 ``artifact_creation_limit``, rolling the upload back)
    when the ``inserted`` artifacts this transaction just added take the
    environment's last-24-hours count over the quota. A replaced artifact
    keeps its ``created_at`` and is not charged again."""
    limit = limits_settings.max_artifacts_per_environment_24h
    if limit is None or inserted == 0:
        return
    count = await _count_after_insert(session, environment_id, TaskArtifact, now=now)
    if count > limit:
        raise TooManyRequests(
            "artifact_creation_limit",
            f"24-hour artifact creation quota exceeded ({limit} per"
            f" environment).{CONTACT_SUFFIX}",
            limit=limit,
            current=count - inserted,
            requested=inserted,
        )
