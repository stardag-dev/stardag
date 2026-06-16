"""Granular concurrency limiting for executing tasks.

This module provides build-level control over how many tasks run
concurrently, independent of which ``TaskExecutor`` is used. It offers:

- An overall cap on concurrently executing tasks (``max_concurrent_tasks``).
- A collection of **named** limits mapped to tasks via a callback, e.g.
  ``limits={"request-to-service-X": 10}`` with a ``key_selector`` returning
  ``"request-to-service-X"`` for the tasks that hit that service.

Enforcement lives in ``build_aio``: it wraps the executor ``submit`` call in
``async with limiter.slot(task)`` so the same throttle applies to every
executor (local hybrid, Modal, Routed).

Relationship to the global lock (``GlobalConcurrencyLockManager``):
- A **lock** is about exactly-once *identity* (one execution per task-id
  globally) and is held across dynamic-deps suspension.
- A **slot** is about *active execution* capacity. It is released when a task
  suspends waiting for its own dynamic deps and re-acquired on resume —
  required for correctness, since a task holding a slot while suspended on its
  own deps would deadlock under a tight limit.

The ``ConcurrencyLimiter`` protocol is the seam for a future registry-backed
implementation that enforces the named limits *globally* (acquiring a named
slot server-side and polling on a concurrency-limit response), configured from
the Stardag API/UI. Switching to it needs no change in ``build_aio``.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import (
    AbstractAsyncContextManager,
    AsyncExitStack,
    asynccontextmanager,
)
from dataclasses import dataclass, field
from typing import (
    AsyncIterator,
    Callable,
    Protocol,
    Sequence,
)

from stardag import BaseTask

logger = logging.getLogger(__name__)


# Maps a task to zero or more named limit keys. ``None`` (or an empty
# sequence) means the task is not subject to any named limit. A bare ``str``
# is shorthand for a single key.
ConcurrencyKeySelector = Callable[[BaseTask], "str | Sequence[str] | None"]


@dataclass
class ConcurrencyConfig:
    """Configuration for granular, build-local concurrency limiting.

    Attributes:
        limits: Named limits — ``{name: max_concurrent_tasks}``. Every value
            must be ``>= 1`` (a value below 1 can never admit a task and would
            deadlock the build).
        key_selector: Maps each task to the limit name(s) that apply to it.
            Returns ``None`` for "no named limit", a ``str`` for a single
            limit, or a sequence of names for multiple. Any returned name must
            be present in ``limits`` (an unknown name raises ``ValueError`` at
            execution time).
        max_concurrent_tasks: Optional overall cap on the number of tasks
            executing concurrently in the build, across all executors and
            execution modes. ``None`` means no overall cap. Composes with (is
            the min of) any executor-internal worker limits.
    """

    limits: dict[str, int] = field(default_factory=dict)
    key_selector: ConcurrencyKeySelector | None = None
    max_concurrent_tasks: int | None = None


class ConcurrencyLimiter(Protocol):
    """Protocol for limiting concurrent task execution.

    Implementations return an async context manager that admits the task into
    an execution slot on entry (blocking if necessary) and releases it on exit.
    """

    def slot(self, task: BaseTask) -> AbstractAsyncContextManager[None]:
        """Return an async context manager guarding one execution slot.

        Entering blocks until the task may execute (respecting all applicable
        limits); exiting releases the held slot(s).
        """
        ...


class NoOpConcurrencyLimiter:
    """Limiter that never throttles. Used when no limits are configured."""

    @asynccontextmanager
    async def slot(self, task: BaseTask) -> AsyncIterator[None]:
        yield


class LocalConcurrencyLimiter:
    """Semaphore-backed, build-local concurrency limiter.

    Holds one :class:`asyncio.Semaphore` per named limit plus an optional
    overall semaphore. ``slot()`` acquires the overall semaphore first, then
    the task's named semaphores in **sorted key order** so that tasks sharing
    multiple limits can never deadlock on acquisition ordering.

    Construct inside the running event loop that the build uses (``build_aio``
    does this from ``ConcurrencyConfig``); a single instance is not safe to
    reuse across event loops.
    """

    def __init__(self, config: ConcurrencyConfig) -> None:
        for name, value in config.limits.items():
            if value < 1:
                raise ValueError(
                    f"Concurrency limit {name!r} must be >= 1, got {value}."
                )
        if config.max_concurrent_tasks is not None and config.max_concurrent_tasks < 1:
            raise ValueError(
                f"max_concurrent_tasks must be >= 1, got {config.max_concurrent_tasks}."
            )

        self._key_selector = config.key_selector
        self._named: dict[str, asyncio.Semaphore] = {
            name: asyncio.Semaphore(value) for name, value in config.limits.items()
        }
        self._overall: asyncio.Semaphore | None = (
            asyncio.Semaphore(config.max_concurrent_tasks)
            if config.max_concurrent_tasks is not None
            else None
        )

    def _keys_for(self, task: BaseTask) -> list[str]:
        if self._key_selector is None:
            return []
        raw = self._key_selector(task)
        if raw is None:
            return []
        keys = [raw] if isinstance(raw, str) else list(raw)
        for key in keys:
            if key not in self._named:
                raise ValueError(
                    f"Concurrency limit {key!r} (returned by key_selector for "
                    f"task {task.id}) is not defined in ConcurrencyConfig.limits "
                    f"(known limits: {sorted(self._named)})."
                )
        # Sorted + de-duplicated: stable ordering avoids acquisition deadlock,
        # de-dup avoids acquiring the same semaphore twice (which would
        # self-deadlock a limit of 1).
        return sorted(set(keys))

    @asynccontextmanager
    async def slot(self, task: BaseTask) -> AsyncIterator[None]:
        keys = self._keys_for(task)
        async with AsyncExitStack() as stack:
            if self._overall is not None:
                await stack.enter_async_context(self._overall)
            for key in keys:
                await stack.enter_async_context(self._named[key])
            yield


def build_concurrency_limiter(
    config: ConcurrencyConfig | None,
    limiter: ConcurrencyLimiter | None,
) -> ConcurrencyLimiter:
    """Resolve the limiter for a build.

    An explicit ``limiter`` wins; otherwise a :class:`LocalConcurrencyLimiter`
    is built from ``config``; otherwise a :class:`NoOpConcurrencyLimiter`
    (zero overhead) is used.
    """
    if limiter is not None:
        return limiter
    if config is not None:
        return LocalConcurrencyLimiter(config)
    return NoOpConcurrencyLimiter()


__all__ = [
    "ConcurrencyConfig",
    "ConcurrencyKeySelector",
    "ConcurrencyLimiter",
    "LocalConcurrencyLimiter",
    "NoOpConcurrencyLimiter",
    "build_concurrency_limiter",
]
