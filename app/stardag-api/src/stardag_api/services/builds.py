"""The minimal build surface the static path needs: create and read.

Build lifecycle (complete, fail, cancel, exit-early, resume, releasing
claims) is I0 step 3; this module is where it will land.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.models import Build, BuildStatus, EventType
from stardag_api.models.base import generate_uuid7, utc_now
from stardag_api.services import event_log
from stardag_api.services.errors import Conflict, NotFound
from stardag_api.services.slug import generate_build_slug
from stardag_api.services.tx import transaction


async def create_build(
    session: AsyncSession,
    environment_id: UUID,
    *,
    build_id: UUID | None = None,
    name: str | None = None,
    description: str | None = None,
    root_task_ids: Sequence[str] = (),
    user_id: UUID | None = None,
) -> Build:
    """Create a RUNNING build (``BUILD_STARTED``). Idempotent on a
    client-minted id: a re-delivered create returns the build unchanged."""
    async with transaction(session):
        if build_id is not None:
            existing = await session.get(Build, build_id)
            if existing is not None:
                if existing.environment_id != environment_id:
                    raise Conflict("build_id_conflict", f"build id {build_id} is taken")
                return existing
        now = utc_now()
        build = Build(
            id=build_id or generate_uuid7(),
            environment_id=environment_id,
            user_id=user_id,
            name=name or generate_build_slug(),
            description=description,
            root_task_ids=list(root_task_ids),
            status=BuildStatus.RUNNING,
            started_at=now,
            last_active_at=now,
            created_at=now,
        )
        session.add(build)
        await session.flush()
        await event_log.append(
            session,
            [
                event_log.event_row(
                    environment_id, EventType.BUILD_STARTED, at=now, build_id=build.id
                )
            ],
        )
        return build


async def get_build(
    session: AsyncSession, environment_id: UUID, build_id: UUID
) -> Build:
    build = await session.scalar(
        select(Build).where(
            Build.environment_id == environment_id, Build.id == build_id
        )
    )
    if build is None:
        raise NotFound("unknown_build", f"no build {build_id}", build_id=str(build_id))
    return build
