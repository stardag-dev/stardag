"""The observation fence: when a driver starts looking at the world.

A driver's walk observes targets and reports what it saw (design.md,
"Invalidation: the registry follows the world"): a target seen missing
withdraws a completion. That is only sound if the observation is at least as
fresh as the walk. Backends that serve reads from a local view -- a Modal
Volume mounted into a warm container sees another process's writes and
*deletions* only after a reload -- must therefore not answer a walk from a
view older than the walk itself.

``begin_observation()`` records the moment a walk starts; a backend with a
cached view compares its last refresh against ``observation_fence()`` and
refreshes once before answering. Before any walk the fence is 0, so nothing
changes for code that never walks.

Two walks can start concurrently on different threads, so the assignment is
guarded by a lock and is monotonic: the fence only ever moves forward, so an
older walk's ``begin_observation()`` completing after a newer one's can never
push it backwards and weaken the freshness bar the newer walk relies on.
"""

from __future__ import annotations

import threading
import time

_fence = 0.0
_fence_lock = threading.Lock()


def begin_observation() -> float:
    """Mark the start of an observation; returns the fence (monotonic).

    Thread-safe and monotonic: the fence never moves backwards, even if an
    older walk's call races a newer one's and lands after it.
    """
    global _fence
    now = time.monotonic()
    with _fence_lock:
        _fence = max(_fence, now)
        return _fence


def observation_fence() -> float:
    """The latest ``begin_observation()`` time in this process (0 if none)."""
    with _fence_lock:
        return _fence
