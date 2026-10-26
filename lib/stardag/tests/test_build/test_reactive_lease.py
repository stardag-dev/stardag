"""The scheduler lease's own calls are bounded by the lease: a renewal or a
release still waiting (a rate limit being waited out, STA-135) when the
lease lapses is abandoned rather than holding the tick past it."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

from stardag.build._reactive import _lease
from stardag.build._reactive._lease import SchedulerLease


class _StuckRegistry:
    """Grants the lease, then never answers a renewal or a release."""

    def __init__(self) -> None:
        self.renewals = 0
        self.releases = 0

    async def scheduler_lease_acquire_aio(self, build_id, *, owner_id, ttl_seconds):
        return SimpleNamespace(held=True)

    async def scheduler_lease_renew_aio(self, build_id, *, owner_id, ttl_seconds):
        self.renewals += 1
        await asyncio.Event().wait()

    async def scheduler_lease_release_aio(self, build_id, *, owner_id):
        self.releases += 1
        await asyncio.Event().wait()


@pytest.fixture
def short_lease(monkeypatch):
    monkeypatch.setattr(_lease, "_LEASE_TTL_SECONDS", 0.2)
    monkeypatch.setattr(_lease, "_LEASE_RENEW_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr(_lease, "_MIN_CALL_SECONDS", 0.05)


async def test_a_stuck_renewal_is_abandoned_and_retried(short_lease):
    registry = _StuckRegistry()
    async with SchedulerLease(registry, uuid4()) as lease:  # type: ignore[arg-type]
        await asyncio.sleep(0.5)
        assert lease.lost
        # Abandoned at the lapse and asked again, not stuck on the first.
        assert registry.renewals >= 2


async def test_a_stuck_release_does_not_hold_the_tick(short_lease):
    registry = _StuckRegistry()
    loop = asyncio.get_running_loop()
    lease = SchedulerLease(registry, uuid4())  # type: ignore[arg-type]
    await lease.__aenter__()
    started = loop.time()
    await asyncio.wait_for(lease.__aexit__(None, None, None), timeout=2)
    assert registry.releases == 1
    assert loop.time() - started < 0.5
