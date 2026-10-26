"""The build's scheduler lease: single-flight for ticks."""

from __future__ import annotations

import asyncio
import logging
import time
from uuid import UUID, uuid4

from stardag.registry import RegistryABC
from stardag.registry._api_http import last_call_delivered_no_earlier_than

logger = logging.getLogger(__name__)

# The lease's TTL is the dead-tick recovery window: nothing releases the
# lease of a container that vanished, so until it lapses the build is
# invisible to drainers and ``notify`` answers ``scheduler_live=True``.
_LEASE_TTL_SECONDS = 60
# A third of the TTL: two consecutive renewals may fail before the lease
# is at risk, and a refused renewal re-acquires.
_LEASE_RENEW_INTERVAL_SECONDS = _LEASE_TTL_SECONDS / 3
# The least time a release is given, even past the client-side deadline:
# that deadline is anchored before the granting request, so the server
# may still hold the lease a moment longer.
_MIN_CALL_SECONDS = 5.0


class SchedulerLease:
    """The build's single-flight lease, held for the life of one tick.

    Renews itself in the background while the tick lingers, and reports
    itself :attr:`lost` if it can no longer show that it holds the lease —
    including when renewals keep *raising*: a lease has an expiry whether
    or not anyone is reachable to confirm it, so the deadline is tracked
    client-side (monotonic, anchored before the granting request).
    """

    def __init__(self, registry: RegistryABC, build_id: UUID) -> None:
        self._registry = registry
        self._build_id = build_id
        # Per tick, not per process: two ticks for one build in one
        # container must not renew or release each other's lease.
        self._owner_id = uuid4().hex
        self.acquired = False
        self._renewal: asyncio.Task | None = None
        self._lost = False
        self._expires_at: float | None = None

    @property
    def lost(self) -> bool:
        if self._lost:
            return True
        if self._expires_at is None:
            return False
        return asyncio.get_running_loop().time() >= self._expires_at

    def _arm(self, asked_at: float) -> None:
        """Start the client-side TTL at ``asked_at`` -- or later, if the
        call that just returned spent part of its time waiting out a rate
        limit, which no attempt the server saw was inside (STA-135)."""
        delivered = last_call_delivered_no_earlier_than()
        if delivered is not None:
            loop = asyncio.get_running_loop()
            asked_at = max(asked_at, loop.time() - (time.monotonic() - delivered))
        self._expires_at = asked_at + _LEASE_TTL_SECONDS

    def _time_left(self) -> float:
        """Seconds until the lease lapses on its own (client-side)."""
        if self._expires_at is None:
            return float(_LEASE_TTL_SECONDS)
        return max(self._expires_at - asyncio.get_running_loop().time(), 0.0)

    async def __aenter__(self) -> "SchedulerLease":
        asked_at = asyncio.get_running_loop().time()
        result = await self._registry.scheduler_lease_acquire_aio(
            self._build_id, owner_id=self._owner_id, ttl_seconds=_LEASE_TTL_SECONDS
        )
        self.acquired = result.held
        if self.acquired:
            self._arm(asked_at)
            self._renewal = asyncio.create_task(self._renew_forever())
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._renewal is not None:
            renewal, self._renewal = self._renewal, None
            renewal.cancel()
            # Reaps the child's CancelledError as a result while still
            # propagating a cancellation of *this* task.
            await asyncio.gather(renewal, return_exceptions=True)
        if not self.acquired:
            return
        # Bounded by the lease itself: a release still waiting out a rate
        # limit when the lease lapses has nothing left to release.
        try:
            await asyncio.wait_for(
                self._registry.scheduler_lease_release_aio(
                    self._build_id, owner_id=self._owner_id
                ),
                timeout=max(self._time_left(), _MIN_CALL_SECONDS),
            )
        except Exception as e:
            logger.warning(
                "Could not release the scheduler lease for build %s "
                "(ignored; it expires on its own): %s",
                self._build_id,
                e,
            )

    async def _renew_once(self) -> bool:
        """Extend the lease, re-acquiring if it lapsed. False = really lost
        (somebody else holds it)."""
        asked_at = asyncio.get_running_loop().time()
        result = await self._registry.scheduler_lease_renew_aio(
            self._build_id, owner_id=self._owner_id, ttl_seconds=_LEASE_TTL_SECONDS
        )
        if result.held:
            self._arm(asked_at)
            return True
        asked_at = asyncio.get_running_loop().time()
        retaken = await self._registry.scheduler_lease_acquire_aio(
            self._build_id, owner_id=self._owner_id, ttl_seconds=_LEASE_TTL_SECONDS
        )
        if retaken.held:
            self._arm(asked_at)
            return True
        return False

    async def _renew_forever(self) -> None:
        while True:
            await asyncio.sleep(_LEASE_RENEW_INTERVAL_SECONDS)
            # A renewal still waiting out a rate limit when the lease lapses
            # is abandoned so the next one starts (``lost`` is true by the
            # deadline either way). Never bounded below one interval: a
            # lapsed lease is re-acquired by the renewal, so it must still
            # get to ask.
            try:
                held = await asyncio.wait_for(
                    self._renew_once(),
                    timeout=max(self._time_left(), _LEASE_RENEW_INTERVAL_SECONDS),
                )
            except Exception as e:
                logger.warning(
                    "Could not renew the scheduler lease for build %s (will "
                    "retry; it expires on its own if this keeps failing): %s",
                    self._build_id,
                    e,
                )
                continue
            if not held:
                logger.warning(
                    "Lost the scheduler lease for build %s to another tick; "
                    "stopping rather than double-driving the build.",
                    self._build_id,
                )
                self._lost = True
                return
