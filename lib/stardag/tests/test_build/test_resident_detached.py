"""The resident engine with a detached executor (a hybrid build's Modal
half, faked): claim before spawn, the execution id forwarded, the ref
recorded, the orphan stopped, and who reports what."""

from __future__ import annotations

import typing

import pytest

from stardag import auto_namespace
from stardag.build import BuildExitStatus, FailMode, build_aio
from stardag.build._registration import new_id
from stardag.target import InMemoryFileTarget
from stardag.testing import InMemoryRegistry
from stardag.testing._registry_state import refuse
from stardag.utils.testing.dynamic_deps_dag import DynamicDepsTask
from stardag.utils.testing.helper_tasks import SyncOnlyTask

from tests.test_build.fakes import FAKE_EXECUTOR, FakeDetachedExecutor

auto_namespace(__name__)

Target = typing.Type[InMemoryFileTarget]


@pytest.fixture
def registry() -> InMemoryRegistry:
    registry = InMemoryRegistry()
    registry.add_deployment(app_name="app")
    return registry


def _executor(**kwargs) -> FakeDetachedExecutor:
    return FakeDetachedExecutor(app_name="app", **kwargs)


async def test_claim_then_spawn_with_its_id_then_the_ref_recorded(
    registry: InMemoryRegistry, default_in_memory_fs_target: Target
):
    task = SyncOnlyTask(name=f"spawned-{new_id()}")
    executor = _executor(timeout_seconds=600)
    summary = await build_aio([task], registry=registry, task_executor=executor)
    assert summary.status == BuildExitStatus.SUCCESS

    claim, ref_start = registry.calls_to("member_start", task_id=task.id)
    assert claim["claim"] is True
    # The claim TTL of a detached execution comes from its executor's timeout.
    assert claim["claim_ttl_seconds"] == 600 + 900
    assert claim["executor_metadata"] == {"kind": FAKE_EXECUTOR, "task": "SyncOnlyTask"}
    ((spawned_task, spawned_execution, spawned_plan),) = executor.spawns
    assert spawned_execution == claim["execution_id"]
    assert spawned_plan == registry.active_plan(summary.build_id).id  # type: ignore[union-attr]
    assert ref_start["claim"] is False
    assert ref_start["execution_id"] == claim["execution_id"]
    assert (ref_start["executor"], ref_start["executor_ref"]) == (
        FAKE_EXECUTOR,
        f"ref-{claim['execution_id']}",
    )
    # A non-reporting worker: the engine reports the completion.
    (complete,) = registry.calls_to("member_complete", task_id=task.id)
    assert complete["execution_id"] == claim["execution_id"]


async def test_a_spawn_failure_fails_the_claimed_execution(
    registry: InMemoryRegistry, default_in_memory_fs_target: Target
):
    task = SyncOnlyTask(name=f"no-spawn-{new_id()}")
    executor = _executor(spawn_error=RuntimeError("backend refused"))
    summary = await build_aio(
        [task], registry=registry, task_executor=executor, fail_mode=FailMode.CONTINUE
    )
    assert summary.status == BuildExitStatus.FAILURE
    (claim,) = registry.calls_to("member_start", task_id=task.id)
    (fail,) = registry.calls_to("member_fail", task_id=task.id)
    assert fail["execution_id"] == claim["execution_id"]
    assert "backend refused" in fail["error_message"]


class _RefusesRefStart(InMemoryRegistry):
    """The claim moves on while the spawn is in flight."""

    code = "execution_not_current"

    def member_start(self, plan_id, task_id, **kwargs):
        if not kwargs.get("claim", True):
            self._record("member_start", plan_id=plan_id, task_id=task_id, **kwargs)
            raise refuse(self.code)
        return super().member_start(plan_id, task_id, **kwargs)


@pytest.mark.parametrize(
    "code", ["execution_not_current", "not_claim_holder", "unknown_execution"]
)
async def test_an_orphaned_spawn_is_stopped_and_nothing_is_reported(
    code: str, default_in_memory_fs_target: Target
):
    """``not_claim_holder`` (the claim is held through another plan) and
    ``unknown_execution`` are "this execution is over" like
    ``execution_not_current``: the spawned container is an orphan."""
    registry = _RefusesRefStart()
    registry.code = code
    registry.add_deployment(app_name="app")
    task = SyncOnlyTask(name=f"orphan-{new_id()}")
    executor = _executor()
    summary = await build_aio(
        [task], registry=registry, task_executor=executor, fail_mode=FailMode.CONTINUE
    )
    assert summary.status == BuildExitStatus.FAILURE
    ((stopped_task, stopped_executor, stopped_ref),) = executor.cancel_detached_calls
    assert stopped_task == task.id
    assert stopped_ref.startswith("ref-")
    assert not registry.called("member_complete", task_id=task.id)
    assert not registry.called("member_fail", task_id=task.id)


async def test_a_self_reporting_worker_reports_and_the_engine_does_not(
    registry: InMemoryRegistry, default_in_memory_fs_target: Target
):
    task = SyncOnlyTask(name=f"reports-{new_id()}")
    executor = _executor(registry=registry, workers=True)
    summary = await build_aio([task], registry=registry, task_executor=executor)
    assert summary.status == BuildExitStatus.SUCCESS
    # Exactly one completion — the worker's.
    assert len(registry.calls_to("member_complete", task_id=task.id)) == 1
    assert registry.status_of(task.id) == "completed"


async def test_a_detached_yield_suspends_and_the_next_run_is_a_new_execution(
    registry: InMemoryRegistry, default_in_memory_fs_target: Target
):
    """The container exits at a yield, so the yield suspends (the claim is
    released) and the engine claims the parent again once its children are
    built — in both reporting modes."""
    for workers in (False, True):
        child = DynamicDepsTask(value=f"child-{new_id()}")
        parent = DynamicDepsTask(value=f"parent-{new_id()}", dynamic_deps=(child,))
        executor = _executor(registry=registry, workers=workers)
        summary = await build_aio([parent], registry=registry, task_executor=executor)
        assert summary.status == BuildExitStatus.SUCCESS, (workers, summary)

        (yield_call,) = registry.calls_to("member_yield", task_id=parent.id)
        assert yield_call["suspend"] is True
        claims = [
            c
            for c in registry.calls_to("member_start", task_id=parent.id)
            if c["claim"]
        ]
        assert len(claims) == 2
        assert claims[0]["execution_id"] == yield_call["execution_id"]
        assert claims[1]["execution_id"] != claims[0]["execution_id"]
        assert registry.executions[claims[0]["execution_id"]].outcome == "suspended"
        assert registry.status_of(parent.id) == "completed"


async def test_a_hybrid_build_wakes_its_flagged_neighbours(
    registry: InMemoryRegistry, default_in_memory_fs_target: Target
):
    neighbour = registry.build_create(root_task_ids=["x"]).id
    registry.build_set_reactive_meta(neighbour, app_name="app")
    registry.builds[neighbour].needs_tick = True
    executor = _executor()
    summary = await build_aio(
        [SyncOnlyTask(name=f"hybrid-{new_id()}")],
        registry=registry,
        task_executor=executor,
    )
    assert summary.status == BuildExitStatus.SUCCESS
    assert (neighbour, "app") in executor.ticks_spawned


async def test_a_throttled_drain_is_deferred_not_dropped(
    registry: InMemoryRegistry, monkeypatch: pytest.MonkeyPatch
):
    """A result inside the drain interval defers its drain to the interval's
    end: nothing else may ask again, and the neighbour it flagged would
    wait for the build's end."""
    import asyncio
    import types

    from stardag.build import _resident
    from stardag.build._concurrency import NoOpConcurrencyLimiter

    monkeypatch.setattr(_resident, "_RESIDENT_DRAIN_INTERVAL_SECONDS", 0.05)
    executor = _executor()
    engine = _resident._ResidentEngine(
        [],
        task_executor=executor,
        fail_mode=FailMode.FAIL_FAST,
        session=types.SimpleNamespace(  # type: ignore[arg-type]
            enabled=True, registry=registry, build_id=None
        ),
        max_concurrent_discover=1,
        register_all=False,
        limiter=NoOpConcurrencyLimiter(),
    )
    await engine.drain_neighbours()  # leading edge: nothing flagged yet
    neighbour = registry.build_create(root_task_ids=["x"]).id
    registry.build_set_reactive_meta(neighbour, app_name="app")
    registry.builds[neighbour].needs_tick = True
    await engine.drain_neighbours()  # throttled
    assert executor.ticks_spawned == []
    await asyncio.sleep(0.1)
    assert executor.ticks_spawned == [(neighbour, "app")]
    await engine.settle_trailing_drain()


async def test_a_drain_asked_for_while_one_is_asking_runs_again(
    registry: InMemoryRegistry, monkeypatch: pytest.MonkeyPatch
):
    """The trailing drain's answer may predate a flag raised while it was
    asking, so a request in that window schedules one more drain."""
    import asyncio
    import types

    from stardag.build import _resident
    from stardag.build._concurrency import NoOpConcurrencyLimiter

    monkeypatch.setattr(_resident, "_RESIDENT_DRAIN_INTERVAL_SECONDS", 0.05)
    asking = asyncio.Event()
    answer = asyncio.Event()
    original = registry.build_wake_candidates_aio

    async def slow_candidates(*args, **kwargs):
        candidates = await original(*args, **kwargs)
        if not asking.is_set():
            asking.set()
            await answer.wait()
        return candidates

    monkeypatch.setattr(registry, "build_wake_candidates_aio", slow_candidates)
    executor = _executor()
    engine = _resident._ResidentEngine(
        [],
        task_executor=executor,
        fail_mode=FailMode.FAIL_FAST,
        session=types.SimpleNamespace(  # type: ignore[arg-type]
            enabled=True, registry=registry, build_id=None
        ),
        max_concurrent_discover=1,
        register_all=False,
        limiter=NoOpConcurrencyLimiter(),
    )
    engine._last_drain = asyncio.get_running_loop().time()
    await engine.drain_neighbours()  # deferred
    await asyncio.wait_for(asking.wait(), timeout=1)
    neighbour = registry.build_create(root_task_ids=["x"]).id
    registry.build_set_reactive_meta(neighbour, app_name="app")
    registry.builds[neighbour].needs_tick = True
    await engine.drain_neighbours()  # while the trailing one is asking
    answer.set()
    await asyncio.sleep(0.2)
    assert executor.ticks_spawned == [(neighbour, "app")]
    await engine.settle_trailing_drain()
