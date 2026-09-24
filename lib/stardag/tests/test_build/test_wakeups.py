"""Cross-build wake-ups, SDK side: ``drain_wake_candidates`` spawns one tick
per build the registry hands out, and every failure degrades to "the flag
stays set" rather than to a failed pass. (The tick and the resident engine
calling it: ``test_reactive_tick.py``, ``test_resident_detached.py``.)"""

from __future__ import annotations

import typing
from uuid import UUID

from stardag.build._wakeups import drain_wake_candidates
from stardag.registry import WakeCandidate
from stardag.testing import InMemoryRegistry


class _Scripted(InMemoryRegistry):
    def __init__(self, candidates: list[WakeCandidate], error: Exception | None = None):
        super().__init__()
        self.candidates = candidates
        self.error = error

    def build_wake_candidates(self, limit: int = 20) -> list[WakeCandidate]:
        if self.error is not None:
            raise self.error
        handed, self.candidates = self.candidates, []
        return handed


def _spawner() -> tuple[list[tuple[UUID, str]], typing.Callable[[UUID, str], None]]:
    spawned: list[tuple[UUID, str]] = []
    return spawned, lambda build_id, app: spawned.append((build_id, app))


def _candidate(app: str) -> WakeCandidate:
    from stardag.build._registration import new_id

    return WakeCandidate(build_id=new_id(), reactive_app_name=app)


async def test_spawns_once_per_candidate_with_its_app():
    a, b = _candidate("app-a"), _candidate("app-b")
    spawned, spawn = _spawner()
    assert await drain_wake_candidates(_Scripted([a, b]), spawn) == [
        a.build_id,
        b.build_id,
    ]
    assert spawned == [(a.build_id, "app-a"), (b.build_id, "app-b")]


async def test_one_failing_spawn_does_not_stop_the_rest():
    a, b = _candidate("gone"), _candidate("app-b")
    spawned, spawn = _spawner()

    def flaky(build_id: UUID, app_name: str) -> None:
        if app_name == "gone":
            raise RuntimeError("app deleted")
        spawn(build_id, app_name)

    assert await drain_wake_candidates(_Scripted([a, b]), flaky) == [b.build_id]
    assert spawned == [(b.build_id, "app-b")]


async def test_a_registry_error_is_swallowed():
    spawned, spawn = _spawner()
    registry = _Scripted([], error=RuntimeError("503"))
    assert await drain_wake_candidates(registry, spawn) == []
    assert spawned == []


async def test_the_registry_hands_a_flagged_build_out_once_per_window():
    registry = InMemoryRegistry()
    build_id = registry.build_create(root_task_ids=["x"]).id
    registry.build_set_reactive_meta(build_id, app_name="app")
    registry.builds[build_id].needs_tick = True
    spawned, spawn = _spawner()
    assert await drain_wake_candidates(registry, spawn) == [build_id]
    assert await drain_wake_candidates(registry, spawn) == []
