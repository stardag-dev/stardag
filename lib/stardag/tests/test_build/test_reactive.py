"""Unit tests for reactive tick scheduling (stardag.build._reactive).

Uses an in-memory fake registry that mirrors the API's frontier semantics
(dependency gating on task statuses), driven entirely by the tick's own
event calls — plus a fake detached executor whose "workers" complete
instantly (simulating worker-side lifecycle reporting + wake-up).
"""

from __future__ import annotations

import asyncio
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
from stardag.build._reactive import _RETRYABLE_STATUSES, TickSummary, _skip_blocked
from stardag.exceptions import NotFoundError
from stardag.registry import (
    BuildFrontier,
    FrontierExternalBlocker,
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
        # The reactive marker/owner, surfaced on the frontier. Presence is
        # the marker (defaults to a reactive build); tests set it to None to
        # simulate a non-reactive build.
        self.reactive_app_name: str | None = "test-app"
        self.reactive_tick_kwargs: dict | None = {}
        self.statuses: dict[str, str] = {}
        self.upstreams: dict[str, set[str]] = {}
        self.refs: dict[str, tuple[str | None, str | None]] = {}
        self.start_metadata: dict[str, dict | None] = {}
        self.needs_tick = False
        self.build_status = "running"
        self.build_error_message: str | None = None
        self.calls: list[tuple[str, str | None]] = []
        # Named concurrency limits: key -> cap; holders tracked per task.
        self.limits: dict[str, int] = {}
        self.task_limit_keys: dict[str, set[str]] = {}
        self.status_at: dict[str, datetime] = {}
        # task_id -> task_data body, served by task_get_metadata_aio
        # (rehydration fallback); missing key -> KeyError, like a 404.
        self.metadata_bodies: dict[str, dict] = {}
        # --- cross-build scope (mirrors the API's two scopes) ---
        # ``statuses`` is environment-global; these task ids exist in the
        # environment but are NOT in this build's task set, so they gate
        # this build's tasks (dependency edges are global too) while
        # contributing to neither ``actionable`` nor ``status_counts``.
        self.not_in_build: set[str] = set()
        # task_id -> the build whose event produced the current status. An
        # ABSENT key means "this build's own doing" (the common case), which
        # is what makes a task NOT an external blocker; an explicit None is a
        # row predating status denormalisation — not this build's doing
        # either, and with no build to ask about it.
        self.status_build_id: dict[str, UUID | None] = {}
        # task_id -> (namespace, name), echoed on blocker entries.
        self.task_names: dict[str, tuple[str, str]] = {}
        self.blocked_by_external_truncated = False
        # Derived status of OTHER builds in the environment, served by
        # build_get_aio — how the tick decides whether the build owning a
        # blocker is still going to schedule it. Absent ids 404, and
        # ``build_get_status`` of None emulates a server that doesn't report
        # the field.
        self.other_build_statuses: dict[UUID, str | None] = {}
        self.build_get_calls: list[UUID] = []
        # Set False to emulate a server predating blocked_by_external: the
        # fields stay at their model defaults, as they would deserialising
        # a response that never carried them.
        self.serves_blocked_by_external = True

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

    def add_blocking_task(
        self,
        task_id: str,
        *,
        blocks: "set[str]",
        status: str = "running",
        owner_build_id: "UUID | None" = None,
        owner_build_status: "str | None" = "running",
        owner_build_known: bool = True,
        name: str = "BlockingTask",
        namespace: str = "",
        status_at: "datetime | None" = None,
        in_build: bool = False,
    ) -> None:
        """Register an upstream whose current status this build did not set.

        Defaults to the #208 A1 shape: RUNNING under some *other* build and
        absent from this build's task set, so it gates ``blocks`` while
        appearing in neither this build's ``running`` nor its
        ``status_counts``. ``owner_build_status`` is what ``build_get`` will
        report for that other build (None = a server that doesn't report it);
        ``owner_build_known=False`` makes the lookup 404 instead.
        """
        self.statuses[task_id] = status
        owner_build_id = owner_build_id or uuid4()
        self.status_build_id[task_id] = owner_build_id
        if owner_build_known:
            self.other_build_statuses[owner_build_id] = owner_build_status
        self.task_names[task_id] = (namespace, name)
        if status_at is not None:
            self.status_at[task_id] = status_at
        if not in_build:
            self.not_in_build.add(task_id)
        for downstream in blocks:
            self.upstreams.setdefault(downstream, set()).add(task_id)

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

    async def task_start_aio(
        self, build_id, task, executor=None, executor_ref=None, executor_metadata=None
    ):
        tid = str(task.id)
        self.calls.append(("start", tid))
        self.statuses[tid] = "running"
        self.refs[tid] = (executor, executor_ref)
        self.start_metadata[tid] = executor_metadata
        if self.auto_complete:
            # Instant worker: completes and wakes the scheduler.
            self.statuses[tid] = "completed"
            self.needs_tick = True

    async def task_start_with_limits_aio(
        self,
        build_id,
        task,
        executor=None,
        executor_ref=None,
        executor_metadata=None,
        limit_keys=None,
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
            build_id,
            task,
            executor=executor,
            executor_ref=executor_ref,
            executor_metadata=executor_metadata,
        )
        return True

    async def task_complete_aio(self, build_id, task):
        tid = str(task.id)
        self.calls.append(("complete", tid))
        self.statuses[tid] = "completed"

    async def task_retry_aio(self, build_id, task):
        tid = str(task.id)
        self.calls.append(("retry", tid))
        # Same retryable set the server applies (suspended included: a
        # suspended task has no live execution to orphan).
        if self.statuses.get(tid) in _RETRYABLE_STATUSES:
            self.statuses[tid] = "pending"
            self.refs.pop(tid, None)

    async def build_add_roots_aio(self, build_id, root_task_ids):
        self.calls.append(("add_roots", ",".join(root_task_ids)))
        self.root_task_ids += [t for t in root_task_ids if t not in self.root_task_ids]

    async def task_cancel_aio(self, build_id, task):
        tid = str(task.id)
        self.calls.append(("cancel", tid))
        self.statuses[tid] = "cancelled"

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
        self.build_error_message = error_message

    async def task_get_metadata_aio(self, task_id):
        from stardag.registry._base import TaskMetadata

        body = self.metadata_bodies[str(task_id)]
        return TaskMetadata(
            id=task_id,
            body=body,
            name=body.get("__name", ""),
            namespace=body.get("__namespace", ""),
            version=body.get("version", ""),
            output_uri=None,
            status=self.statuses.get(str(task_id), "pending"),
            registered_at=None,
            started_at=None,
            completed_at=None,
            error_message=None,
        )

    async def build_skip_blocked_aio(self, build_id):
        # Mirrors the API: pending/suspended tasks transitively downstream
        # of a failed/cancelled/skipped task become skipped.
        self.calls.append(("skip_blocked", None))
        blocked = {
            tid
            for tid, status in self.statuses.items()
            if status in ("failed", "cancelled", "skipped")
        }
        # Blockage only propagates through nodes that will themselves never
        # complete (mirrors the API's CTE gate): a completed intermediate
        # satisfies its downstream; a running one may still complete.
        propagating = ("failed", "cancelled", "skipped", "pending", "suspended")
        changed = True
        while changed:
            changed = False
            for tid, ups in self.upstreams.items():
                if tid not in blocked and any(
                    up in blocked and self.statuses.get(up) in propagating for up in ups
                ):
                    blocked.add(tid)
                    changed = True
        skipped = []
        for tid in blocked:
            if self.statuses.get(tid) in ("pending", "suspended"):
                self.statuses[tid] = "skipped"
                skipped.append(tid)
        return skipped

    async def build_set_reactive_meta_aio(
        self, build_id, *, app_name, tick_kwargs=None
    ):
        self.calls.append(("set_reactive_meta", app_name))
        self.reactive_app_name = app_name
        if tick_kwargs is not None:
            self.reactive_tick_kwargs = tick_kwargs

    async def build_get_aio(self, build_id):
        from stardag.registry import BuildInfo

        self.build_get_calls.append(build_id)
        if build_id not in self.other_build_statuses:
            raise NotFoundError(f"Build {build_id} not found", detail="Build not found")
        return BuildInfo(id=build_id, status=self.other_build_statuses[build_id])

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

        # Build-scoped, like the API: actionable/running/status_counts see
        # only this build's task set. Dependency gating below stays global.
        in_build = {
            tid: status
            for tid, status in self.statuses.items()
            if tid not in self.not_in_build
        }
        actionable = [
            ref(tid)
            for tid, status in in_build.items()
            if status in ("pending", "suspended", "running")
            and all(
                self.statuses.get(up) == "completed"
                for up in self.upstreams.get(tid, set())
            )
        ]
        counts: dict[str, int] = {}
        for status in in_build.values():
            counts[status] = counts.get(status, 0) + 1
        running = [ref(tid) for tid, status in in_build.items() if status == "running"]
        # Mirrors the API: computed ONLY when the build has nothing
        # actionable and nothing running, so an empty list means "not
        # blocked externally, OR not stalled".
        blocked_by_external: list[FrontierExternalBlocker] = []
        if self.serves_blocked_by_external and not actionable and not running:
            for tid, status in in_build.items():
                if status not in ("pending", "suspended", "running"):
                    continue
                for up in sorted(self.upstreams.get(tid, set())):
                    if up not in self.statuses:
                        continue
                    if self.statuses[up] == "completed":
                        continue
                    if up not in self.status_build_id:
                        continue  # this build's own doing — not external
                    owner_id = self.status_build_id[up]
                    if owner_id == build_id:
                        continue
                    namespace, name = self.task_names.get(up, ("", up))
                    blocked_by_external.append(
                        FrontierExternalBlocker(
                            task_id=tid,
                            blocking_task_id=up,
                            blocking_task_namespace=namespace,
                            blocking_task_name=name,
                            blocking_status=self.statuses[up],
                            blocking_status_at=self.status_at.get(up),
                            blocking_status_build_id=owner_id,
                            blocking_in_build=up not in self.not_in_build,
                        )
                    )
        return BuildFrontier(
            build_id=build_id,
            build_status=self.build_status,
            needs_tick=self.needs_tick,
            root_task_ids=self.root_task_ids,
            roots=[ref(t) for t in self.root_task_ids if t in self.statuses],
            status_counts=counts,
            actionable=actionable,
            running=running,
            blocked_by_external=blocked_by_external,
            blocked_by_external_truncated=(
                self.blocked_by_external_truncated and bool(blocked_by_external)
            ),
            reactive_app_name=self.reactive_app_name,
            reactive_tick_kwargs=self.reactive_tick_kwargs,
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
    """BuildTaskStore on a dict — no target roots needed in engine tests.

    Pickle-only now: the reactive marker/owner/config live in the registry
    (see ``FakeReactiveRegistry.reactive_meta``), not the store.
    """

    def __init__(self, build_id: UUID):
        super().__init__(build_id)
        self._tasks: dict[str, BaseTask] = {}

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
        registry, locks, executor, store = _setup([root])
        # No reactive_app_name on the frontier → not a reactively-scheduled
        # build; the tick must not act on it.
        registry.reactive_app_name = None

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
        # No scheduling happened — the tick observed reactive_meta is None on
        # its first frontier fetch and bailed before acting.
        assert registry.build_status == "running"


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
    def test_pickle_round_trip(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        # The store is pickle-only; the reactive marker/config live in the
        # registry, not here.
        build_id = uuid4()
        store = BuildTaskStore(build_id)

        task = SyncOnlyTask(name="store-roundtrip")
        store.save_tasks([task])

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

    async def test_retry_failed_resets_an_abandoned_suspended_task(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """#208 A2: a task left SUSPENDED — its execution yielded dynamic
        dependencies and returned, then the build was abandoned — used to be
        permanently unschedulable, since the re-trigger's retry skipped it.
        It is now reset like any other non-completed status."""
        (root,) = _chain("suspended-retry-root")
        registry, locks, executor, store = _setup([root])
        registry.add_task(str(root.id), status="suspended")

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

    async def test_worker_dynamic_dep_registration_never_resets_suspended(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The other caller of discover_and_register_aio is a worker
        registering its dynamically yielded deps — it takes the default
        retry_failed=False, so widening the retryable set cannot make a
        suspending worker reset its own task."""
        (root,) = _chain("suspended-worker-root")
        registry, _, _, _ = _setup([root], auto_complete=False)
        registry.add_task(str(root.id), status="suspended")

        result = await discover_and_register_aio(registry, uuid4(), root)

        assert result.retried == []
        assert registry.statuses[str(root.id)] == "suspended"
        assert not any(method == "retry" for (method, _) in registry.calls)

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

    async def test_cancelled_branch_descendants_also_skipped(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """FAIL_FAST with a second, still-running branch: the cancel pass
        records TASK_CANCELLED (a cancelled task must not dangle RUNNING —
        workers killed by the executor's cancel can't self-report), so the
        cancelled branch's descendants land in the skip closure too."""
        bad = SyncOnlyTask(name="cb-bad")
        long_running = SyncOnlyTask(name="cb-running")
        downstream = SyncOnlyTask(name="cb-downstream", deps=(long_running,))
        root = SyncOnlyTask(name="cb-root", deps=(bad, downstream))
        registry, locks, executor, store = _setup(
            [bad, long_running, downstream, root], auto_complete=False
        )
        registry.add_task(str(bad.id), status="failed")
        registry.add_task(
            str(long_running.id),
            status="running",
            executor="fake",
            executor_ref="ref-live",
        )
        store.save_task(long_running)

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            lock_manager=locks,
            task_store=store,
            config=FAST_TICK,  # FAIL_FAST default
        )

        assert summary.terminal_status == "failed"
        assert executor.cancelled_refs == ["ref-live"]
        assert registry.statuses[str(long_running.id)] == "cancelled"
        assert registry.statuses[str(downstream.id)] == "skipped"
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


class TestRehydrationFallback:
    async def test_store_miss_rehydrates_from_registry(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """A task missing from the pickle store is reconstructed from the
        registry's stored task_data and scheduled — instead of being failed."""
        import stardag as sd

        @sd.task(name="RehydrateFallbackTask")
        def fallback_task(limit: int) -> list[int]:
            return list(range(limit))

        root = fallback_task(limit=3)
        registry = FakeReactiveRegistry(
            root_task_ids=[str(root.id)], auto_complete=True
        )
        registry.add_task(str(root.id))
        registry.metadata_bodies[str(root.id)] = root.model_dump(mode="json")
        store = InMemoryTaskStore(uuid4())  # empty: no pickle for the task

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=(executor := FakeTickExecutor()),
            lock_manager=_lock_manager(),
            task_store=store,
            config=FAST_TICK,
        )

        assert executor.spawned == [root.id]
        assert summary.terminal_status == "completed"
        assert summary.failed_recorded == 0
        # Healed back into the store for subsequent ticks.
        assert store.load_task(root.id) is not None

    async def test_store_miss_and_no_metadata_still_fails_task(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """Without rehydratable data either, the stall-prevention failure
        path is preserved."""
        (root,) = _chain("no-rehydrate-root")
        registry, locks, executor, store = _setup([root], auto_complete=False)
        store._tasks.clear()
        # no metadata_bodies entry -> fallback raises -> task failed

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            lock_manager=locks,
            task_store=store,
            config=FAST_TICK,
        )

        assert summary.failed_recorded == 1
        assert summary.terminal_status == "failed"


class TestRehydrationDiagnostics:
    """A rehydration failure names the declared task modules that failed to
    import — "class X unresolved" and "the module defining X blew up on
    import" are the same incident seen from two ends, and only the
    annotation connects them."""

    @pytest.fixture
    def failed_task_module_import(self):
        from stardag.build._task_modules import (
            _reset_import_state_for_tests,
            import_task_modules,
        )

        _reset_import_state_for_tests()
        import_task_modules(["stardag_no_such_declared_task_module"])
        yield
        _reset_import_state_for_tests()

    async def test_failure_note_is_appended_to_the_rehydration_error(
        self,
        caplog,
        failed_task_module_import,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
    ):
        (root,) = _chain("diagnostic-root")
        registry, locks, executor, store = _setup([root], auto_complete=False)
        store._tasks.clear()  # neither a pickle nor rehydratable metadata

        with caplog.at_level("WARNING"):
            await run_tick_aio(
                uuid4(),
                registry=registry,
                task_executor=executor,
                lock_manager=locks,
                task_store=store,
                config=FAST_TICK,
            )

        messages = "\n".join(record.getMessage() for record in caplog.records)
        assert "could not be rehydrated from registry data" in messages
        assert "stardag_no_such_declared_task_module" in messages
        assert "likely cause" in messages

    async def test_no_note_when_every_task_module_imported(
        self, caplog, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        from stardag.build._task_modules import _reset_import_state_for_tests

        _reset_import_state_for_tests()
        (root,) = _chain("diagnostic-clean-root")
        registry, locks, executor, store = _setup([root], auto_complete=False)
        store._tasks.clear()

        with caplog.at_level("WARNING"):
            await run_tick_aio(
                uuid4(),
                registry=registry,
                task_executor=executor,
                lock_manager=locks,
                task_store=store,
                config=FAST_TICK,
            )

        messages = "\n".join(record.getMessage() for record in caplog.records)
        assert "could not be rehydrated from registry data" in messages
        assert "failed to import" not in messages


class TestExecutorMetadataRecording:
    """The post-spawn ref-recording start carries the handle's
    executor_metadata (and drops it for pre-metadata registries)."""

    class MetadataTickExecutor(FakeTickExecutor):
        METADATA = {"kind": "modal", "app_name": "tick-app"}

        async def submit_detached(self, task: BaseTask) -> DetachedHandle:
            handle = await super().submit_detached(task)
            return DetachedHandle(
                executor=handle.executor,
                ref=handle.ref,
                wait=handle.wait,
                executor_metadata=self.METADATA,
            )

    async def test_post_spawn_start_carries_handle_metadata(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        (root,) = _chain("meta-root")
        registry, locks, _, store = _setup([root])

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=self.MetadataTickExecutor(),
            lock_manager=locks,
            task_store=store,
            config=FAST_TICK,
        )

        assert summary.spawned == 1
        assert registry.start_metadata[str(root.id)] == (
            self.MetadataTickExecutor.METADATA
        )

    async def test_metadata_dropped_for_pre_metadata_registry(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """A registry whose task_start_aio predates the executor_metadata
        kwarg still gets the ref-recording start — no TypeError."""

        class PreMetadataRegistry(FakeReactiveRegistry):
            async def task_start_aio(  # pyright: ignore[reportIncompatibleMethodOverride]  # pre-metadata signature (deliberate)
                self, build_id, task, executor=None, executor_ref=None
            ):
                await super().task_start_aio(
                    build_id, task, executor=executor, executor_ref=executor_ref
                )

        (root,) = _chain("meta-legacy-root")
        registry = PreMetadataRegistry(root_task_ids=[str(root.id)], auto_complete=True)
        registry.add_task(str(root.id))
        store = InMemoryTaskStore(uuid4())
        store.save_tasks([root])

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=self.MetadataTickExecutor(),
            lock_manager=_lock_manager(),
            task_store=store,
            config=FAST_TICK,
        )

        assert summary.outcome == "terminal"
        assert registry.refs[str(root.id)] == ("fake", "ref-1")


class TestAcquiringStartExecutorMetadata:
    """The limits-acquiring TASK_STARTED (recorded BEFORE the spawn) carries
    the executor metadata resolvable pre-spawn, closing the acquire→spawn
    window where a RUNNING task would otherwise show blank executor info."""

    PRE_SPAWN_METADATA = {"kind": "modal", "app_name": "tick-app"}

    class PreSpawnMetadataExecutor(FakeTickExecutor):
        async def get_executor_metadata(self, task: BaseTask):
            return TestAcquiringStartExecutorMetadata.PRE_SPAWN_METADATA

    class AcquireRecordingRegistry(FakeReactiveRegistry):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.acquire_metadata: dict[str, dict | None] = {}

        async def task_start_with_limits_aio(
            self,
            build_id,
            task,
            executor=None,
            executor_ref=None,
            executor_metadata=None,
            limit_keys=None,
        ):
            self.acquire_metadata[str(task.id)] = executor_metadata
            return await super().task_start_with_limits_aio(
                build_id,
                task,
                executor=executor,
                executor_ref=executor_ref,
                executor_metadata=executor_metadata,
                limit_keys=limit_keys,
            )

    async def test_acquiring_start_carries_metadata(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        (root,) = _chain("acquire-meta-root")
        registry = self.AcquireRecordingRegistry(
            root_task_ids=[str(root.id)], auto_complete=True
        )
        registry.add_task(str(root.id))
        registry.limits["gpu"] = 1
        store = InMemoryTaskStore(uuid4())
        store.save_tasks([root])
        config = TickConfig(
            linger_seconds=0.3,
            poll_interval_seconds=0.01,
            limit_key_selector=lambda t: ["gpu"],
        )

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=self.PreSpawnMetadataExecutor(),
            lock_manager=_lock_manager(),
            task_store=store,
            config=config,
        )

        assert summary.spawned == 1
        assert registry.acquire_metadata[str(root.id)] == self.PRE_SPAWN_METADATA

    async def test_acquiring_start_metadata_dropped_for_legacy_registry(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """A registry whose task_start_with_limits_aio predates the kwarg
        gets the plain acquiring start — no TypeError."""

        class PreMetadataLimitsRegistry(FakeReactiveRegistry):
            async def task_start_with_limits_aio(  # pyright: ignore[reportIncompatibleMethodOverride]  # pre-metadata signature (deliberate)
                self, build_id, task, executor=None, executor_ref=None, limit_keys=None
            ):
                return await super().task_start_with_limits_aio(
                    build_id,
                    task,
                    executor=executor,
                    executor_ref=executor_ref,
                    limit_keys=limit_keys,
                )

        (root,) = _chain("acquire-meta-legacy")
        registry = PreMetadataLimitsRegistry(
            root_task_ids=[str(root.id)], auto_complete=True
        )
        registry.add_task(str(root.id))
        registry.limits["gpu"] = 1
        store = InMemoryTaskStore(uuid4())
        store.save_tasks([root])
        config = TickConfig(
            linger_seconds=0.3,
            poll_interval_seconds=0.01,
            limit_key_selector=lambda t: ["gpu"],
        )

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=self.PreSpawnMetadataExecutor(),
            lock_manager=_lock_manager(),
            task_store=store,
            config=config,
        )

        assert summary.outcome == "terminal"
        assert summary.spawned == 1


class ClaimingReactiveRegistry(FakeReactiveRegistry):
    """FakeReactiveRegistry with real claim arbitration (API semantics).

    ``claim_race_once`` simulates the cross-build race the claim closes:
    the frontier snapshot says PENDING, but by claim time another build's
    scheduler has already started (and instantly completed) the task.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.claim_race_once: set[str] = set()

    async def task_start_claim_aio(
        self,
        build_id,
        task,
        executor=None,
        executor_ref=None,
        executor_metadata=None,
        limit_keys=None,
    ):
        from stardag.registry import StartClaimResult

        tid = str(task.id)
        self.calls.append(("start_claim", tid))
        if tid in self.claim_race_once:
            # "Another build" won this task just before us and its instant
            # worker completed it (completion wakes our scheduler).
            self.claim_race_once.discard(tid)
            self.statuses[tid] = "completed"
            self.needs_tick = True
            return StartClaimResult(
                started=False,
                denied_reason="already_running",
                executor="fake",
                executor_ref="fc-other-build",
            )
        if self.statuses.get(tid) == "running":
            executor_name, ref = self.refs.get(tid, (None, None))
            return StartClaimResult(
                started=False,
                denied_reason="already_running",
                executor=executor_name,
                executor_ref=ref,
            )
        if self.statuses.get(tid) == "completed":
            return StartClaimResult(started=False, denied_reason="already_completed")
        started = await self.task_start_with_limits_aio(
            build_id,
            task,
            executor=executor,
            executor_ref=executor_ref,
            executor_metadata=executor_metadata,
            limit_keys=limit_keys,
        )
        if not started:
            return StartClaimResult(started=False, denied_reason="limit")
        return StartClaimResult(started=True)


class TestTickClaims:
    async def test_claim_race_lost_then_build_completes(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The tick loses the claim to 'another build' — the task stays in
        the frontier, no duplicate spawn happens, no false stuck-failure,
        and the build completes once the winner's completion is observed."""
        (root,) = _chain("tick-claim-race")
        registry = ClaimingReactiveRegistry(
            root_task_ids=[str(root.id)], auto_complete=True
        )
        registry.add_task(str(root.id))
        registry.claim_race_once.add(str(root.id))
        store = InMemoryTaskStore(uuid4())
        store.save_tasks([root])
        executor = FakeTickExecutor()

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            lock_manager=_lock_manager(),
            task_store=store,
            config=FAST_TICK,
        )

        assert summary.outcome == "terminal"
        assert summary.terminal_status == "completed"
        assert summary.claim_denied == 1
        assert executor.spawned == []  # the duplicate spawn never happened
        assert registry.build_status == "completed"

    async def test_claims_and_limits_compose(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """With claims on, limit denials still resolve via the claim start
        (single acquiring call) and the chain completes under a 1-slot key."""
        a = SyncOnlyTask(name="tick-claim-a")
        b = SyncOnlyTask(name="tick-claim-b")
        root = SyncOnlyTask(name="tick-claim-root", deps=(a, b))
        registry = ClaimingReactiveRegistry(
            root_task_ids=[str(root.id)], auto_complete=True
        )
        for task in (a, b, root):
            registry.add_task(
                str(task.id),
                upstreams={str(d.id) for d in flatten_task_struct(task.requires())},
            )
        registry.limits["one-slot"] = 1
        store = InMemoryTaskStore(uuid4())
        store.save_tasks([a, b, root])
        executor = FakeTickExecutor()

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            lock_manager=_lock_manager(),
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
        # all acquisitions went through the claiming start
        assert any(m == "start_claim" for (m, _) in registry.calls)

    async def test_claim_off_uses_legacy_limits_path(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        (root,) = _chain("tick-claim-off")
        registry = ClaimingReactiveRegistry(
            root_task_ids=[str(root.id)], auto_complete=True
        )
        registry.add_task(str(root.id))
        store = InMemoryTaskStore(uuid4())
        store.save_tasks([root])

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=FakeTickExecutor(),
            lock_manager=_lock_manager(),
            task_store=store,
            config=TickConfig(
                linger_seconds=0.3, poll_interval_seconds=0.01, claim=False
            ),
        )

        assert summary.terminal_status == "completed"
        assert not any(m == "start_claim" for (m, _) in registry.calls)


class TestExternalBlockers:
    """Terminal detection when the blocker lives outside this build (#208 A1).

    Dependency gating is environment-global while ``running`` and
    ``status_counts`` are build-scoped, so a task another build is executing
    gates this build's tasks while appearing in neither count. Read as
    "nothing runnable, nothing running", that shape used to fail the build
    outright.
    """

    BLOCKER_ID = "blocking-task-id"

    def _blocked_build(
        self,
        *,
        blocker_status: str = "running",
        blocker_age_seconds: float | None = 60.0,
        in_build: bool = False,
        owner_build_id: "UUID | None" = None,
        owner_build_status: "str | None" = "running",
        owner_build_known: bool = True,
    ):
        """A build whose only task is gated by an upstream it does not own."""
        (root,) = _chain("ext-blocked-root")
        registry, locks, executor, store = _setup([root], auto_complete=False)
        registry.add_blocking_task(
            self.BLOCKER_ID,
            blocks={str(root.id)},
            status=blocker_status,
            status_at=(
                None
                if blocker_age_seconds is None
                else datetime.now(timezone.utc) - timedelta(seconds=blocker_age_seconds)
            ),
            in_build=in_build,
            owner_build_id=owner_build_id,
            owner_build_status=owner_build_status,
            owner_build_known=owner_build_known,
            namespace="pipelines",
            name="Ingest",
        )
        return root, registry, locks, executor, store

    async def test_running_blocker_in_another_build_waits_instead_of_failing(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The exact A1 repro: this build has nothing actionable and nothing
        running because its task waits on an upstream another build is
        executing. That upstream's completion will unblock it, so the tick
        must wait (as it does for a concurrency-limit denial) rather than
        fail the build."""
        _, registry, locks, executor, store = self._blocked_build()

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            lock_manager=locks,
            task_store=store,
            config=FAST_TICK,
        )

        assert summary.outcome == "lingered_out"
        assert registry.build_status == "running"
        assert not any(method == "build_fail" for (method, _) in registry.calls)
        assert executor.spawned == []
        assert (summary.external_blockers, summary.external_blockers_waited) == (1, 1)
        assert summary.external_blockers_fatal == 0

    async def test_wait_is_bounded_by_the_blockers_status_age(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """A blocker RUNNING far longer than the bound is a claim nobody is
        going to release — waiting again would hang the build silently, so
        the tick fails it with the full explanation."""
        _, registry, locks, executor, store = self._blocked_build(
            blocker_age_seconds=60 * 60 * 24  # a day; default bound is 6h
        )

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            lock_manager=locks,
            task_store=store,
            config=FAST_TICK,
        )

        assert summary.terminal_status == "failed"
        assert summary.external_blockers_fatal == 1
        assert summary.external_blockers_waited == 0
        message = registry.build_error_message or ""
        assert "pipelines.Ingest" in message
        assert self.BLOCKER_ID in message
        assert "RUNNING for 86400s" in message
        assert str(registry.status_build_id[self.BLOCKER_ID]) in message
        # The stale-claim escape hatch, which #208 A2 notes was undocumented.
        assert "release the claim" in message

    async def test_a_generous_bound_keeps_waiting_on_a_long_running_blocker(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """Control for the bound: the same age with a larger bound waits."""
        _, registry, locks, executor, store = self._blocked_build(
            blocker_age_seconds=60 * 60 * 24
        )

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            lock_manager=locks,
            task_store=store,
            config=TickConfig(
                linger_seconds=0.2,
                poll_interval_seconds=0.01,
                stale_external_blocker_seconds=60 * 60 * 24 * 7,
            ),
        )

        assert summary.outcome == "lingered_out"
        assert summary.external_blockers_waited == 1
        assert registry.build_status == "running"

    @pytest.mark.parametrize(
        "blocker_status", ["pending", "suspended", "failed", "cancelled", "skipped"]
    )
    async def test_non_running_external_blocker_fails_immediately(
        self,
        blocker_status: str,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
    ):
        """Nobody is executing the blocker, the build that owns it has gone
        terminal, and this build will never schedule it (it is not in this
        build's task set) — so waiting would be waiting forever. Fail now,
        naming the task and the build that owns it."""
        _, registry, locks, executor, store = self._blocked_build(
            blocker_status=blocker_status, owner_build_status="completed"
        )

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            lock_manager=locks,
            task_store=store,
            config=FAST_TICK,
        )

        assert summary.terminal_status == "failed"
        assert summary.external_blockers_fatal == 1
        assert summary.external_blockers_waited == 0
        message = registry.build_error_message or ""
        assert "pipelines.Ingest" in message
        assert blocker_status.upper() in message
        assert f"under build {registry.status_build_id[self.BLOCKER_ID]}" in message
        # Actionable: retry now covers suspended too (#208 A2), and only a
        # RUNNING blocker needs the cancel-first hint.
        assert "/retry" in message
        assert "release the claim" not in message

    @pytest.mark.parametrize("owner_build_status", ["running", "pending"])
    async def test_non_running_blocker_of_a_live_build_waits(
        self,
        owner_build_status: str,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
    ):
        """A PENDING blocker belonging to a build that is still live *will* be
        scheduled by that build — failing here would be a brand-new spurious
        failure, the very class of bug being fixed. A build the server still
        reports as pending counts as live too: it may yet start, and the
        staleness bound is what keeps that from being an unbounded wait."""
        _, registry, locks, executor, store = self._blocked_build(
            blocker_status="pending", owner_build_status=owner_build_status
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
        assert registry.build_status == "running"
        assert summary.external_blockers_waited == 1
        assert summary.external_blockers_fatal == 0

    async def test_non_running_blocker_of_a_live_build_is_still_bounded(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The owner being live is not a licence to wait forever: a build that
        has left a task pending past the bound is not making progress on it."""
        _, registry, locks, executor, store = self._blocked_build(
            blocker_status="pending",
            owner_build_status="running",
            blocker_age_seconds=60 * 60 * 24,
        )

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            lock_manager=locks,
            task_store=store,
            config=FAST_TICK,
        )

        assert summary.terminal_status == "failed"
        assert summary.external_blockers_fatal == 1
        assert "staleness bound" in (registry.build_error_message or "")

    async def test_blocker_with_no_status_owning_build_fails(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """No build owns the blocker's status (a row predating status
        denormalisation), so no status-moving event was ever recorded against
        it — there is no evidence anyone intends to run it, and no build to
        ask. Fail, with the remediation intact."""
        _, registry, locks, executor, store = self._blocked_build(
            blocker_status="pending"
        )
        registry.status_build_id[self.BLOCKER_ID] = None

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            lock_manager=locks,
            task_store=store,
            config=FAST_TICK,
        )

        assert summary.terminal_status == "failed"
        assert summary.external_blockers_fatal == 1
        assert registry.build_get_calls == []  # nothing to look up
        message = registry.build_error_message or ""
        assert "no build owns its status" in message
        assert "/retry" in message

    async def test_failed_owner_lookup_fails_without_propagating(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """An unresolvable owner (deleted build, unreachable registry) is not
        evidence of life: the build fails with a precise message rather than
        the lookup error escaping the tick."""
        _, registry, locks, executor, store = self._blocked_build(
            blocker_status="pending", owner_build_known=False
        )

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            lock_manager=locks,
            task_store=store,
            config=FAST_TICK,
        )

        assert summary.terminal_status == "failed"
        assert summary.external_blockers_fatal == 1
        assert "status is unknown" in (registry.build_error_message or "")

    async def test_server_not_reporting_build_status_fails(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """Same treatment for a server (or custom registry) that doesn't
        report the derived build status: the field defaults to None, unknown
        is not evidence of life, and the message says so."""
        _, registry, locks, executor, store = self._blocked_build(
            blocker_status="pending", owner_build_status=None
        )

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            lock_manager=locks,
            task_store=store,
            config=FAST_TICK,
        )

        assert summary.terminal_status == "failed"
        assert "status is unknown" in (registry.build_error_message or "")

    async def test_owner_status_is_resolved_once_per_build(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """A wide DAG stalled behind one build yields one blocker entry per
        blocked edge, all naming the same owner — that must cost one request,
        not one per entry."""
        root, registry, locks, executor, store = self._blocked_build(
            blocker_status="pending", owner_build_id=(owner := uuid4())
        )
        sibling = SyncOnlyTask(name="ext-memo-sibling")
        store.save_task(sibling)
        registry.add_task(str(sibling.id), status="pending")
        registry.add_blocking_task(
            "second-blocker",
            blocks={str(sibling.id)},
            status="pending",
            owner_build_id=owner,  # same owner as the first blocker
            namespace="pipelines",
            name="Transform",
        )
        registry.build_get_calls.clear()

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            lock_manager=locks,
            task_store=store,
            config=TickConfig(linger_seconds=0.0, poll_interval_seconds=0.01),
        )

        assert summary.external_blockers_waited == 2
        assert registry.build_get_calls == [owner]

    async def test_in_build_blocker_does_not_cause_a_wait(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """A blocker inside this build's own task set is already modelled by
        actionable/running/status_counts, so it must not buy the build a
        wait — but it does get named, since the status counts alone never
        said which task was holding things up."""
        _, registry, locks, executor, store = self._blocked_build(
            blocker_status="cancelled", in_build=True
        )

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
        assert summary.external_blockers == 1
        assert summary.external_blockers_waited == 0
        assert summary.external_blockers_fatal == 0
        message = registry.build_error_message or ""
        assert "No runnable or running tasks left" in message
        assert "Blocked within this build by" in message
        assert "pipelines.Ingest" in message

    async def test_unknown_blocker_age_waits_unbounded(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """No ``blocking_status_at`` (a task row predating server-side status
        denormalisation) → the wait cannot be aged. Waiting is the safe
        direction: failing on missing information would reintroduce exactly
        the spurious failure this path removes."""
        _, registry, locks, executor, store = self._blocked_build(
            blocker_age_seconds=None
        )

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            lock_manager=locks,
            task_store=store,
            config=TickConfig(
                linger_seconds=0.2,
                poll_interval_seconds=0.01,
                stale_external_blocker_seconds=0.0,  # would expire anything
            ),
        )

        assert summary.outcome == "lingered_out"
        assert summary.external_blockers_waited == 1
        assert registry.build_status == "running"

    async def test_bound_disabled_waits_indefinitely(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        _, registry, locks, executor, store = self._blocked_build(
            blocker_age_seconds=60 * 60 * 24 * 365
        )

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            lock_manager=locks,
            task_store=store,
            config=TickConfig(
                linger_seconds=0.2,
                poll_interval_seconds=0.01,
                stale_external_blocker_seconds=None,
            ),
        )

        assert summary.outcome == "lingered_out"
        assert summary.external_blockers_waited == 1

    async def test_a_fatal_blocker_wins_over_a_waitable_one(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """One blocker nothing will ever run means the build cannot complete,
        whatever else it is also waiting on."""
        root, registry, locks, executor, store = self._blocked_build()
        registry.add_blocking_task(
            "dead-blocker",
            blocks={str(root.id)},
            status="suspended",
            owner_build_status="cancelled",
            namespace="pipelines",
            name="Abandoned",
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

        assert summary.terminal_status == "failed"
        assert summary.external_blockers == 2
        assert summary.external_blockers_waited == 1
        assert summary.external_blockers_fatal == 1
        # Only the fatal one is named as the reason to fail.
        message = registry.build_error_message or ""
        assert "pipelines.Abandoned" in message
        assert "pipelines.Ingest" not in message

    async def test_truncated_blocker_list_is_flagged_in_the_message(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The server caps its blocker list; the failure must not read as an
        exhaustive account of what is holding the build back."""
        _, registry, locks, executor, store = self._blocked_build(
            blocker_status="suspended", owner_build_status="failed"
        )
        registry.blocked_by_external_truncated = True

        await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            lock_manager=locks,
            task_store=store,
            config=FAST_TICK,
        )

        message = registry.build_error_message or ""
        assert "capped the blocker list" in message

    async def test_older_server_without_the_fields_behaves_exactly_as_before(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """A server predating blocked_by_external leaves the fields at their
        defaults, and terminal detection degrades to the pre-fix failure —
        the bug is unfixable client-side there, but nothing regresses."""
        _, registry, locks, executor, store = self._blocked_build()
        registry.serves_blocked_by_external = False

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            lock_manager=locks,
            task_store=store,
            config=FAST_TICK,
        )

        assert summary.terminal_status == "failed"
        assert summary.external_blockers == 0
        assert summary.external_blockers_waited == 0
        assert summary.external_blockers_fatal == 0
        assert "No runnable or running tasks left" in (
            registry.build_error_message or ""
        )

    async def test_healthy_build_never_evaluates_blockers(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The frontier reports blockers only while a build looks stalled, so
        a build that runs to completion records none."""
        dep, root = _chain("ext-healthy-dep", "ext-healthy-root")
        registry, locks, executor, store = _setup([dep, root])

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            lock_manager=locks,
            task_store=store,
            config=FAST_TICK,
        )

        assert summary.terminal_status == "completed"
        assert summary.external_blockers == 0

    async def test_blocker_completion_releases_the_waiting_build(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """End of the wait: once the owning build completes the blocker (and
        wakes this scheduler), the same lingering tick schedules the task it
        would previously have failed the build over."""
        root, registry, locks, executor, store = self._blocked_build()
        registry.auto_complete = True

        async def complete_blocker_soon() -> None:
            await asyncio.sleep(0.05)
            registry.statuses[self.BLOCKER_ID] = "completed"
            registry.needs_tick = True

        waiter = asyncio.create_task(complete_blocker_soon())
        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            lock_manager=locks,
            task_store=store,
            config=TickConfig(linger_seconds=1.0, poll_interval_seconds=0.01),
        )
        await waiter

        assert summary.terminal_status == "completed"
        assert executor.spawned == [root.id]
        assert summary.external_blockers_waited >= 1
