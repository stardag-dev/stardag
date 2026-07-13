"""Registry-backed concurrency limiter for resident builds.

Implements the :class:`~stardag.build._concurrency.ConcurrencyLimiter`
protocol on top of the registry's named environment concurrency limits —
the same server-side primitive reactive scheduler ticks enforce with — so
resident ``build()``/``build_aio()`` builds share slots with each other
and with reactive builds, **across processes and machines**. This fills
the seam ``_concurrency.py`` reserved for a "global, server-driven
limiter".

Semantics (mirroring the reactive tick's acquisition):

- A slot is acquired by an *enforced task start*
  (``task_start_with_limits_aio``): the server atomically counts RUNNING
  holders per key against the environment caps and records TASK_STARTED
  on success (the engine's own later start-with-executor-refs is a
  tolerated duplicate).
- The slot is released by the task leaving RUNNING status — the engine's
  completed/failed/cancelled events, or TASK_SUSPENDED while a task waits
  on its dynamic deps (parity with ``LocalConcurrencyLimiter``, which
  also releases during suspension). ``slot()``'s exit therefore does
  nothing; there is no lease to renew or leak.
- A denied acquire blocks and retries at a fixed poll interval with a
  small jitter (there is no push channel to resident builds; slot
  releases in other builds/processes are observed by polling, and the
  jitter avoids N denied waiters hammering the limit rows in lockstep).
  Transient registry errors (5xx/429/network) are retried with
  exponential backoff instead of failing the task — a registry restart
  mid-build must not cascade into task/build failures. Non-transient
  errors (auth, validation) raise.

Operational caveats:

- **A slot leaked by a crashed resident build has no automatic healer.**
  The reactive-mode liveness story (worker self-reporting + tick
  staleness self-heal) does not apply here: if a resident build process
  is killed after acquiring, its task stays RUNNING and holds the slot
  until the task (or build) is explicitly failed/cancelled via the
  API/UI. Prefer reactive scheduling for unattended runs; an admin
  eviction path is planned.
- The acquiring start is recorded without an executor ref. If the same
  task is RUNNING with a ref in another build, this transiently clears
  ``latest_executor_ref`` (until the engine's ref-recording start lands)
  and refreshes the staleness clock other ticks are watching — a small
  churn-only window.
- Conversely, a legitimately long-running limited resident task (no
  executor ref — local executors never record one) that also appears in
  a concurrently *ticking reactive* build can be force-failed by that
  build's ``stale_running_no_ref_seconds`` heal once it exceeds the
  bound. Raise the bound in that build's ``TickConfig`` if you mix modes
  over the same long tasks.

Requires a registry server with concurrency-limit support; against an
older server, enforcement parameters are ignored and acquisition always
succeeds (see the Modal how-to's server-requirement note).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import typing
from uuid import UUID

from stardag import BaseTask
from stardag.build._base import current_build_id_var
from stardag.exceptions import APIError, RateLimitError
from stardag.build._concurrency import ConcurrencyKeySelector
from stardag.registry import RegistryABC, registry_provider

logger = logging.getLogger(__name__)


class RegistryConcurrencyLimiter:
    """ConcurrencyLimiter enforcing the registry's named environment limits.

    Example::

        import stardag as sd
        from stardag.build import RegistryConcurrencyLimiter

        sd.build(
            root,
            concurrency_limiter=RegistryConcurrencyLimiter(
                key_selector=lambda task: ["gpu"] if needs_gpu(task) else [],
            ),
        )

    Args:
        key_selector: Maps a task to the named limit key(s) it runs under —
            a single key, a sequence of keys, or None/empty (same contract
            as ``ConcurrencyConfig.key_selector``). Tasks mapping to no
            keys are admitted without any registry call.
        registry: Registry backend; defaults to the configured provider.
        poll_interval_seconds: Retry interval while a key is at capacity.
        max_wait_seconds: Give up (raise TimeoutError) after this long
            waiting for a slot; None waits indefinitely.
    """

    def __init__(
        self,
        key_selector: ConcurrencyKeySelector,
        *,
        registry: RegistryABC | None = None,
        poll_interval_seconds: float = 2.0,
        max_wait_seconds: float | None = None,
    ) -> None:
        self.key_selector = key_selector
        self._registry = registry
        self.poll_interval_seconds = poll_interval_seconds
        self.max_wait_seconds = max_wait_seconds

    @property
    def registry(self) -> RegistryABC:
        if self._registry is None:
            self._registry = registry_provider.get()
        return self._registry

    def slot(self, task: BaseTask) -> typing.AsyncContextManager[None]:
        return self._slot(task)

    def _keys_for(self, task: BaseTask) -> list[str]:
        raw = self.key_selector(task)
        if raw is None:
            return []
        keys = [raw] if isinstance(raw, str) else list(raw)
        # Sorted + de-duplicated, mirroring LocalConcurrencyLimiter (and the
        # server's sorted-key lock ordering); duplicates would double-count
        # the task against a single cap.
        return sorted(set(keys))

    @contextlib.asynccontextmanager
    async def _slot(self, task: BaseTask) -> typing.AsyncIterator[None]:
        limit_keys = self._keys_for(task)
        if not limit_keys:
            yield
            return

        build_id = current_build_id_var.get()
        if build_id is None:
            # Outside a build context there is no build to record the
            # acquiring TASK_STARTED against — admit without enforcement
            # rather than failing the caller.
            logger.warning(
                f"RegistryConcurrencyLimiter used outside a build context; "
                f"admitting task {task.id} without limit enforcement."
            )
            yield
            return

        await self._acquire(build_id, task, limit_keys)
        # No release action: the slot is the task's RUNNING status — freed
        # by the engine's completed/failed/cancelled/suspended events.
        yield

    # Transient registry failures back off exponentially from
    # poll_interval_seconds up to this bound.
    _ERROR_BACKOFF_MAX_SECONDS = 30.0

    @staticmethod
    def _is_transient(error: Exception) -> bool:
        """Errors worth retrying: rate limits, server errors, network."""
        if isinstance(error, RateLimitError):
            return True
        if isinstance(error, APIError):
            return error.status_code is None or error.status_code >= 500
        return isinstance(error, (TimeoutError, OSError))

    async def _acquire(
        self, build_id: UUID, task: BaseTask, limit_keys: list[str]
    ) -> None:
        loop = asyncio.get_running_loop()
        deadline = (
            loop.time() + self.max_wait_seconds
            if self.max_wait_seconds is not None
            else None
        )
        error_backoff = self.poll_interval_seconds
        while True:
            try:
                # Note: this acquiring start carries no executor ref — if
                # the task is already RUNNING with a ref in another build it
                # transiently clears latest_executor_ref until the engine's
                # ref-recording start lands (churn-only; see module docs).
                started = await self.registry.task_start_with_limits_aio(
                    build_id, task, limit_keys=limit_keys
                )
            except Exception as e:
                # A registry blip must not cascade into task/build failures
                # (every other engine registry call is best-effort): retry
                # transient errors with backoff; raise the rest (auth,
                # validation — retrying can't help).
                if not self._is_transient(e):
                    raise
                delay = error_backoff
                error_backoff = min(error_backoff * 2, self._ERROR_BACKOFF_MAX_SECONDS)
                logger.warning(
                    f"Registry error acquiring concurrency-limit slot(s) "
                    f"{limit_keys} for task {task.id} (retrying in "
                    f"{delay:.1f}s): {e}"
                )
            else:
                if started:
                    return
                error_backoff = self.poll_interval_seconds  # healthy again
                delay = self.poll_interval_seconds
                logger.debug(
                    f"Task {task.id} denied by concurrency limits "
                    f"{limit_keys}; retrying in {delay:.1f}s"
                )
            # Jitter so N waiters don't re-take the server's limit rows
            # (FOR UPDATE) in lockstep.
            delay += random.uniform(0, delay * 0.25)
            if deadline is not None and loop.time() + delay >= deadline:
                raise TimeoutError(
                    f"Timed out after {self.max_wait_seconds}s waiting for "
                    f"concurrency-limit slot(s) {limit_keys} for task {task.id}"
                )
            await asyncio.sleep(delay)
