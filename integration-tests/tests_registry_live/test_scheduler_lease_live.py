"""The scheduler lease against the deployed registry, on real Postgres.

STA-17 verification. The properties below were, until this file, only ever
exercised against ``FakeReactiveRegistry``:

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

**On timing (STA-47).** A lease is an expiry, so a clock cannot be removed
from these scenarios -- but it can be waited *on* rather than asserted
*at*, and that is the rule here:

- Anything that must be true *while* a lease is live is asked of a lease
  with minutes left on it, never of the short one that is about to lapse.
  The earlier form asked "is a competitor refused?" of a five-second lease
  with a network round trip in between, which is a bet that the round trip
  is fast; on a loaded runner it lost, the lease had genuinely lapsed, and
  the test called the correct answer a failure.
- Anything that must be true *after* an expiry is polled for until it
  happens, with a generous timeout -- so a slow container costs seconds,
  not a red check on somebody's unrelated PR.
- What remains asserted at an instant is only ever the direction no
  latency can produce: a lower bound that fails if, and only if, the
  server handed out a lease that was still live.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

import httpx
import pytest

from stardag_integration_tests.registry_live._guard import registry_live_guard

registry_live_guard()

pytestmark = [
    pytest.mark.registry_live,
    pytest.mark.timeout(300),
]

RACERS = 12

# The server's floor on a lease TTL (``MIN_LEASE_TTL_SECONDS`` in
# ``stardag_api.services.wakeups``), and therefore the shortest lapse this
# tier can wait for. Spelled out rather than imported: the API package is
# not a dependency of this one, and this is the wire contract, not an
# implementation detail.
MIN_TTL_SECONDS = 5

# The TTL for every lease whose *liveness* is the point. Long enough that no
# round trip, container cold start or loaded runner can consume it -- which
# is the whole of what makes the assertions against it deterministic.
LIVE_TTL_SECONDS = 600

# How long a wait is given before it is called a failure rather than
# slowness. Generous on purpose: every one of these waits is for something
# that takes seconds when the stack is healthy, so the timeout only ever
# fires on a genuine "this never happened".
WAIT_TIMEOUT_SECONDS = 60.0


def _registry():
    """A fresh APIRegistry from the stack coordinates in the environment.

    Fresh instances on purpose: the outage scenarios need one client cut
    off while another keeps working, and the route-missing latch is
    per-instance state that must not leak between them.
    """
    from stardag.registry import APIRegistry

    return APIRegistry()


def _new_build(registry) -> "object":
    return registry.build_start(description="STA-17 lease verification")


async def _wait_for(
    condition: Callable[[], Awaitable[Any] | Any],
    *,
    what: str,
    timeout: float = WAIT_TIMEOUT_SECONDS,
    poll_interval: float = 0.25,
) -> Any:
    """Poll until ``condition`` is truthy; return it. Fail saying what was wanted.

    The async counterpart of ``_wait.wait_until``, and here for the same
    reason: the alternative is a sleep sized for how long something ought
    to take, which is both slower than necessary when the stack is warm and
    a false failure when it is not.
    """
    loop = asyncio.get_running_loop()
    started = loop.time()
    deadline = started + timeout
    while True:
        result = condition()
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return result
        if loop.time() >= deadline:
            raise AssertionError(
                f"Waited {loop.time() - started:.1f}s for {what} and it did not happen."
            )
        await asyncio.sleep(poll_interval)


class _CountingTransport(httpx.AsyncBaseTransport):
    """Pass requests through, and count the ones that came back.

    Used to wait for the renewal loop to have *received real answers*,
    rather than sleeping for about as long as that usually takes. A sleep
    there does not risk a false failure -- it risks the quieter one, of
    passing before the code under test had done anything at all.

    Counted after the response, which is the only count that means what
    the wait says it means: incrementing on dispatch would let the wait
    finish while the answer was still on the wire, so "the loop has its
    refusal in hand" would again be a guess about timing.
    """

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner
        self.responses = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self._inner.handle_async_request(request)
        self.responses += 1
        return response


def test_concurrent_acquires_grant_exactly_one() -> None:
    """N concurrent acquires; the FOR UPDATE transaction lets one through."""

    registry = _registry()
    build_id = _new_build(registry)

    async def race(round_no: int) -> None:
        owners = [f"racer-{round_no}-{i}" for i in range(RACERS)]
        results = await asyncio.gather(
            *(
                registry.build_acquire_scheduler_lease_aio(
                    build_id, owner_id=owner, ttl_seconds=LIVE_TTL_SECONDS
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
            build_id, owner_id=loser, ttl_seconds=LIVE_TTL_SECONDS
        )
        assert renewed.held is False
        released = await registry.build_release_scheduler_lease_aio(
            build_id, owner_id=loser
        )
        assert released.held is False
        renewed = await registry.build_renew_scheduler_lease_aio(
            build_id, owner_id=winner, ttl_seconds=LIVE_TTL_SECONDS
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
    """Server-side expiry, no release: the takeover is the healing path.

    Two legs, and keeping them apart is the point. The refusal leg is what
    makes "lapsed" mean anything -- without it, a server that granted every
    acquire would pass the takeover leg -- and it has no clock in it at
    all. The lapse leg has to have one, and spends it waiting.
    """

    registry = _registry()
    build_id = _new_build(registry)

    async def scenario() -> None:
        loop = asyncio.get_running_loop()

        # (1) While a lease is live, a competitor is refused -- and with ten
        # minutes on it, that stays true however slow the wire is.
        live = await registry.build_acquire_scheduler_lease_aio(
            build_id, owner_id="incumbent", ttl_seconds=LIVE_TTL_SECONDS
        )
        assert live.held is True
        refused = await registry.build_acquire_scheduler_lease_aio(
            build_id, owner_id="early-bird", ttl_seconds=LIVE_TTL_SECONDS
        )
        assert refused.held is False
        # The denial reports the holder's expiry: same row, same value.
        assert refused.expires_at == live.expires_at
        released = await registry.build_release_scheduler_lease_aio(
            build_id, owner_id="incumbent"
        )
        assert released.held is True

        # (2) Now let one really lapse, held by a tick that dies without
        # releasing it -- which across containers is the only way it ever
        # ends.
        #
        # ``asked_at`` is read *before* the request, so the server's expiry
        # is at or after ``asked_at + ttl`` whatever the wire does. That is
        # what makes the lower bound below unfalsifiable by latency: it can
        # fail only if the server handed a still-live lease to a second
        # owner.
        asked_at = loop.time()
        first = await registry.build_acquire_scheduler_lease_aio(
            build_id, owner_id="dead-tick", ttl_seconds=MIN_TTL_SECONDS
        )
        assert first.held is True

        async def takeover():
            result = await registry.build_acquire_scheduler_lease_aio(
                build_id, owner_id="successor", ttl_seconds=LIVE_TTL_SECONDS
            )
            return result if result.held else None

        await _wait_for(
            takeover,
            poll_interval=0.5,
            what=(
                f"the {MIN_TTL_SECONDS}s lease of a tick that never released "
                "it to lapse, so that a successor can take it over"
            ),
        )
        waited = loop.time() - asked_at
        assert waited >= MIN_TTL_SECONDS, (
            f"the takeover was granted {waited:.1f}s after the acquire was "
            f"sent, which is inside its {MIN_TTL_SECONDS}s TTL: a live lease "
            "was handed to a second owner"
        )

        # And the dead holder cannot renew its way back in.
        stale = await registry.build_renew_scheduler_lease_aio(
            build_id, owner_id="dead-tick", ttl_seconds=LIVE_TTL_SECONDS
        )
        assert stale.held is False

    asyncio.run(scenario())


def _cut_wire(registry) -> tuple[httpx.AsyncBaseTransport, Callable[[], int]]:
    """Point ``registry``'s async client at a transport that always raises.

    Returns the live transport, to put back, and a reader for how many
    requests the cut has swallowed -- which is the evidence that the
    renewal loop has been trying and failing, rather than an assumption
    that enough time has passed for it to have.
    """
    swallowed = 0

    def _unreachable(request: httpx.Request) -> httpx.Response:
        nonlocal swallowed
        swallowed += 1
        raise httpx.ConnectError("outage (simulated): connection refused")

    live_transport = registry.async_client._transport
    registry.async_client._transport = httpx.MockTransport(_unreachable)
    return live_transport, lambda: swallowed


def test_failing_renewals_are_a_blip_and_not_a_lost_lease(monkeypatch) -> None:
    """Renewals that raise prove nothing, and must not stop the tick.

    The lease's own docstring: "can no longer show that it holds the lease"
    is deliberately broader than "the server said no", but it is also not
    *narrower* than it -- a registry that is merely unreachable has said
    nothing, and a tick that stopped on that would be stopping on no
    evidence at all.

    A long TTL, and the assertion taken as soon as the failures have
    actually happened: the property is about failed renewals, not about
    elapsed time, so the run should not depend on how much of the TTL the
    acquire round trip ate.
    """
    # The lease timing knobs are mutable globals, so the patch must land on
    # the module whose code reads them -- the package deliberately does not
    # re-export them, precisely so a patch against it fails loudly here
    # rather than silently patching nothing.
    from stardag.build._reactive import SchedulerLease, _tick

    monkeypatch.setattr(_tick, "_LEASE_TTL_SECONDS", LIVE_TTL_SECONDS)
    monkeypatch.setattr(_tick, "_LEASE_RENEW_INTERVAL_SECONDS", 1.0)

    registry = _registry()
    build_id = _new_build(registry)

    async def scenario() -> None:
        lease = SchedulerLease(registry, build_id)
        async with lease:
            assert lease.acquired is True
            live_transport, swallowed = _cut_wire(registry)
            try:
                # ``lease.lost`` is in the condition so that a lease
                # given up early ends the wait then and there, and is
                # reported as the wrong answer it is -- rather than as a
                # wait for renewals that a stopped loop will never make
                # again.
                await _wait_for(
                    lambda: lease.lost or swallowed() >= 3,
                    what="three renewals to raise against the cut wire",
                )
                assert lease.lost is False, (
                    f"renewals have been raising against a cut wire "
                    f"({swallowed()} so far) and the lease was declared lost "
                    f"with most of its {LIVE_TTL_SECONDS}s TTL still to run; "
                    "a renewal that raises proves nothing"
                )
            finally:
                registry.async_client._transport = live_transport

    asyncio.run(scenario())


def test_an_outage_spanning_the_ttl_stops_the_lease_on_the_clock(
    monkeypatch,
) -> None:
    """Cut the network under a held lease and leave it cut past the TTL.

    What must happen, in order:

    1. once the TTL passes with no successful renewal, ``lost`` turns True
       on the client's clock alone: nothing returned an answer;
    2. the lease really did lapse server-side -- an independent client
       takes the build over;
    3. when the network comes back, the old owner is refused: renew fails
       and the re-acquire fails, so the lease must stay lost rather than
       re-arm over a live successor;
    4. and its exit release, being owner-checked, clears nothing.

    The TTL is the server's minimum, because every wait here is for the
    expiry to have *happened*. Its sibling above owns the opposite
    direction -- that nothing is declared lost early -- and owns it on a
    lease with ten minutes left, where no amount of load can make the
    answer ambiguous.
    """
    from stardag.build._reactive import SchedulerLease, _tick

    ttl = MIN_TTL_SECONDS
    monkeypatch.setattr(_tick, "_LEASE_TTL_SECONDS", ttl)
    monkeypatch.setattr(_tick, "_LEASE_RENEW_INTERVAL_SECONDS", 1.0)

    registry = _registry()
    bystander = _registry()
    build_id = _new_build(registry)

    async def scenario() -> None:
        lease = SchedulerLease(registry, build_id)
        async with lease:
            assert lease.acquired is True

            live_transport, swallowed = _cut_wire(registry)
            counted = _CountingTransport(live_transport)
            try:
                # (1a) The renewal loop is alive and getting nowhere.
                # Waited for separately, and first, because an acquire slow
                # enough to outlive this short TTL -- the exact condition
                # this tier has already seen -- enters the block with the
                # lease *already* given up. Asserting on the renewal count
                # after the deadline wait would then read zero and fail a
                # perfectly valid slow run.
                await _wait_for(
                    lambda: swallowed() > 0,
                    what=(
                        "the renewal loop to attempt a renewal against the "
                        "cut wire: nothing else here evidences that it is "
                        "running at all"
                    ),
                )

                # (1b) Past the TTL with the registry still unreachable, the
                # client-side deadline is the only thing that can stop the
                # tick -- and it must.
                await _wait_for(
                    lambda: lease.lost,
                    timeout=ttl + WAIT_TIMEOUT_SECONDS,
                    what=(
                        f"the {ttl}s lease to be given up: the registry has "
                        "been unreachable since it was taken, so the "
                        "client-side deadline is all there is to stop the "
                        "tick driving a build it can no longer claim"
                    ),
                )

                # (2) The server agrees it lapsed: somebody else can have
                # it. Waited for rather than asserted outright, because the
                # client's deadline is anchored *before* its acquire was
                # sent and the server's starts when the row is written --
                # so the client always gives up first, by up to one round
                # trip. That ordering is deliberate (expiring early is a
                # spurious re-acquire; expiring late is a double-drive), and
                # a test that asserted the takeover at the instant the
                # client gave up would be asserting the opposite of it.
                async def takeover():
                    result = await bystander.build_acquire_scheduler_lease_aio(
                        build_id, owner_id="successor", ttl_seconds=LIVE_TTL_SECONDS
                    )
                    return result if result.held else None

                await _wait_for(
                    takeover,
                    poll_interval=0.5,
                    what=(
                        "the lease to have lapsed server-side during the "
                        "outage, so a successor can take the build over"
                    ),
                )
            finally:
                # Counting from here on, so the wait below is for the
                # renewal loop's real answers rather than for a plausible
                # amount of time.
                registry.async_client._transport = counted

            # (3) Network is back. The renewal loop's next answer is a real
            # refusal (renew: not yours; re-acquire: the successor holds
            # it) -- two requests, and the lease must still be lost when
            # they have both been answered.
            await _wait_for(
                lambda: counted.responses >= 2,
                what=(
                    "the renewal loop to have its refusal in hand: a renew "
                    "and the re-acquire behind it, both against a live "
                    "successor"
                ),
            )
            assert lease.lost is True, (
                "the lease re-armed itself over a successor's live lease"
            )

        # (4) __aexit__ released best-effort; owner-checked, so the
        # successor's lease must have survived it.
        still_held = await bystander.build_renew_scheduler_lease_aio(
            build_id, owner_id="successor", ttl_seconds=LIVE_TTL_SECONDS
        )
        assert still_held.held is True, (
            "the old owner's exit release cleared the successor's lease"
        )

    asyncio.run(scenario())
