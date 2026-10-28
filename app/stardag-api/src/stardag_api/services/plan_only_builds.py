"""The end of a plan-only build: ``POST /builds/{id}/registered``.

A plan-only build registers its roots and their static closure under its
scope and runs nothing. It is created PENDING by
:func:`~stardag_api.services.builds.create_build` and is never RUNNING, so
no read that hands out or wakes work acts on it; this module ends it
REGISTERED. The rest of its lifecycle (refusals, fail, cancel) lives with
the other build transitions in :mod:`stardag_api.services.builds`. See
design.md, "Plan-only builds".
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.models import Build, BuildStatus, EventType
from stardag_api.models.base import utc_now
from stardag_api.services import event_log
from stardag_api.services.builds import (
    active_plan,
    refuse_if_terminal,
    set_build_status,
)
from stardag_api.services.errors import Conflict
from stardag_api.services.registration import lock_build
from stardag_api.services.tx import retry_on_deadlock, transaction


@retry_on_deadlock
async def register_build(
    session: AsyncSession,
    environment_id: UUID,
    build_id: UUID,
    *,
    triggered_by: str | None = None,
) -> Build:
    """``BUILD_REGISTERED``: a plan-only build's plan is fully stated, and
    the build ends REGISTERED, having run nothing.

    Refused 409 ``build_not_plan_only`` for an ordinary build, 409
    ``build_terminal`` (recorded) for one already FAILED or CANCELLED, and
    409 ``plan_incomplete`` (``reason: not_sealed``) unless the active plan
    is sealed. The closure step runs first, as ``/seal`` and ``/complete``
    run it, so a member another plan's expansion made reachable since the
    seal is admitted before the build is recorded registered (a conflict
    it finds fails the build, as there). Members need not be COMPLETED:
    registering is what this build was for. Releases nothing — a plan-only
    build is never RUNNING, so no claim was ever granted through it."""
    async with transaction(session):
        build = await lock_build(session, environment_id, build_id)
        if build.status == BuildStatus.REGISTERED:
            return build
        if not build.plan_only:
            raise Conflict(
                "build_not_plan_only",
                f"build {build.id} is not plan-only; only a plan-only build"
                " ends REGISTERED (complete, fail or cancel it)",
                build_id=str(build.id),
                build_status=build.status.value,
            )
        await refuse_if_terminal(
            session, build, EventType.BUILD_REGISTERED, triggered_by=triggered_by
        )
        plan = await active_plan(session, build.id)
        if plan is None or plan.sealed_at is None:
            raise Conflict(
                "plan_incomplete",
                "the build's active plan is not sealed: the request is not yet"
                " fully stated",
                reason="not_sealed",
                plan_id=str(plan.id) if plan else None,
            )
        from stardag_api.services.plans import close_plan, refuse_closure_conflict

        closed = await close_plan(session, environment_id, plan, now=utc_now())
        if closed.build_failed:
            refuse_closure_conflict(plan, closed)
        now = utc_now()
        set_build_status(
            build, BuildStatus.REGISTERED, now=now, triggered_by=triggered_by
        )
        await event_log.append(
            session,
            [
                event_log.event_row(
                    environment_id,
                    EventType.BUILD_REGISTERED,
                    at=now,
                    build_id=build.id,
                    metadata={"plan_id": str(plan.id)},
                )
            ],
        )
        await session.flush()
        return build
