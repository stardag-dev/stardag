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
"""

from __future__ import annotations

import time

_fence = 0.0


def begin_observation() -> float:
    """Mark the start of an observation; returns the fence (monotonic)."""
    global _fence
    _fence = time.monotonic()
    return _fence


def observation_fence() -> float:
    """The latest ``begin_observation()`` time in this process (0 if none)."""
    return _fence
