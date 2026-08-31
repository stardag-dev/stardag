"""The scheduler lease against the deployed registry, on real Postgres.

STA-17 verification. The two properties below were, until this file, only
ever exercised against ``FakeReactiveRegistry``:

- **Contention.** ``app/stardag-api``'s own suite runs on SQLite, where
  ``SELECT ... FOR UPDATE`` is silently dropped, so it structurally cannot
  evidence that two concurrent acquires serialize. Here the arbiter is the
  deployed API's transaction on a real Postgres, reached by genuinely
  concurrent requests.

- **An outage spanning the TTL.** The client-side deadline is what stops a
  tick driving a build whose lease lapsed server-side while the registry
  was unreachable. The unit tests fake the registry; here the lease is
  real, the server-side lapse is real (wall clock), and the takeover is
  performed by a second, independent client. Only the *cut* is simulated
  (the client's transport raises), which is what an outage is from the
  client's side of the wire.
"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from stardag_integration_tests.registry_live._guard import registry_live_guard

registry_live_guard()

pytestmark = [
    pytest.mark.registry_live,
    pytest.mark.timeout(300),
]

RACERS = 12


def _registry():
    """A fresh APIRegistry from the stack coordinates in the environment.

    Fresh instances on purpose: the outage scenario needs one client cut
    off while another keeps working, and the route-missing latch is
    per-instance state that must not leak between them.
    """
    from stardag.registry import APIRegistry

    return APIRegistry()


def _new_build(registry) -> "object":
    return registry.build_start(description="STA-17 lease verification")


def test_concurrent_acquires_grant_exactly_one() -> None:
    """N concurrent acquires; the FOR UPDATE transaction lets one through."""

    registry = _registry()
    build_id = _new_build(registry)

    async def race(round_no: int) -> None:
        owners = [f"racer-{round_no}-{i}" for i in range(RACERS)]
        results = await asyncio.gather(
            *(
                registry.build_acquire_scheduler_lease_aio(
                    build_id, owner_id=owner, ttl_seconds=60
                )
                for owner in owners
            )
        )
        held = [r for r in results if r.held]
        denied = [r for r in results if not r.held]
        assert len(held) == 1, (
            f"round {round_no}: {len(held)} of {RACERS} concurrent acquires "
            "were granted; the build row's FOR UPDATE must serialize them "
            "down to exactly one"
        )
        # Every denial reports the winner's expiry: same row, same value.
        assert {r.expires_at for r in denied} == {held[0].expires_at}

        winner = owners[results.index(held[0])]
        loser = next(o for o in owners if o != winner)

        # Owner checks, against the live row: the loser can neither extend
        # nor clear the winner's lease, and the winner can do both.
        renewed = await registry.build_renew_scheduler_lease_aio(
            build_id, owner_id=loser, ttl_seconds=60
        )
        assert renewed.held is False
        released = await registry.build_release_scheduler_lease_aio(
            build_id, owner_id=loser
        )
        assert released.held is False
        renewed = await registry.build_renew_scheduler_lease_aio(
            build_id, owner_id=winner, ttl_seconds=60
        )
        assert renewed.held is True
        released = await registry.build_release_scheduler_lease_aio(
            build_id, owner_id=winner
        )
        assert released.held is True

    async def rounds() -> None:
        # Three rounds, not one: a single win proves a winner exists, the
        # release-then-race-again cycle proves the row arbitrates every
        # time rather than staying stuck on its first owner.
        for round_no in range(3):
            await race(round_no)

    asyncio.run(rounds())


def test_a_lapsed_lease_is_taken_over_on_the_real_clock() -> None:
    """Server-side expiry, no release: the takeover is the healing path."""

    registry = _registry()
    build_id = _new_build(registry)

    async def scenario() -> None:
        first = await registry.build_acquire_scheduler_lease_aio(
            build_id, owner_id="dead-tick", ttl_seconds=5
        )
        assert first.held is True

        # Not lapsed yet: a competitor is refused while the lease is live.
        early = await registry.build_acquire_scheduler_lease_aio(
            build_id, owner_id="early-bird", ttl_seconds=60
        )
        assert early.held is False

        await asyncio.sleep(6)

        taken = await registry.build_acquire_scheduler_lease_aio(
            build_id, owner_id="successor", ttl_seconds=60
        )
        assert taken.held is True, "a lapsed lease must deny nothing"
        # And the dead holder cannot renew its way back in.
        stale = await registry.build_renew_scheduler_lease_aio(
            build_id, owner_id="dead-tick", ttl_seconds=60
        )
        assert stale.held is False

    asyncio.run(scenario())


def test_an_outage_spanning_the_ttl_stops_the_lease_on_the_clock(
    monkeypatch,
) -> None:
    """Cut the network under a held lease and leave it cut past the TTL.

    What must happen, in order:

    1. failing renewals are a blip, not a loss — ``lost`` stays False while
       the TTL has time left;
    2. once the TTL passes with no successful renewal, ``lost`` turns True
       on the client's clock alone: nothing returned an answer;
    3. the lease really did lapse server-side — an independent client takes
       the build over;
    4. when the network comes back, the old owner is refused: renew fails,
       the re-acquire fails, release clears nothing, and the successor's
       lease survives untouched.
    """
    from stardag.build import _reactive
    from stardag.build._reactive import SchedulerLease

    ttl = 10
    monkeypatch.setattr(_reactive, "_LEASE_TTL_SECONDS", ttl)
    monkeypatch.setattr(_reactive, "_LEASE_RENEW_INTERVAL_SECONDS", 1.0)

    registry = _registry()
    bystander = _registry()
    build_id = _new_build(registry)

    def _unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("outage (simulated): connection refused")

    async def scenario() -> None:
        lease = SchedulerLease(registry, build_id)
        async with lease:
            assert lease.acquired is True
            t0 = time.monotonic()

            live_transport = registry.async_client._transport
            registry.async_client._transport = httpx.MockTransport(_unreachable)
            try:
                # (1) Half the TTL in, renewals have been raising for a
                # while — and that alone must not read as a loss.
                await asyncio.sleep(ttl / 2)
                assert lease.lost is False, (
                    "a renewal that raises proves nothing; the lease was "
                    "declared lost with time left on the TTL"
                )

                # (2) Past the TTL with the registry still unreachable, the
                # client-side deadline is the only thing that can stop the
                # tick — and it must.
                await asyncio.sleep(ttl / 2 + 1.5)
                assert lease.lost is True, (
                    f"{time.monotonic() - t0:.1f}s since acquire, registry "
                    "unreachable throughout, and the lease still claims to "
                    "be held: the client-side deadline did not fire"
                )

                # (3) The server agrees it lapsed: somebody else can have it.
                taken = await bystander.build_acquire_scheduler_lease_aio(
                    build_id, owner_id="successor", ttl_seconds=60
                )
                assert taken.held is True, (
                    "the lease should have lapsed server-side during the "
                    "outage; a successor was refused"
                )
            finally:
                registry.async_client._transport = live_transport

            # (4) Network is back. The renewal loop's next answer is a real
            # refusal (renew: not yours; re-acquire: successor holds it), so
            # the lease must stay lost — never re-arm over a live successor.
            await asyncio.sleep(3)
            assert lease.lost is True, (
                "the lease re-armed itself over a successor's live lease"
            )

        # __aexit__ released best-effort; owner-checked, so the successor's
        # lease must have survived it.
        still_held = await bystander.build_renew_scheduler_lease_aio(
            build_id, owner_id="successor", ttl_seconds=60
        )
        assert still_held.held is True, (
            "the old owner's exit release cleared the successor's lease"
        )

    asyncio.run(scenario())
