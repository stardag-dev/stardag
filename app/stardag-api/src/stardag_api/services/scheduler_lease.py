"""The scheduler lease: at most one tick drives a reactive build.

Two owner-checked columns on ``build`` (``scheduler_lease_owner``,
``scheduler_lease_until``); a lapsed lease is taken over by the next
acquire. Lease writers lock only the build row, never ``build_wake``, so
they stay out of the wake-up flaggers' way (``services/wakeups.py``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.models import Build
from stardag_api.models.base import utc_now
from stardag_api.services.errors import BadRequest, NotFound
from stardag_api.services.tx import transaction

#: Bounds on a scheduler lease's TTL, in seconds.
MIN_LEASE_TTL_SECONDS = 5
MAX_LEASE_TTL_SECONDS = 3600


def lease_live(build: Build, now: datetime) -> bool:
    return build.scheduler_lease_until is not None and (
        build.scheduler_lease_until > now
    )


async def _locked_build(
    session: AsyncSession, environment_id: UUID, build_id: UUID
) -> Build:
    """The build row ``FOR NO KEY UPDATE``: the lease writers only."""
    build = await session.scalar(
        select(Build)
        .where(Build.environment_id == environment_id, Build.id == build_id)
        .with_for_update(key_share=True)
        .execution_options(populate_existing=True)
    )
    if build is None:
        raise NotFound("unknown_build", f"no build {build_id}", build_id=str(build_id))
    return build


@dataclass(frozen=True)
class LeaseState:
    build_id: UUID
    held: bool
    expires_at: datetime | None = None


def _lease_ttl(ttl_seconds: int) -> timedelta:
    if not MIN_LEASE_TTL_SECONDS <= ttl_seconds <= MAX_LEASE_TTL_SECONDS:
        raise BadRequest(
            "invalid_lease_ttl",
            f"ttl_seconds must be in [{MIN_LEASE_TTL_SECONDS},"
            f" {MAX_LEASE_TTL_SECONDS}]",
            ttl_seconds=ttl_seconds,
        )
    return timedelta(seconds=ttl_seconds)


def _owner(owner_id: str) -> str:
    # Two callers both sending "" would hold each other's lease.
    if not owner_id:
        raise BadRequest("invalid_lease_owner", "owner_id must be non-empty")
    return owner_id


async def acquire_lease(
    session: AsyncSession,
    environment_id: UUID,
    build_id: UUID,
    *,
    owner_id: str,
    ttl_seconds: int,
) -> LeaseState:
    """Take the lease if it is free, lapsed, or already this owner's (a
    retried acquire is not a lost race; owner ids are per tick)."""
    ttl, owner_id = _lease_ttl(ttl_seconds), _owner(owner_id)
    async with transaction(session):
        build = await _locked_build(session, environment_id, build_id)
        now = utc_now()
        if lease_live(build, now) and build.scheduler_lease_owner != owner_id:
            return LeaseState(
                build_id, held=False, expires_at=build.scheduler_lease_until
            )
        build.scheduler_lease_owner = owner_id
        build.scheduler_lease_until = now + ttl
        return LeaseState(build_id, held=True, expires_at=build.scheduler_lease_until)


async def renew_lease(
    session: AsyncSession,
    environment_id: UUID,
    build_id: UUID,
    *,
    owner_id: str,
    ttl_seconds: int,
) -> LeaseState:
    """Extend the lease, for its live holder only: a tick whose lease lapsed
    and was taken over learns it here."""
    ttl, owner_id = _lease_ttl(ttl_seconds), _owner(owner_id)
    async with transaction(session):
        build = await _locked_build(session, environment_id, build_id)
        now = utc_now()
        if build.scheduler_lease_owner != owner_id or not lease_live(build, now):
            return LeaseState(build_id, held=False)
        build.scheduler_lease_until = now + ttl
        return LeaseState(build_id, held=True, expires_at=build.scheduler_lease_until)


async def release_lease(
    session: AsyncSession, environment_id: UUID, build_id: UUID, *, owner_id: str
) -> LeaseState:
    """Drop the lease if ``owner_id`` still holds it; ``held`` reports
    whether it did (a lost tick cannot clear its successor's lease).

    A released lease also **consumes the hand-out** that spawned its tick:
    the release time is recorded on the build (``scheduler_lease_released_at``),
    and :func:`~stardag_api.services.wakeups.wake_candidates` treats a hand-out older than it as spent. The
    hand-out window exists to collapse the askers between a hand-out and its
    tick taking the lease into one spawn; once that tick has run and ended,
    the window has done its job, and holding it any longer only hides a flag
    that landed after the tick looked (STA-34). Without this, a build
    re-flagged within the window after its tick exited is handed out to
    nobody until the window lapses -- and then only if something else happens
    to ask, which in an environment with no other builds and no watchdog is
    never.

    Written on the build row, which this transaction already holds, and not
    on the wake row: locking the wake row here would make a concurrent
    flagger ``SKIP LOCKED`` past it and lose its flag. A tick that dies
    without releasing, or releases a lease that had already lapsed, leaves
    no release time, so its hand-out keeps the whole window, as before.
    """
    owner_id = _owner(owner_id)
    async with transaction(session):
        build = await _locked_build(session, environment_id, build_id)
        if build.scheduler_lease_owner != owner_id:
            return LeaseState(build_id, held=False)
        now = utc_now()
        # Only a release of a *live* lease is evidence the tick ran to its
        # end as the build's scheduler. A holder whose lease lapsed may
        # already have a successor handed out; spending that hand-out would
        # spawn a second one.
        if lease_live(build, now):
            build.scheduler_lease_released_at = now
        build.scheduler_lease_owner = None
        build.scheduler_lease_until = None
    return LeaseState(build_id, held=True)


__all__ = [
    "MAX_LEASE_TTL_SECONDS",
    "MIN_LEASE_TTL_SECONDS",
    "LeaseState",
    "acquire_lease",
    "lease_live",
    "release_lease",
    "renew_lease",
]
