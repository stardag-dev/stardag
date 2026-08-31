"""Cross-build wake-ups, SDK side: the scheduler drains the registry's
wake candidates and spawns their ticks.

The registry flags builds; only something with an executor can spawn. These
tests pin *when* the tick and the resident engine ask, that a spawner is
called once per handed-out build, and that every failure degrades to "the
flag stays set" rather than to a failed pass.
"""

from __future__ import annotations

import dataclasses
import typing
from uuid import UUID, uuid4

import pytest

from stardag import BaseTask
from stardag.build import build_aio, discover_and_register_aio, run_tick_aio
from stardag.build import _wakeups as wakeups_module
from stardag.build._base import TaskExecutorABC
from stardag.build._wakeups import drain_wake_candidates
from stardag.exceptions import APIError, NotFoundError
from stardag.registry import WakeCandidate
from stardag.target._in_memory import InMemoryFileTarget
from stardag.utils.testing.helper_tasks import SyncOnlyTask
from tests.test_build.conftest import RecordingRegistry
from tests.test_build.test_detached import FakeDetachedExecutor
from tests.test_build.test_reactive import (
    FAST_TICK,
    FakeReactiveRegistry,
    FakeTickExecutor,
    InMemoryTaskStore,
    _chain,
)


@pytest.fixture(autouse=True)
def _reset_route_flag():
    wakeups_module._wake_candidates_route_missing = False
    yield
    wakeups_module._wake_candidates_route_missing = False


class WakingRegistry(FakeReactiveRegistry):
    """FakeReactiveRegistry whose wake-candidates answer is scripted.

    ``candidates`` is handed out once, the way the server hands each build
    out once per window; ``candidate_calls`` counts the asks.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.candidates: list[WakeCandidate] = []
        self.candidate_calls = 0
        self.candidates_error: Exception | None = None
        self.bulk_limit_keys: list[typing.Any] = []

    async def build_wake_candidates_aio(self, limit: int = 20) -> list[WakeCandidate]:
        self.candidate_calls += 1
        if self.candidates_error is not None:
            raise self.candidates_error
        handed, self.candidates = self.candidates, []
        return handed

    async def task_register_bulk_aio(self, build_id, tasks, *, limit_keys=None):
        self.bulk_limit_keys.append(limit_keys)
        return await super().task_register_bulk_aio(build_id, tasks)


def _candidate(app: str = "app-b") -> WakeCandidate:
    return WakeCandidate(build_id=uuid4(), reactive_app_name=app)


def _spawner() -> tuple[list[tuple[UUID, str]], typing.Callable[[UUID, str], None]]:
    spawned: list[tuple[UUID, str]] = []

    def spawn(build_id: UUID, app_name: str) -> None:
        spawned.append((build_id, app_name))

    return spawned, spawn


def _tick_setup(
    tasks: list[BaseTask], *, auto_complete: bool = True, lease_acquired: bool = True
):
    from stardag import flatten_task_struct

    root = tasks[-1]
    registry = WakingRegistry(root_task_ids=[str(root.id)], auto_complete=auto_complete)
    for task in tasks:
        registry.add_task(
            str(task.id),
            upstreams={str(d.id) for d in flatten_task_struct(task.requires())},
        )
    store = InMemoryTaskStore(uuid4())
    store.save_tasks(tasks)
    registry.lease_acquired = lease_acquired
    return registry, FakeTickExecutor(), store


# --- the helper itself -------------------------------------------------------


class TestDrainWakeCandidates:
    async def test_spawns_once_per_candidate_with_its_app(self):
        registry = WakingRegistry(root_task_ids=[])
        a, b = _candidate("app-a"), _candidate("app-b")
        registry.candidates = [a, b]
        spawned, spawn = _spawner()

        assert await drain_wake_candidates(registry, spawn) == [a.build_id, b.build_id]
        assert spawned == [(a.build_id, "app-a"), (b.build_id, "app-b")]

    async def test_one_failing_spawn_does_not_stop_the_rest(self):
        registry = WakingRegistry(root_task_ids=[])
        a, b = _candidate("gone"), _candidate("app-b")
        registry.candidates = [a, b]
        spawned, spawn = _spawner()

        def flaky(build_id: UUID, app_name: str) -> None:
            if app_name == "gone":
                raise RuntimeError("app deleted")
            spawn(build_id, app_name)

        assert await drain_wake_candidates(registry, flaky) == [b.build_id]
        assert spawned == [(b.build_id, "app-b")]

    async def test_a_missing_route_disables_the_drain_for_the_process(self):
        """An older registry: cross-build wake-ups are the watchdog's job,
        and every later pass must not pay for a doomed request."""
        registry = WakingRegistry(root_task_ids=[])
        registry.candidates_error = NotFoundError("Not Found", detail="Not Found")
        spawned, spawn = _spawner()

        assert await drain_wake_candidates(registry, spawn) == []
        assert wakeups_module._wake_candidates_route_missing is True
        registry.candidates_error = None
        registry.candidates = [_candidate()]
        assert await drain_wake_candidates(registry, spawn) == []
        assert registry.candidate_calls == 1

    async def test_a_405_also_means_the_route_is_missing(self):
        """The route sits under /builds, so a server that has
        GET /builds/{build_id} and not this route answers 405, not 404."""
        registry = WakingRegistry(root_task_ids=[])
        registry.candidates_error = APIError("Method Not Allowed", status_code=405)
        spawned, spawn = _spawner()
        assert await drain_wake_candidates(registry, spawn) == []
        assert wakeups_module._wake_candidates_route_missing is True

    async def test_a_registry_error_is_swallowed(self):
        registry = WakingRegistry(root_task_ids=[])
        registry.candidates_error = RuntimeError("503")
        spawned, spawn = _spawner()
        assert await drain_wake_candidates(registry, spawn) == []
        assert wakeups_module._wake_candidates_route_missing is False


# --- the tick ----------------------------------------------------------------


class TestTickDrainsNeighbours:
    async def test_drained_after_a_pass_that_acted(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """A pass that spawned or healed something may have flagged a
        neighbour; the tick asks right then, not minutes later at exit."""
        tasks = _chain("leaf", "root")
        registry, executor, store = _tick_setup(tasks, auto_complete=False)
        neighbour = _candidate()
        registry.candidates = [neighbour]
        spawned, spawn = _spawner()

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=dataclasses.replace(FAST_TICK, spawn_tick=spawn),
        )

        assert summary.spawned == 1  # the leaf
        assert (neighbour.build_id, "app-b") in spawned
        assert summary.neighbour_ticks_spawned == 1
        # Asked after the acting pass AND on exit — the exit ask found
        # nothing, since the fake hands each candidate out once.
        assert registry.candidate_calls >= 2

    async def test_drained_on_a_terminal_exit(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The finishing build's last completion is often exactly what
        unblocked a neighbour, so terminal is not exempt from the drain."""
        (root,) = _chain("done-root")
        registry, executor, store = _tick_setup([root])
        registry.statuses[str(root.id)] = "completed"
        neighbour = _candidate()
        registry.candidates = [neighbour]
        spawned, spawn = _spawner()

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=dataclasses.replace(FAST_TICK, spawn_tick=spawn),
        )

        assert summary.outcome == "terminal"
        assert spawned == [(neighbour.build_id, "app-b")]
        assert summary.neighbour_ticks_spawned == 1

    async def test_drained_when_lingering_out(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        (root,) = _chain("waiting-root")
        registry, executor, store = _tick_setup([root], auto_complete=False)
        registry.add_task(
            str(root.id), status="running", executor="fake", executor_ref="r"
        )
        neighbour = _candidate()
        registry.candidates = [neighbour]
        spawned, spawn = _spawner()

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=dataclasses.replace(FAST_TICK, spawn_tick=spawn),
        )

        assert summary.outcome == "lingered_out"
        assert spawned == [(neighbour.build_id, "app-b")]

    async def test_not_drained_when_the_lease_is_held(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The holder drains on its own passes; a no-op tick asking too
        would only race it for the same hand-out."""
        (root,) = _chain("held-root")
        registry, executor, store = _tick_setup([root], lease_acquired=False)
        registry.candidates = [_candidate()]
        spawned, spawn = _spawner()

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=dataclasses.replace(FAST_TICK, spawn_tick=spawn),
        )

        assert summary.outcome == "lease_held"
        assert spawned == []
        assert registry.candidate_calls == 0

    async def test_not_drained_without_a_spawner(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """No ``spawn_tick`` means no way to act on an answer, so the
        question is not asked — the same rule as the exit hand-off."""
        (root,) = _chain("spawnerless-root")
        registry, executor, store = _tick_setup([root])
        registry.statuses[str(root.id)] = "completed"
        registry.candidates = [_candidate()]

        await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=FAST_TICK,
        )

        assert registry.candidate_calls == 0

    async def test_own_build_handed_out_by_the_drain_replaces_the_hand_off(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """A wake-up that landed while this tick held the lease makes the
        tick's own build a candidate once the lease is released. The drain
        runs first and spawns it — counted as the successor it is — and the
        hand-off must then NOT spawn a second one."""
        (root,) = _chain("drained-own-root")
        registry, executor, store = _tick_setup([root], auto_complete=False)
        registry.add_task(
            str(root.id), status="running", executor="fake", executor_ref="r"
        )
        registry.reactive_app_name = "my-app"
        build_id = uuid4()

        def on_release() -> None:
            registry.needs_tick = True
            registry.candidates = [
                WakeCandidate(build_id=build_id, reactive_app_name="my-app")
            ]

        registry.lease_on_release = on_release
        spawned, spawn = _spawner()

        summary = await run_tick_aio(
            build_id,
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=dataclasses.replace(FAST_TICK, spawn_tick=spawn),
        )

        assert spawned == [(build_id, "my-app")]
        assert summary.successor_spawned == 1
        assert summary.neighbour_ticks_spawned == 0

    async def test_hand_off_spawns_on_the_builds_own_app(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The exit hand-off shares the spawner with the drain, so it has
        to name the app: the build's own, read off the frontier."""
        (root,) = _chain("handoff-root")
        registry, executor, store = _tick_setup([root], auto_complete=False)
        registry.add_task(
            str(root.id), status="running", executor="fake", executor_ref="r"
        )
        registry.reactive_app_name = "my-app"
        registry.lease_on_release = lambda: setattr(registry, "needs_tick", True)
        spawned, spawn = _spawner()
        build_id = uuid4()

        summary = await run_tick_aio(
            build_id,
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=dataclasses.replace(FAST_TICK, spawn_tick=spawn),
        )

        assert summary.successor_spawned == 1
        assert (build_id, "my-app") in spawned


# --- discovery registers plan-time limit keys ----------------------------------


class TestDiscoveryRegistersLimitKeys:
    async def test_keys_are_sent_per_task(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        leaf, root = _chain("keyed-leaf", "keyed-root")
        registry = WakingRegistry(root_task_ids=[str(root.id)], auto_complete=False)

        def selector(task: BaseTask) -> list[str]:
            return ["gpu"] if task.id == leaf.id else []

        await discover_and_register_aio(
            registry, uuid4(), (root,), limit_key_selector=selector
        )

        (keys,) = registry.bulk_limit_keys
        assert keys == {leaf.id: ["gpu"], root.id: []}

    async def test_no_selector_sends_nothing(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """An absent mapping leaves any recorded keys alone server-side —
        which is what a caller without a selector must do."""
        (root,) = _chain("unkeyed-root")
        registry = WakingRegistry(root_task_ids=[str(root.id)], auto_complete=False)
        await discover_and_register_aio(registry, uuid4(), (root,))
        assert registry.bulk_limit_keys == [None]

    async def test_a_raising_selector_fails_discovery(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """One error policy: the tick's spawn path calls the selector
        unguarded, so plan time does not hide what it would raise later."""
        (root,) = _chain("raising-root")
        registry = WakingRegistry(root_task_ids=[str(root.id)], auto_complete=False)

        def selector(task: BaseTask) -> list[str]:
            raise ValueError("no")

        with pytest.raises(ValueError):
            await discover_and_register_aio(
                registry, uuid4(), (root,), limit_key_selector=selector
            )
        assert registry.bulk_limit_keys == []


# --- the resident engine -----------------------------------------------------


class WakingRecordingRegistry(RecordingRegistry):
    def __init__(self) -> None:
        super().__init__()
        self.candidates: list[WakeCandidate] = []
        self.candidate_calls = 0

    async def build_wake_candidates_aio(self, limit: int = 20) -> list[WakeCandidate]:
        self.candidate_calls += 1
        handed, self.candidates = self.candidates, []
        return handed


class SpawningExecutor(FakeDetachedExecutor):
    """A detached executor that can reach a deployed tick — a hybrid run."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.ticks: list[tuple[UUID, str]] = []

    def can_spawn_scheduler_ticks(self) -> bool:
        return True

    def spawn_scheduler_tick(self, build_id: UUID, app_name: str) -> None:
        self.ticks.append((build_id, app_name))


class TestResidentEngineDrainsNeighbours:
    async def test_hybrid_build_spawns_ticks_for_handed_out_builds(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        registry = WakingRecordingRegistry()
        neighbour = _candidate("their-app")
        registry.candidates = [neighbour]
        executor = SpawningExecutor()

        await build_aio(
            [SyncOnlyTask(name="hybrid")], task_executor=executor, registry=registry
        )

        assert executor.ticks == [(neighbour.build_id, "their-app")]

    async def test_local_only_executor_never_asks(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """A purely local executor has nobody to spawn with, so the engine
        does not even ask the registry."""
        registry = WakingRecordingRegistry()
        registry.candidates = [_candidate()]

        await build_aio(
            [SyncOnlyTask(name="local")],
            task_executor=FakeDetachedExecutor(),
            registry=registry,
        )

        assert registry.candidate_calls == 0

    def test_default_executor_cannot_spawn(self):
        class Bare(TaskExecutorABC):
            async def submit(self, task):  # pragma: no cover - never called
                return None

            async def setup(self) -> None:
                pass

            async def teardown(self) -> None:
                pass

        assert Bare().can_spawn_scheduler_ticks() is False
        with pytest.raises(NotImplementedError):
            Bare().spawn_scheduler_tick(uuid4(), "x")
