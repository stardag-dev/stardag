"""Unit tests for reactive tick scheduling (stardag.build._reactive).

Uses an in-memory fake registry that mirrors the API's frontier semantics
(dependency gating on task statuses), driven entirely by the tick's own
event calls — plus a fake detached executor whose "workers" complete
instantly (simulating worker-side lifecycle reporting + wake-up).
"""

from __future__ import annotations

import typing
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4


import pytest

from stardag import BaseTask, auto_namespace, flatten_task_struct
from stardag.build import (
    BuildTaskStore,
    DetachedExecutionStatus,
    DetachedHandle,
    FailMode,
    TaskExecutorABC,
    TickConfig,
    discover_and_register_aio,
    run_tick_aio,
)
from stardag.build._base import (
    GlobalConcurrencyLockManager,
    LockAcquisitionResult,
    LockAcquisitionStatus,
)
from stardag.build._reactive import TickSummary, _skip_blocked
from stardag.exceptions import NotFoundError
from stardag.registry import (
    BuildFrontier,
    FrontierTaskRef,
    NoOpRegistry,
    RegisteredTaskInfo,
)
from stardag.target import InMemoryFileTarget
from stardag.utils.testing.helper_tasks import SyncOnlyTask

auto_namespace(__name__)

FAST_TICK = TickConfig(linger_seconds=0.3, poll_interval_seconds=0.01)


# =============================================================================
# Fakes
# =============================================================================


class FakeReactiveRegistry(NoOpRegistry):
    """In-memory registry with frontier semantics mirroring the API.

    State transitions are driven by the tick's own calls (and by tests
    setting up preconditions). ``auto_complete`` simulates instant workers:
    a task transitions straight to completed when its start is recorded,
    with the wake-up flag set (as a self-reporting worker would).
    """

    def __init__(
        self,
        *,
        root_task_ids: list[str],
        auto_complete: bool = False,
    ) -> None:
        super().__init__()
        self.root_task_ids = root_task_ids
        self.auto_complete = auto_complete
        self.statuses: dict[str, str] = {}
        self.upstreams: dict[str, set[str]] = {}
        self.refs: dict[str, tuple[str | None, str | None]] = {}
        self.needs_tick = False
        self.build_status = "running"
        self.calls: list[tuple[str, str | None]] = []
        # Named concurrency limits: key -> cap; holders tracked per task.
        self.limits: dict[str, int] = {}
        self.task_limit_keys: dict[str, set[str]] = {}
        self.status_at: dict[str, datetime] = {}

    # --- test setup helpers ---

    def add_task(
        self,
        task_id: str,
        status: str = "pending",
        upstreams: set[str] | None = None,
        executor: str | None = None,
        executor_ref: str | None = None,
        status_at: "datetime | None" = None,
    ) -> None:
        self.statuses[task_id] = status
        self.upstreams.setdefault(task_id, set()).update(upstreams or set())
        if executor or executor_ref:
            self.refs[task_id] = (executor, executor_ref)
        if status_at is not None:
            self.status_at[task_id] = status_at

    # --- registry surface used by the tick ---

    async def task_register_bulk_aio(self, build_id, tasks):
        infos = []
        for task in tasks:
            tid = str(task.id)
            self.statuses.setdefault(tid, "pending")
            self.upstreams.setdefault(tid, set()).update(
                str(dep.id)
                for dep in __import__("stardag").flatten_task_struct(task.requires())
            )
            self.calls.append(("register", tid))
            executor, executor_ref = self.refs.get(tid, (None, None))
            infos.append(
                RegisteredTaskInfo(
                    task_id=tid,
                    latest_status=self.statuses[tid],
                    latest_executor=executor,
                    latest_executor_ref=executor_ref,
                )
            )
        return infos

    async def task_start_aio(self, build_id, task, executor=None, executor_ref=None):
        tid = str(task.id)
        self.calls.append(("start", tid))
        self.statuses[tid] = "running"
        self.refs[tid] = (executor, executor_ref)
        if self.auto_complete:
            # Instant worker: completes and wakes the scheduler.
            self.statuses[tid] = "completed"
            self.needs_tick = True

    async def task_start_with_limits_aio(
        self, build_id, task, executor=None, executor_ref=None, limit_keys=None
    ):
        # Mirrors the API's semantics: count running holders per key against
        # configured caps (self.limits); all-or-nothing acquisition.
        self.calls.append(("start_with_limits", str(task.id)))
        for key in limit_keys or []:
            cap = self.limits.get(key)
            if cap is None:
                continue
            active = sum(
                1
                for tid, keys in self.task_limit_keys.items()
                if key in keys
                and self.statuses.get(tid) == "running"
                and tid != str(task.id)
            )
            if active >= cap:
                return False
        self.task_limit_keys[str(task.id)] = set(limit_keys or [])
        await self.task_start_aio(
            build_id, task, executor=executor, executor_ref=executor_ref
        )
        return True

    async def task_complete_aio(self, build_id, task):
        tid = str(task.id)
        self.calls.append(("complete", tid))
        self.statuses[tid] = "completed"

    async def task_retry_aio(self, build_id, task):
        tid = str(task.id)
        self.calls.append(("retry", tid))
        if self.statuses.get(tid) in ("failed", "cancelled", "skipped"):
            self.statuses[tid] = "pending"
            self.refs.pop(tid, None)

    async def build_add_roots_aio(self, build_id, root_task_ids):
        self.calls.append(("add_roots", ",".join(root_task_ids)))
        self.root_task_ids += [t for t in root_task_ids if t not in self.root_task_ids]

    async def task_fail_aio(self, build_id, task, error_message=None):
        tid = str(task.id)
        self.calls.append(("fail", tid))
        self.statuses[tid] = "failed"

    async def build_complete_aio(self, build_id):
        self.calls.append(("build_complete", None))
        self.build_status = "completed"

    async def build_fail_aio(self, build_id, error_message=None):
        self.calls.append(("build_fail", None))
        self.build_status = "failed"

    async def build_skip_blocked_aio(self, build_id):
        # Mirrors the API: pending/suspended tasks transitively downstream
        # of a failed/cancelled/skipped task become skipped.
        self.calls.append(("skip_blocked", None))
        blocked = {
            tid
            for tid, status in self.statuses.items()
            if status in ("failed", "cancelled", "skipped")
        }
        changed = True
        while changed:
            changed = False
            for tid, ups in self.upstreams.items():
                if tid not in blocked and ups & blocked:
                    blocked.add(tid)
                    changed = True
        skipped = []
        for tid in blocked:
            if self.statuses.get(tid) in ("pending", "suspended"):
                self.statuses[tid] = "skipped"
                skipped.append(tid)
        return skipped

    async def build_notify_aio(self, build_id):
        self.needs_tick = True

    async def build_clear_notify_aio(self, build_id):
        self.needs_tick = False

    async def build_get_frontier_aio(self, build_id) -> BuildFrontier:
        def ref(tid: str) -> FrontierTaskRef:
            executor, executor_ref = self.refs.get(tid, (None, None))
            return FrontierTaskRef(
                task_id=tid,
                latest_status=self.statuses[tid],
                latest_executor=executor,
                latest_executor_ref=executor_ref,
                latest_status_at=self.status_at.get(tid),
            )

        actionable = [
            ref(tid)
            for tid, status in self.statuses.items()
            if status in ("pending", "suspended", "running")
            and all(
                self.statuses.get(up) == "completed"
                for up in self.upstreams.get(tid, set())
            )
        ]
        counts: dict[str, int] = {}
        for status in self.statuses.values():
            counts[status] = counts.get(status, 0) + 1
        return BuildFrontier(
            build_id=build_id,
            build_status=self.build_status,
            needs_tick=self.needs_tick,
            root_task_ids=self.root_task_ids,
            roots=[ref(t) for t in self.root_task_ids if t in self.statuses],
            status_counts=counts,
            actionable=actionable,
            running=[
                ref(tid) for tid, status in self.statuses.items() if status == "running"
            ],
        )


class FakeLease:
    def __init__(self, acquired: bool):
        self.result = LockAcquisitionResult(
            status=(
                LockAcquisitionStatus.ACQUIRED
                if acquired
                else LockAcquisitionStatus.HELD_BY_OTHER
            ),
            acquired=acquired,
        )

    def mark_completed(self) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class FakeLockManager:
    def __init__(self, acquired: bool = True):
        self.acquired = acquired
        self.lock_names: list[str] = []

    def lock(self, task_id: str) -> FakeLease:
        self.lock_names.append(task_id)
        return FakeLease(self.acquired)

    async def acquire(self, task_id: str) -> LockAcquisitionResult:
        raise NotImplementedError

    async def release(self, task_id: str, task_completed: bool = False) -> bool:
        return True


class FakeTickExecutor(TaskExecutorABC):
    """Detached executor for ticks: spawn only (results never awaited)."""

    def __init__(self, statuses: dict[str, DetachedExecutionStatus] | None = None):
        # ref -> probe status
        self.probe_statuses = statuses or {}
        self.spawned: list[UUID] = []
        self.cancelled_refs: list[str] = []
        self._spawn_count = 0

    async def submit(self, task):
        raise AssertionError("ticks must not use blocking submit")

    def supports_detached(self, task: BaseTask) -> bool:
        return True

    async def submit_detached(self, task: BaseTask) -> DetachedHandle:
        self.spawned.append(task.id)
        self._spawn_count += 1

        async def wait():
            raise AssertionError("ticks must not await detached results")

        return DetachedHandle(
            executor="fake", ref=f"ref-{self._spawn_count}", wait=wait
        )

    async def detached_status(self, task, executor, ref):
        return self.probe_statuses.get(ref, DetachedExecutionStatus.UNKNOWN)

    async def cancel_detached(self, task, executor, ref):
        self.cancelled_refs.append(ref)

    async def setup(self):
        pass

    async def teardown(self):
        pass


class InMemoryTaskStore(BuildTaskStore):
    """BuildTaskStore on a dict — no target roots needed in engine tests."""

    def __init__(self, build_id: UUID, reactive: bool = True):
        super().__init__(build_id)
        self._meta: dict | None = {"reactive": True} if reactive else None
        self._tasks: dict[str, BaseTask] = {}

    def write_meta(self, meta):
        self._meta = meta

    def read_meta(self):
        return self._meta

    def save_task(self, task: BaseTask) -> None:
        self._tasks[str(task.id)] = task

    def load_task(self, task_id):
        return self._tasks.get(str(task_id))


# =============================================================================
# Helpers
# =============================================================================


def _chain(*names: str) -> list[BaseTask]:
    """Build a linear chain of SyncOnlyTasks: first is leaf, last is root."""
    tasks: list[BaseTask] = []
    prev: tuple = ()
    for name in names:
        task = SyncOnlyTask(name=name, deps=prev)
        tasks.append(task)
        prev = (task,)
    return tasks


def _lock_manager(acquired: bool = True) -> "GlobalConcurrencyLockManager":
    # The fakes satisfy the runtime contract; cast for the type checker
    # (LockHandle is structurally stricter than the tests need).
    return typing.cast("GlobalConcurrencyLockManager", FakeLockManager(acquired))


def _setup(
    tasks: list[BaseTask],
    *,
    auto_complete: bool = True,
    lease_acquired: bool = True,
    executor: FakeTickExecutor | None = None,
) -> tuple[
    FakeReactiveRegistry,
    "GlobalConcurrencyLockManager",
    FakeTickExecutor,
    InMemoryTaskStore,
]:
    root = tasks[-1]
    registry = FakeReactiveRegistry(
        root_task_ids=[str(root.id)], auto_complete=auto_complete
    )
    for task in tasks:
        registry.add_task(
            str(task.id),
            upstreams={str(d.id) for d in flatten_task_struct(task.requires())},
        )
    store = InMemoryTaskStore(uuid4())
    store.save_tasks(tasks)
    return (
        registry,
        _lock_manager(lease_acquired),
        executor or FakeTickExecutor(),
        store,
    )


# =============================================================================
# Tests
# =============================================================================


class TestTickHappyPath:
    async def test_completes_chain_within_one_lingering_tick(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """Instant workers: one tick drives dep → root → BUILD_COMPLETED via
        linger wake-ups, spawning in dependency order."""
        dep, root = _chain("tick-dep", "tick-root")
        registry, locks, executor, store = _setup([dep, root])

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            lock_manager=locks,
            task_store=store,
            config=FAST_TICK,
        )

        assert summary.outcome == "terminal"
        assert summary.terminal_status == "completed"
        assert executor.spawned == [dep.id, root.id]
        assert registry.build_status == "completed"
        assert summary.spawned == 2

    async def test_lease_held_is_noop(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        dep, root = _chain("lease-dep", "lease-root")
        registry, locks, executor, store = _setup([dep, root], lease_acquired=False)

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            lock_manager=locks,
            task_store=store,
            config=FAST_TICK,
        )

        assert summary.outcome == "lease_held"
        assert executor.spawned == []
        assert registry.calls == []

    async def test_not_reactive_build_is_noop(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        (root,) = _chain("not-reactive-root")
        registry, locks, executor, _ = _setup([root])
        store = InMemoryTaskStore(uuid4(), reactive=False)

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            lock_manager=locks,
            task_store=store,
            config=FAST_TICK,
        )

        assert summary.outcome == "not_reactive"
        assert executor.spawned == []
        assert (
            typing.cast(FakeLockManager, locks).lock_names == []
        )  # lease not attempted


class TestRunningTaskResolution:
    async def test_live_ref_left_alone(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        (root,) = _chain("live-root")
        executor = FakeTickExecutor(
            statuses={"fc-live": DetachedExecutionStatus.RUNNING}
        )
        registry, locks, executor, store = _setup(
            [root], auto_complete=False, executor=executor
        )
        registry.add_task(
            str(root.id), status="running", executor="fake", executor_ref="fc-live"
        )

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            lock_manager=locks,
            task_store=store,
            config=FAST_TICK,
        )

        assert summary.outcome == "lingered_out"
        assert executor.spawned == []
        assert summary.self_healed == 0

    async def test_target_exists_self_heals_completion(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """Worker wrote the output then died before reporting: the tick
        emits the completion (target is ground truth) and the build
        finishes."""
        (root,) = _chain("heal-root")
        root.run()  # target now exists
        registry, locks, executor, store = _setup([root], auto_complete=False)
        registry.add_task(
            str(root.id), status="running", executor="fake", executor_ref="fc-gone"
        )

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            lock_manager=locks,
            task_store=store,
            config=FAST_TICK,
        )

        assert summary.outcome == "terminal"
        assert summary.terminal_status == "completed"
        assert summary.self_healed == 1
        assert ("complete", str(root.id)) in registry.calls
        assert executor.spawned == []

    async def test_failed_ref_records_failure_and_fails_build(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        (root,) = _chain("failed-ref-root")
        executor = FakeTickExecutor(
            statuses={"fc-dead": DetachedExecutionStatus.FAILED}
        )
        registry, locks, _, store = _setup(
            [root], auto_complete=False, executor=executor
        )
        registry.add_task(
            str(root.id), status="running", executor="fake", executor_ref="fc-dead"
        )

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            lock_manager=locks,
            task_store=store,
            config=FAST_TICK,
        )

        assert summary.failed_recorded == 1
        assert summary.outcome == "terminal"
        assert summary.terminal_status == "failed"
        assert registry.build_status == "failed"

    async def test_unknown_ref_left_alone(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """UNKNOWN probe status → conservatively leave (no duplicate spawn)."""
        (root,) = _chain("unknown-ref-root")
        registry, locks, executor, store = _setup([root], auto_complete=False)
        registry.add_task(
            str(root.id),
            status="running",
            executor="other-backend",
            executor_ref="job-1",
        )

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            lock_manager=locks,
            task_store=store,
            config=FAST_TICK,
        )

        assert summary.outcome == "lingered_out"
        assert executor.spawned == []


class TestTerminalHandling:
    async def test_cancelled_build_cancels_running_refs(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        (root,) = _chain("cancelled-root")
        registry, locks, executor, store = _setup([root], auto_complete=False)
        registry.add_task(
            str(root.id), status="running", executor="fake", executor_ref="fc-run"
        )
        registry.build_status = "cancelled"

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            lock_manager=locks,
            task_store=store,
            config=FAST_TICK,
        )

        assert summary.outcome == "terminal"
        assert summary.terminal_status == "cancelled"
        assert executor.cancelled_refs == ["fc-run"]
        assert executor.spawned == []

    async def test_blocked_build_fails_instead_of_idling(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """CONTINUE mode with a failed upstream: nothing runnable/running →
        the tick fails the build rather than idling forever."""
        dep, root = _chain("blocked-dep", "blocked-root")
        registry, locks, executor, store = _setup([dep, root], auto_complete=False)
        registry.add_task(str(dep.id), status="failed")

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            lock_manager=locks,
            task_store=store,
            config=TickConfig(
                linger_seconds=0.3,
                poll_interval_seconds=0.01,
                fail_mode=FailMode.CONTINUE,
            ),
        )

        assert summary.outcome == "terminal"
        assert summary.terminal_status == "failed"
        assert registry.build_status == "failed"

    async def test_fail_fast_cancels_running_and_fails(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        dep, root = _chain("ff-dep", "ff-root")
        other = SyncOnlyTask(name="ff-other")
        registry, locks, executor, store = _setup([dep, root], auto_complete=False)
        store.save_task(other)
        registry.add_task(str(dep.id), status="failed")
        registry.add_task(
            str(other.id), status="running", executor="fake", executor_ref="fc-x"
        )
        executor.probe_statuses["fc-x"] = DetachedExecutionStatus.RUNNING

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            lock_manager=locks,
            task_store=store,
            config=FAST_TICK,  # FAIL_FAST default
        )

        assert summary.outcome == "terminal"
        assert summary.terminal_status == "failed"
        assert executor.cancelled_refs == ["fc-x"]
        assert registry.build_status == "failed"


class TestDiscoverAndRegister:
    async def test_post_order_registration_and_previously_completed(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        done_leaf = SyncOnlyTask(name="disc-done-leaf")
        done_leaf.run()  # complete
        fresh_leaf = SyncOnlyTask(name="disc-fresh-leaf")
        root = SyncOnlyTask(name="disc-root", deps=(done_leaf, fresh_leaf))

        registry = FakeReactiveRegistry(root_task_ids=[str(root.id)])
        result = await discover_and_register_aio(registry, uuid4(), root)

        assert set(result.incomplete) == {fresh_leaf.id, root.id}
        assert [t.id for t in result.previously_completed] == [done_leaf.id]
        register_order = [tid for (m, tid) in registry.calls if m == "register"]
        assert register_order.index(str(fresh_leaf.id)) < register_order.index(
            str(root.id)
        )
        # Previously-complete tasks are reflected as completed in the registry
        # (the frontier is the scheduler state).
        assert registry.statuses[str(done_leaf.id)] == "completed"


class TestBuildTaskStoreRoundTrip:
    def test_pickle_round_trip_and_meta(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        build_id = uuid4()
        store = BuildTaskStore(build_id)
        assert store.read_meta() is None  # not reactive until written

        task = SyncOnlyTask(name="store-roundtrip")
        store.write_meta({"reactive": True, "app_name": "test-app"})
        store.save_tasks([task])

        assert store.read_meta() == {"reactive": True, "app_name": "test-app"}
        loaded = store.load_task(task.id)
        assert loaded is not None
        assert loaded.id == task.id
        assert isinstance(loaded, SyncOnlyTask)
        assert loaded.name == "store-roundtrip"

    def test_load_missing_task_returns_none(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        store = BuildTaskStore(uuid4())
        assert store.load_task(uuid4()) is None


class TestMissingTaskStoreEntry:
    async def test_missing_pickle_fails_task_instead_of_stalling(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """A pending actionable task whose object is missing from the store
        can never be scheduled — the tick fails it (and thereby the build)
        rather than leaving it in the frontier forever, where endless
        watchdog ticks would do nothing."""
        (root,) = _chain("missing-pickle-root")
        registry, locks, executor, store = _setup([root], auto_complete=False)
        store._tasks.clear()  # simulate a lost/never-written pickle

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            lock_manager=locks,
            task_store=store,
            config=FAST_TICK,
        )

        assert summary.failed_recorded == 1
        assert summary.outcome == "terminal"
        assert summary.terminal_status == "failed"
        assert registry.build_status == "failed"
        assert executor.spawned == []


class TestRetryPath:
    async def test_retry_failed_discovery_resets_and_build_completes(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The reactive retry path: a task failed in a previous run is reset
        to pending by discovery (retry_failed=True) and the re-triggered
        build runs to completion instead of FAIL_FASTing on tick 1."""
        (root,) = _chain("retry-root")
        registry, locks, executor, store = _setup([root])
        registry.add_task(str(root.id), status="failed")

        # What the reactive trigger does on (re-)trigger:
        result = await discover_and_register_aio(
            registry, uuid4(), root, retry_failed=True
        )
        assert [t.id for t in result.retried] == [root.id]
        assert registry.statuses[str(root.id)] == "pending"

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            lock_manager=locks,
            task_store=store,
            config=FAST_TICK,
        )
        assert summary.terminal_status == "completed"
        assert registry.build_status == "completed"

    async def test_without_retry_failed_build_fail_fasts(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """Control: without the retry, the failed status poisons the build
        (the pre-fix behavior the stack review flagged)."""
        (root,) = _chain("poison-root")
        registry, locks, executor, store = _setup([root])
        registry.add_task(str(root.id), status="failed")

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            lock_manager=locks,
            task_store=store,
            config=FAST_TICK,
        )
        assert summary.terminal_status == "failed"


class TestAddedRootsTerminalDetection:
    async def test_added_roots_gate_completion(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """Roots appended mid-build keep the build running until they too
        complete (previously completion of the original roots stranded
        re-triggered subtrees)."""
        (r1,) = _chain("roots-r1")
        r2 = SyncOnlyTask(name="roots-r2")
        registry, locks, executor, store = _setup([r1])
        store.save_task(r2)
        # Original root completed already; new root appended (as the
        # re-trigger path does server-side) but still pending.
        registry.statuses[str(r1.id)] = "completed"
        await registry.build_add_roots_aio(uuid4(), [str(r2.id)])
        registry.add_task(str(r2.id), status="pending")

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            lock_manager=locks,
            task_store=store,
            config=FAST_TICK,
        )
        # r2 was spawned (auto-completes) and only then the build completed.
        assert executor.spawned == [r2.id]
        assert summary.terminal_status == "completed"


class TestCancelDynamicDepWindow:
    async def test_cancel_reaches_non_actionable_running(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """A RUNNING task inside the dynamic-dep window (incomplete upstream
        → not actionable) is still cancelled on build cancellation."""
        blocker = SyncOnlyTask(name="cxl-blocker")
        runner = SyncOnlyTask(name="cxl-runner")
        registry, locks, executor, store = _setup(
            [blocker, runner], auto_complete=False
        )
        registry.add_task(
            str(runner.id),
            status="running",
            upstreams={str(blocker.id)},  # dynamic edge, blocker incomplete
            executor="fake",
            executor_ref="fc-window",
        )
        registry.build_status = "cancelled"

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            lock_manager=locks,
            task_store=store,
            config=FAST_TICK,
        )

        assert summary.terminal_status == "cancelled"
        assert executor.cancelled_refs == ["fc-window"]


class TestConcurrencyLimits:
    async def test_denied_task_stays_in_frontier_no_false_deadlock(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """Two tasks under a 1-slot key: only one spawns per round; a denied
        task never triggers the stuck-build failure (the slot holder may
        even be in another build)."""
        a = SyncOnlyTask(name="lim-a")
        b = SyncOnlyTask(name="lim-b")
        root = SyncOnlyTask(name="lim-root", deps=(a, b))
        registry, locks, executor, store = _setup([a, b, root], auto_complete=False)
        registry.limits["one-slot"] = 1

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            lock_manager=locks,
            task_store=store,
            config=TickConfig(
                linger_seconds=0.2,
                poll_interval_seconds=0.01,
                limit_key_selector=lambda t: ["one-slot"]
                if t.id in (a.id, b.id)
                else [],
            ),
        )

        # One acquired + spawned, one denied; build keeps waiting (no
        # terminal failure) and the tick lingers out.
        assert summary.spawned == 1
        assert summary.limit_denied >= 1
        assert summary.outcome == "lingered_out"
        assert registry.build_status == "running"

    async def test_slot_release_lets_denied_task_proceed(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """With instant workers, the whole chain completes within one tick:
        each completion frees the slot and wakes the scheduler, which then
        acquires it for the next task."""
        a = SyncOnlyTask(name="rel-a")
        b = SyncOnlyTask(name="rel-b")
        root = SyncOnlyTask(name="rel-root", deps=(a, b))
        registry, locks, executor, store = _setup([a, b, root])
        registry.limits["one-slot"] = 1

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            lock_manager=locks,
            task_store=store,
            config=TickConfig(
                linger_seconds=0.5,
                poll_interval_seconds=0.01,
                limit_key_selector=lambda t: ["one-slot"],
            ),
        )

        assert summary.outcome == "terminal"
        assert summary.terminal_status == "completed"
        assert summary.spawned == 3
        assert registry.build_status == "completed"

    async def test_no_selector_no_limit_calls(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        (root,) = _chain("nolim-root")
        registry, locks, executor, store = _setup([root])

        await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            lock_manager=locks,
            task_store=store,
            config=FAST_TICK,
        )

        assert not any(m == "start_with_limits" for (m, _) in registry.calls)


class TestStaleRunningNoRef:
    async def test_stale_running_without_ref_is_failed(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The slot-leak escape hatch: a task RUNNING without an executor
        ref past the staleness bound (scheduler crash between limit-slot
        acquisition and spawn) is failed — it could never resolve on its
        own, and while RUNNING it holds its concurrency-limit slots."""
        (root,) = _chain("stale-root")
        registry, locks, executor, store = _setup([root], auto_complete=False)
        registry.add_task(
            str(root.id),
            status="running",
            status_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            lock_manager=locks,
            task_store=store,
            config=FAST_TICK,  # default stale bound (1800s) < 1h age
        )

        assert summary.failed_recorded == 1
        assert summary.terminal_status == "failed"

    async def test_fresh_running_without_ref_is_left(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        (root,) = _chain("fresh-noref-root")
        registry, locks, executor, store = _setup([root], auto_complete=False)
        registry.add_task(
            str(root.id),
            status="running",
            status_at=datetime.now(timezone.utc),
        )

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            lock_manager=locks,
            task_store=store,
            config=FAST_TICK,
        )

        assert summary.failed_recorded == 0
        assert summary.outcome == "lingered_out"
        assert executor.spawned == []


class TestSkipBlockedOnFailure:
    async def test_fail_fast_skips_blocked_descendants(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """On a failure terminal the tick marks transitively blocked tasks
        skipped — they no longer dangle pending while the build is failed."""
        bad = SyncOnlyTask(name="skip-bad")
        mid = SyncOnlyTask(name="skip-mid", deps=(bad,))
        root = SyncOnlyTask(name="skip-root", deps=(mid,))
        registry, locks, executor, store = _setup([bad, mid, root], auto_complete=False)
        registry.add_task(str(bad.id), status="failed")

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            lock_manager=locks,
            task_store=store,
            config=FAST_TICK,  # FAIL_FAST default
        )

        assert summary.terminal_status == "failed"
        assert summary.skipped == 2
        assert registry.statuses[str(mid.id)] == "skipped"
        assert registry.statuses[str(root.id)] == "skipped"

    async def test_blocked_terminal_in_continue_mode_also_skips(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        dep, root = _chain("skip-cont-dep", "skip-cont-root")
        registry, locks, executor, store = _setup([dep, root], auto_complete=False)
        registry.add_task(str(dep.id), status="failed")

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            lock_manager=locks,
            task_store=store,
            config=TickConfig(
                linger_seconds=0.2,
                poll_interval_seconds=0.01,
                fail_mode=FailMode.CONTINUE,
            ),
        )

        assert summary.terminal_status == "failed"
        assert registry.statuses[str(root.id)] == "skipped"


class _SkipBlocked404Registry(NoOpRegistry):
    """Registry whose skip-blocked endpoint 404s with a given detail."""

    def __init__(self, detail: str):
        super().__init__()
        self.detail = detail

    async def build_skip_blocked_aio(self, build_id) -> list[str]:
        raise NotFoundError(
            "Skip blocked tasks: resource not found", detail=self.detail
        )


class TestSkipBlockedErrorHandling:
    async def test_missing_route_tolerated(self):
        """Old server without the endpoint (FastAPI default 404) → skip
        silently omitted, no raise."""
        summary = TickSummary(outcome="noop")
        await _skip_blocked(_SkipBlocked404Registry("Not Found"), uuid4(), summary)
        assert summary.skipped == 0

    async def test_app_level_404_reraised(self):
        """A 404 raised inside the endpoint (e.g. build no longer exists)
        signals a registry inconsistency and must propagate."""
        with pytest.raises(NotFoundError):
            await _skip_blocked(
                _SkipBlocked404Registry("Build not found"),
                uuid4(),
                TickSummary(outcome="noop"),
            )
