"""The scheduler lease's own calls are bounded by the lease: a renewal or a
release still waiting (a rate limit being waited out, STA-135) when the
lease lapses is abandoned rather than holding the tick past it."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

from stardag.build._reactive import _lease
from stardag.build._reactive._lease import SchedulerLease
from stardag.registry import _api_http


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
        # Wait on the state, not a clock: a slow runner gets there later.
        for _ in range(300):
            if lease.lost and registry.renewals >= 2:
                break
            await asyncio.sleep(0.01)
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


class _RateLimitedAcquire(_StuckRegistry):
    """Grants the lease only after waiting out a rate limit longer than
    the TTL, as the transport does on a 429 (it records when the granted
    attempt was sent)."""

    async def scheduler_lease_acquire_aio(self, build_id, *, owner_id, ttl_seconds):
        await asyncio.sleep(0.3)
        _api_http._delivered_no_earlier_than.set(time.monotonic())
        return SimpleNamespace(held=True)


async def test_a_rate_limit_wait_does_not_count_against_the_lease(short_lease):
    """Copilot on #495: timed from the first, rate-limited attempt, a lease
    granted after a 0.3 s wait with a 0.2 s TTL read as lapsed on arrival
    and the tick exited without acting."""
    async with SchedulerLease(_RateLimitedAcquire(), uuid4()) as lease:  # type: ignore[arg-type]
        assert lease.acquired
        assert not lease.lost


class _TakenOver:
    """Another tick holds the lease once this one's first renewal comes
    round: the renewal is refused and so is the re-acquire."""

    def __init__(self) -> None:
        self.acquires = 0

    async def scheduler_lease_acquire_aio(self, build_id, *, owner_id, ttl_seconds):
        self.acquires += 1
        return SimpleNamespace(held=self.acquires == 1)

    async def scheduler_lease_renew_aio(self, build_id, *, owner_id, ttl_seconds):
        return SimpleNamespace(held=False)

    async def scheduler_lease_release_aio(self, build_id, *, owner_id):
        return SimpleNamespace(held=False)


async def test_a_lease_taken_by_another_tick_says_so(short_lease):
    """``taken_over`` separates a lease another tick holds from one that
    lapsed on unanswered renewals -- the outage the tick flags (#506)."""
    async with SchedulerLease(_TakenOver(), uuid4()) as lease:  # type: ignore[arg-type]
        await asyncio.sleep(0.15)
        assert lease.lost and lease.taken_over
    async with SchedulerLease(_StuckRegistry(), uuid4()) as lapsed:  # type: ignore[arg-type]
        await asyncio.sleep(0.4)
        assert lapsed.lost and not lapsed.taken_over
