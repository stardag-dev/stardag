"""Reactive-scheduling metadata on a build: its owning app, its tick
configuration, and the trail of tick summaries.

``reactive_app_name`` is the "this build is driven by scheduler ticks"
marker (a stray tick no-ops on a build without it) and names the app whose
ticks drive it; ``reactive_tick_kwargs`` is the SDK-owned tick
configuration every tick reads. Tick summaries are purely diagnostic: the
last N per build, stored verbatim (``outcome`` promoted to a column).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.config import settings as app_settings
from stardag_api.models import Build, BuildTickSummary
from stardag_api.models.base import generate_uuid7, utc_now
from stardag_api.services.builds import get_build
from stardag_api.services.errors import BadRequest
from stardag_api.services.registration import lock_build
from stardag_api.services.tx import transaction

#: Size caps (compact-JSON bytes): both are echoed on hot reads.
MAX_TICK_KWARGS_BYTES = 4096
MAX_TICK_SUMMARY_BYTES = 8192


def _size(value: Any) -> int:
    return len(json.dumps(value, separators=(",", ":")).encode("utf-8"))


async def set_reactive_meta(
    session: AsyncSession,
    environment_id: UUID,
    build_id: UUID,
    *,
    app_name: str,
    tick_kwargs: Mapping[str, Any] | None,
) -> Build:
    """Mark the build reactively scheduled by ``app_name``. Upsert:
    ``tick_kwargs`` is replaced only when given, so a bare re-trigger keeps
    the stored configuration."""
    if not app_name:
        raise BadRequest("invalid_app_name", "app_name must be non-empty")
    if tick_kwargs is not None and _size(tick_kwargs) > MAX_TICK_KWARGS_BYTES:
        raise BadRequest(
            "tick_kwargs_too_large",
            f"tick_kwargs must be at most {MAX_TICK_KWARGS_BYTES} bytes as JSON",
        )
    async with transaction(session):
        build = await lock_build(session, environment_id, build_id)
        build.reactive_app_name = app_name
        if tick_kwargs is not None:
            build.reactive_tick_kwargs = dict(tick_kwargs)
        await session.flush()
        return build


async def add_tick_summary(
    session: AsyncSession,
    environment_id: UUID,
    build_id: UUID,
    summary: Mapping[str, Any],
) -> BuildTickSummary:
    """Record one tick's summary and prune the build's trail to the newest
    ``max_tick_summaries_per_build``. One insert, one bounded delete, under
    the build row lock, so the insert-and-retain step is single-flight per
    build: a concurrent writer's insert is committed, and so counted, before
    this one prunes."""
    body = dict(summary)
    outcome = body.get("outcome")
    if not isinstance(outcome, str) or not 0 < len(outcome) <= 32:
        raise BadRequest("invalid_tick_summary", "outcome is a string of 1-32 chars")
    if _size(body) > MAX_TICK_SUMMARY_BYTES:
        raise BadRequest(
            "tick_summary_too_large",
            f"a tick summary must be at most {MAX_TICK_SUMMARY_BYTES} bytes as JSON",
        )
    async with transaction(session):
        build = await lock_build(session, environment_id, build_id)
        row = BuildTickSummary(
            id=generate_uuid7(),
            environment_id=environment_id,
            build_id=build.id,
            outcome=outcome,
            summary=body,
            created_at=utc_now(),
        )
        session.add(row)
        await session.flush()
        retained = (
            select(BuildTickSummary.id)
            .where(BuildTickSummary.build_id == build.id)
            .order_by(BuildTickSummary.created_at.desc(), BuildTickSummary.id.desc())
            .limit(app_settings.max_tick_summaries_per_build)
            .scalar_subquery()
        )
        await session.execute(
            delete(BuildTickSummary)
            .where(
                BuildTickSummary.build_id == build.id,
                BuildTickSummary.id.not_in(retained),
            )
            .execution_options(synchronize_session=False)
        )
        return row


async def list_tick_summaries(
    session: AsyncSession, environment_id: UUID, build_id: UUID, *, limit: int = 20
) -> list[BuildTickSummary]:
    """The build's retained summaries, newest first."""
    build = await get_build(session, environment_id, build_id)
    return list(
        (
            await session.scalars(
                select(BuildTickSummary)
                .where(BuildTickSummary.build_id == build.id)
                .order_by(
                    BuildTickSummary.created_at.desc(), BuildTickSummary.id.desc()
                )
                .limit(limit)
            )
        ).all()
    )
