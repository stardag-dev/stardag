"""Unit tests for reactive tick scheduling (stardag.build._reactive).

Uses an in-memory fake registry that mirrors the API's frontier semantics
(dependency gating on task statuses), driven entirely by the tick's own
event calls — plus a fake detached executor whose "workers" complete
instantly (simulating worker-side lifecycle reporting + wake-up).
"""

from __future__ import annotations

import asyncio
import json
import typing
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4


import pytest

from stardag import (
    BaseTask,
    TaskStruct as TaskStructType,
    auto_namespace,
    flatten_task_struct,
)
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
from stardag.build import _reactive as reactive_module
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
        # Limit keys as sent on each claiming start: task_id -> keys.
        self.claim_limit_keys: dict[str, list[str]] = {}
        self.status_at: dict[str, datetime] = {}
        # task_id -> when its RUNNING execution claim lapses. Absent = the
        # server's "never lapses" (NULL), which is also what a server
        # predating claim expiry reports for everything.
        self.expires_at: dict[str, datetime] = {}
        # claim_ttl_seconds as sent on each start: task_id -> list of TTLs
        # (a spawn records two starts — the claim and the post-spawn ref).
        self.sent_claim_ttls: dict[str, list[int | None]] = {}
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
        # Tick summaries reported by run_tick_aio, and an optional error to
        # raise from the reporting endpoint.
        self.reported_tick_summaries: list[dict] = []
        self.tick_summary_error: Exception | None = None
        # Set to make the frontier fetch blow up, i.e. crash the tick itself.
        self.frontier_error: Exception | None = None

    # --- test setup helpers ---

    def add_task(
        self,
        task_id: str,
        status: str = "pending",
        upstreams: set[str] | None = None,
        executor: str | None = None,
        executor_ref: str | None = None,
        status_at: "datetime | None" = None,
        expires_at: "datetime | None" = None,
    ) -> None:
        self.statuses[task_id] = status
        self.upstreams.setdefault(task_id, set()).update(upstreams or set())
        if executor or executor_ref:
            self.refs[task_id] = (executor, executor_ref)
        if status_at is not None:
            self.status_at[task_id] = status_at
        if expires_at is not None:
            self.expires_at[task_id] = expires_at

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
        expires_at: "datetime | None" = None,
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
        if expires_at is not None:
            self.expires_at[task_id] = expires_at
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
        self,
        build_id,
        task,
        executor=None,
        executor_ref=None,
        executor_metadata=None,
        claim_ttl_seconds=None,
    ):
        tid = str(task.id)
        self.calls.append(("start", tid))
        self.sent_claim_ttls.setdefault(tid, []).append(claim_ttl_seconds)
        self.statuses[tid] = "running"
        self.refs[tid] = (executor, executor_ref)
        self.start_metadata[tid] = executor_metadata
        if self.auto_complete:
            # Instant worker: completes and wakes the scheduler.
            self.statuses[tid] = "completed"
            self.needs_tick = True

    async def task_start_claim_aio(
        self,
        build_id,
        task,
        executor=None,
        executor_ref=None,
        executor_metadata=None,
        limit_keys=None,
        claim_ttl_seconds=None,
    ):
        """Real claim arbitration, mirroring the API's claim-on-start."""
        from stardag.registry import StartClaimResult

        tid = str(task.id)
        self.calls.append(("start_claim", tid))
        self.claim_limit_keys[tid] = list(limit_keys or [])
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
            claim_ttl_seconds=claim_ttl_seconds,
        )
        if not started:
            return StartClaimResult(started=False, denied_reason="limit")
        return StartClaimResult(started=True)

    async def task_start_with_limits_aio(
        self,
        build_id,
        task,
        executor=None,
        executor_ref=None,
        executor_metadata=None,
        limit_keys=None,
        claim_ttl_seconds=None,
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
            claim_ttl_seconds=claim_ttl_seconds,
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

    async def build_report_tick_summary_aio(self, build_id, summary):
        # Observability sink. ``tick_summary_error`` lets a test make it
        # fail the way a real registry can (route missing, server down)
        # and assert the tick is unaffected.
        self.reported_tick_summaries.append(summary)
        if self.tick_summary_error is not None:
            raise self.tick_summary_error

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
        if self.frontier_error is not None:
            raise self.frontier_error

        def ref(tid: str) -> FrontierTaskRef:
            executor, executor_ref = self.refs.get(tid, (None, None))
            return FrontierTaskRef(
                task_id=tid,
                latest_status=self.statuses[tid],
                latest_executor=executor,
                latest_executor_ref=executor_ref,
                latest_status_at=self.status_at.get(tid),
                latest_status_expires_at=self.expires_at.get(tid),
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
                            # Mirrors the server: only a RUNNING task holds
                            # a claim, so only a RUNNING blocker can carry
                            # an expiry.
                            blocking_status_expires_at=(
                                self.expires_at.get(up)
                                if self.statuses[up] == "running"
                                else None
                            ),
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

    def __init__(
        self,
        statuses: dict[str, DetachedExecutionStatus] | None = None,
        timeout_seconds: float | None = None,
    ):
        # ref -> probe status
        self.probe_statuses = statuses or {}
        self.spawned: list[UUID] = []
        self.cancelled_refs: list[str] = []
        self._spawn_count = 0
        # The backend's own wall-clock limit, from which the tick derives
        # the claim TTL. None = a backend that enforces none.
        self.timeout_seconds = timeout_seconds

    async def submit(self, task):
        raise AssertionError("ticks must not use blocking submit")

    def execution_timeout_seconds(self, task: BaseTask) -> float | None:
        return self.timeout_seconds

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

    async def test_no_selector_claims_without_limit_keys(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """Without a selector the claiming start still happens (it is the
        exactly-once arbitration), but carries no limit keys — so nothing
        is enforced and no slot is held."""
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

        assert registry.claim_limit_keys[str(root.id)] == []


class TestRunningWithoutRef:
    """The claiming start is recorded BEFORE the spawn, so a tick that dies
    in between leaves a task RUNNING with no ref: nothing to probe, no
    worker to report it, and its concurrency-limit slots held indefinitely.
    Whether that shape is dead or merely mid-spawn is decided by the
    claim's own expiry, not by how long it has sat there."""

    async def _tick_on_running_root(self, expires_at: "datetime | None"):
        (root,) = _chain(f"noref-root-{expires_at}")
        registry, locks, executor, store = _setup([root], auto_complete=False)
        registry.add_task(
            str(root.id),
            status="running",
            status_at=datetime.now(timezone.utc) - timedelta(hours=1),
            expires_at=expires_at,
        )
        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            lock_manager=locks,
            task_store=store,
            config=FAST_TICK,
        )
        return summary, executor

    async def test_lapsed_claim_without_ref_is_failed(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """Lapsed claim: the server will hand the task to the next claimant
        anyway, so leaving it RUNNING only leaks the slots it holds."""
        summary, _ = await self._tick_on_running_root(
            datetime.now(timezone.utc) - timedelta(minutes=1)
        )

        assert summary.failed_recorded == 1
        assert summary.terminal_status == "failed"

    async def test_live_claim_without_ref_is_left(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """A live claim is left alone however old the status is — the age
        was only ever a proxy for the question the expiry answers."""
        summary, executor = await self._tick_on_running_root(
            datetime.now(timezone.utc) + timedelta(hours=1)
        )

        assert summary.failed_recorded == 0
        assert summary.outcome == "lingered_out"
        assert executor.spawned == []

    async def test_claim_without_an_expiry_is_left(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """No expiry (older server, or a start predating the column): the
        spawn-in-progress window of a healthy tick looks identical from
        here, so leave it rather than kill a task about to start."""
        summary, executor = await self._tick_on_running_root(None)

        assert summary.failed_recorded == 0
        assert summary.outcome == "lingered_out"
        assert executor.spawned == []


class TestDerivedClaimTtl:
    """Every start the tick records carries a TTL derived from the
    executor's own timeout, so the expiry other schedulers read is tied to
    when the execution is actually killed."""

    async def test_derived_ttl_is_sent_on_both_starts(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        from stardag.build._reactive import _CLAIM_TTL_GRACE_SECONDS

        (root,) = _chain("ttl-root")
        registry, locks, _, store = _setup([root])
        executor = FakeTickExecutor(timeout_seconds=3600.0)

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            lock_manager=locks,
            task_store=store,
            config=FAST_TICK,
        )

        expected = int(3600.0 + _CLAIM_TTL_GRACE_SECONDS)
        assert summary.spawned == 1
        # The claiming start and the post-spawn ref-recording start: the
        # second must carry it too, or it would hand the claim straight
        # back to the registry's generic default.
        assert registry.sent_claim_ttls[str(root.id)] == [expected, expected]

    async def test_no_executor_timeout_leaves_the_ttl_to_the_registry(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        (root,) = _chain("ttl-none-root")
        registry, locks, executor, store = _setup([root])

        await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,  # no timeout
            lock_manager=locks,
            task_store=store,
            config=FAST_TICK,
        )

        assert registry.sent_claim_ttls[str(root.id)] == [None, None]

    def test_ttl_is_clamped_to_the_servers_accepted_range(self):
        """A 10-second task and a 100-day task are both legitimate; each
        gets the closest expiry the server can express, not a 422."""
        from stardag.build._reactive import (
            _MAX_CLAIM_TTL_SECONDS,
            _MIN_CLAIM_TTL_SECONDS,
            claim_ttl_seconds,
        )

        (task,) = _chain("ttl-clamp")

        class _Timeout(FakeTickExecutor):
            def __init__(self, seconds):
                super().__init__(timeout_seconds=seconds)

        assert (
            claim_ttl_seconds(task, _Timeout(_MAX_CLAIM_TTL_SECONDS * 10))
            == _MAX_CLAIM_TTL_SECONDS
        )
        assert claim_ttl_seconds(task, _Timeout(None)) is None
        short = claim_ttl_seconds(task, _Timeout(1.0))
        assert short is not None and short >= _MIN_CLAIM_TTL_SECONDS

    def test_a_raising_executor_falls_back_to_the_registry_default(self):
        """Resolving a timeout is a diagnostic; it must never fail a start."""
        from stardag.build._reactive import claim_ttl_seconds

        (task,) = _chain("ttl-raises")

        class _Raising(FakeTickExecutor):
            def execution_timeout_seconds(self, task):
                raise RuntimeError("backend unreachable")

        assert claim_ttl_seconds(task, _Raising()) is None


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
            claim_ttl_seconds=None,
        ):
            self.acquire_metadata[str(task.id)] = executor_metadata
            return await super().task_start_with_limits_aio(
                build_id,
                task,
                executor=executor,
                executor_ref=executor_ref,
                executor_metadata=executor_metadata,
                limit_keys=limit_keys,
                claim_ttl_seconds=claim_ttl_seconds,
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


class ClaimingReactiveRegistry(FakeReactiveRegistry):
    """FakeReactiveRegistry with a scriptable cross-build claim race.

    ``claim_race_once`` simulates the race the claim closes: the frontier
    snapshot says PENDING, but by claim time another build's scheduler has
    already started (and instantly completed) the task.
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
        claim_ttl_seconds=None,
    ):
        from stardag.registry import StartClaimResult

        tid = str(task.id)
        if tid in self.claim_race_once:
            # "Another build" won this task just before us and its instant
            # worker completed it (completion wakes our scheduler).
            self.claim_race_once.discard(tid)
            self.calls.append(("start_claim", tid))
            self.statuses[tid] = "completed"
            self.needs_tick = True
            return StartClaimResult(
                started=False,
                denied_reason="already_running",
                executor="fake",
                executor_ref="fc-other-build",
            )
        return await super().task_start_claim_aio(
            build_id,
            task,
            executor=executor,
            executor_ref=executor_ref,
            executor_metadata=executor_metadata,
            limit_keys=limit_keys,
            claim_ttl_seconds=claim_ttl_seconds,
        )


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
        blocker_expires_in_seconds: float | None = None,
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
            expires_at=(
                None
                if blocker_expires_in_seconds is None
                else datetime.now(timezone.utc)
                + timedelta(seconds=blocker_expires_in_seconds)
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

    async def test_running_blocker_with_a_live_claim_waits(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The claim's expiry is in the future: somebody is executing it and
        the server still honours their claim. Wait — no lookup, no timer."""
        _, registry, locks, executor, store = self._blocked_build(
            blocker_expires_in_seconds=3600
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
        assert summary.external_blockers_waited == 1
        assert summary.external_blockers_fatal == 0
        assert registry.build_status == "running"
        # A RUNNING blocker is decided from its claim alone.
        assert registry.build_get_calls == []

    async def test_running_blocker_with_a_lapsed_claim_fails(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The claim lapsed: it is not "presumed" abandoned, it provably is
        — the server will hand it to the next claimant. This build is not
        that claimant, so it still fails, but now with certainty, naming the
        blocking task and the build that owns it."""
        _, registry, locks, executor, store = self._blocked_build(
            blocker_age_seconds=60 * 60 * 24,
            blocker_expires_in_seconds=-60,  # lapsed a minute ago
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
        assert registry.build_get_calls == []
        message = registry.build_error_message or ""
        assert "pipelines.Ingest" in message  # the blocking task
        assert self.BLOCKER_ID in message
        assert str(registry.status_build_id[self.BLOCKER_ID]) in message  # its owner
        assert "execution claim lapsed" in message
        # Certainty about the cause is not the power to fix it from here.
        assert "does not unblock this build" in message
        assert "release the claim" in message

    async def test_running_blocker_without_an_expiry_waits(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """NULL expiry — an older server, or a start recorded before the
        column. That is the server's own encoding of "never lapses", not
        evidence of death: waiting keeps a live blocker from failing a
        healthy build, and the log line says the wait cannot be shown to
        end. Deliberate; see _classify_external_blockers."""
        _, registry, locks, executor, store = self._blocked_build(
            blocker_age_seconds=60 * 60 * 24 * 365,  # a year, and still waited on
            blocker_expires_in_seconds=None,
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
        assert summary.external_blockers_waited == 1
        assert summary.external_blockers_fatal == 0
        assert registry.build_status == "running"

    @pytest.mark.parametrize("owner_build_status", ["completed", "running"])
    async def test_a_running_blockers_owning_build_is_never_consulted(
        self,
        owner_build_status: str,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
    ):
        """A live owning build proves nothing about one of its claims, and a
        terminal one does not release them — so for a RUNNING blocker the
        owner's status is not consulted in either direction."""
        _, registry, locks, executor, store = self._blocked_build(
            blocker_expires_in_seconds=3600,
            owner_build_status=owner_build_status,
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
        assert registry.build_get_calls == []

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

    @pytest.mark.parametrize("blocker_status", ["pending", "suspended"])
    async def test_a_non_running_blocker_carries_no_expiry_so_the_owner_is_asked(
        self,
        blocker_status: str,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
    ):
        """The two-row collapse applies to RUNNING blockers ONLY. A SUSPENDED
        (or PENDING) task holds no execution claim, so the server clears the
        expiry with it and there is nothing to read — the owning-build lookup
        is not kept out of caution, it is the only evidence that exists. The
        abandoned-SUSPENDED wedge is real and still has to be decided."""
        _, registry, locks, executor, store = self._blocked_build(
            blocker_status=blocker_status,
            blocker_age_seconds=60 * 60 * 24 * 30,  # age is not the question
            owner_build_status="running",
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
        assert summary.external_blockers_waited == 1
        assert len(registry.build_get_calls) == 1  # the lookup did happen

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
        # Both documented remedies are addressed to a build, so quoting
        # them here would hand the reader a URL with no id to put in it.
        # Say what can actually be done instead.
        assert "/retry" not in message
        assert "no build id to address a retry or cancel to" in message
        assert "stardag tasks list" in message

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


class TestTickSummaryReporting:
    """B6: a tick's own account of what it did must outlive its container.

    Reporting is strictly best-effort — it sits at the end of every tick,
    and observability failing must never fail a tick or change its
    outcome. These tests pin both halves: that the summaries do get
    reported, and that nothing about reporting can leak into the result.
    """

    @pytest.fixture(autouse=True)
    def _reset_route_flag(self):
        """The missing-route latch is process-global; isolate the tests."""
        reactive_module._tick_summary_route_missing = False
        yield
        reactive_module._tick_summary_route_missing = False

    async def _run(
        self,
        registry: FakeReactiveRegistry,
        locks,
        executor,
        store,
        config: TickConfig | None = None,
    ) -> TickSummary:
        return await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            lock_manager=locks,
            task_store=store,
            config=config or FAST_TICK,
        )

    async def test_terminal_tick_is_reported(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        dep, root = _chain("report-dep", "report-root")
        registry, locks, executor, store = _setup([dep, root])

        summary = await self._run(registry, locks, executor, store)

        assert summary.outcome == "terminal"
        assert len(registry.reported_tick_summaries) == 1
        reported = registry.reported_tick_summaries[0]
        # The whole dataclass rides, so a field added later needs no server
        # change (the summary is stored as an open blob).
        assert reported["outcome"] == "terminal"
        assert reported["terminal_status"] == "completed"
        assert reported["spawned"] == 2
        assert set(reported) == set(vars(summary))

    async def test_lingered_out_tick_is_reported(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """A tick that found nothing to do still says so."""
        (task,) = _chain("linger-only")
        registry, locks, executor, store = _setup([task], auto_complete=False)
        # Nothing actionable and something running -> not terminal, so the
        # tick lingers and exits on its deadline.
        registry.statuses[str(task.id)] = "running"
        registry.refs[str(task.id)] = ("fake", "ref-live")
        executor.probe_statuses["ref-live"] = DetachedExecutionStatus.RUNNING

        summary = await self._run(registry, locks, executor, store)

        assert summary.outcome == "lingered_out"
        assert [s["outcome"] for s in registry.reported_tick_summaries] == [
            "lingered_out"
        ]

    async def test_lease_held_tick_is_reported(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """Contention is signal: many of these means ticks are piling up."""
        dep, root = _chain("held-dep", "held-root")
        registry, locks, executor, store = _setup([dep, root], lease_acquired=False)

        summary = await self._run(registry, locks, executor, store)

        assert summary.outcome == "lease_held"
        assert [s["outcome"] for s in registry.reported_tick_summaries] == [
            "lease_held"
        ]

    async def test_not_reactive_tick_is_not_reported(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """A stray tick on a non-reactive build learnt nothing worth keeping."""
        dep, root = _chain("stray-dep", "stray-root")
        registry, locks, executor, store = _setup([dep, root])
        registry.reactive_app_name = None

        summary = await self._run(registry, locks, executor, store)

        assert summary.outcome == "not_reactive"
        assert registry.reported_tick_summaries == []

    async def test_disabled_by_config(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        dep, root = _chain("off-dep", "off-root")
        registry, locks, executor, store = _setup([dep, root])

        summary = await self._run(
            registry,
            locks,
            executor,
            store,
            config=TickConfig(
                linger_seconds=0.3,
                poll_interval_seconds=0.01,
                report_tick_summaries=False,
            ),
        )

        assert summary.outcome == "terminal"
        assert registry.reported_tick_summaries == []

    async def test_reporting_failure_does_not_affect_the_tick(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The contract: a broken registry must not fail or alter a tick."""
        dep, root = _chain("raise-dep", "raise-root")
        registry, locks, executor, store = _setup([dep, root])
        registry.tick_summary_error = RuntimeError("registry exploded")

        summary = await self._run(registry, locks, executor, store)

        assert summary.outcome == "terminal"
        assert summary.terminal_status == "completed"
        assert summary.spawned == 2
        assert registry.build_status == "completed"
        # It was attempted — the failure is swallowed, not skipped.
        assert len(registry.reported_tick_summaries) == 1

    async def test_missing_route_is_tolerated_and_latched(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """An older server 404s the route; don't pay for it every tick."""
        dep, root = _chain("route-dep", "route-root")
        registry, locks, executor, store = _setup([dep, root])
        # FastAPI's generic missing-route body — version skew, not an error.
        registry.tick_summary_error = NotFoundError(
            "Report tick summary: resource not found", detail="Not Found"
        )

        summary = await self._run(registry, locks, executor, store)
        assert summary.outcome == "terminal"
        assert len(registry.reported_tick_summaries) == 1
        assert reactive_module._tick_summary_route_missing is True

        # Second tick on a fresh build: the latch keeps it from re-trying.
        dep2, root2 = _chain("route-dep-2", "route-root-2")
        registry2, locks2, executor2, store2 = _setup([dep2, root2])
        summary2 = await self._run(registry2, locks2, executor2, store2)
        assert summary2.outcome == "terminal"
        assert registry2.reported_tick_summaries == []

    async def test_resource_404_does_not_latch(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """A build that vanished is not a reason to stop reporting others."""
        dep, root = _chain("gone-dep", "gone-root")
        registry, locks, executor, store = _setup([dep, root])
        registry.tick_summary_error = NotFoundError(
            "Report tick summary: resource not found", detail="Build not found"
        )

        summary = await self._run(registry, locks, executor, store)

        assert summary.outcome == "terminal"
        assert reactive_module._tick_summary_route_missing is False

    async def test_crashed_tick_is_reported_and_the_exception_propagates(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """A crashed tick is the most informative "why did this stall?" answer.

        Recording it must not change what the caller sees: the original
        exception is re-raised, with its type and message captured.
        """
        dep, root = _chain("crash-dep", "crash-root")
        registry, locks, executor, store = _setup([dep, root])
        registry.frontier_error = RuntimeError("frontier query exploded")

        with pytest.raises(RuntimeError, match="frontier query exploded"):
            await self._run(registry, locks, executor, store)

        assert len(registry.reported_tick_summaries) == 1
        reported = registry.reported_tick_summaries[0]
        assert reported["outcome"] == "error"
        assert reported["error_type"] == "RuntimeError"
        assert reported["error_message"] == "frontier query exploded"

    async def test_error_message_is_bounded(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """An unbounded message would blow the server's 8 KiB summary cap —
        turning a recorded failure into no record at all."""
        dep, root = _chain("huge-dep", "huge-root")
        registry, locks, executor, store = _setup([dep, root])
        registry.frontier_error = RuntimeError("x" * 50_000)

        with pytest.raises(RuntimeError):
            await self._run(registry, locks, executor, store)

        reported = registry.reported_tick_summaries[0]
        message = reported["error_message"]
        assert len(message) == reactive_module._MAX_ERROR_MESSAGE_CHARS
        assert message.endswith(reactive_module._TRUNCATION_MARKER)
        # The whole summary stays well inside the server's cap.
        assert len(json.dumps(reported, separators=(",", ":")).encode()) < 8192

    async def test_failing_to_report_a_crash_does_not_mask_it(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """A failure to record the failure is swallowed, never substituted."""
        dep, root = _chain("mask-dep", "mask-root")
        registry, locks, executor, store = _setup([dep, root])
        registry.frontier_error = RuntimeError("the original problem")
        registry.tick_summary_error = RuntimeError("and the reporter died too")

        with pytest.raises(RuntimeError, match="the original problem"):
            await self._run(registry, locks, executor, store)

    async def test_crash_reporting_respects_the_config_toggle(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        dep, root = _chain("crash-off-dep", "crash-off-root")
        registry, locks, executor, store = _setup([dep, root])
        registry.frontier_error = RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            await self._run(
                registry,
                locks,
                executor,
                store,
                config=TickConfig(
                    linger_seconds=0.3,
                    poll_interval_seconds=0.01,
                    report_tick_summaries=False,
                ),
            )

        assert registry.reported_tick_summaries == []


# =============================================================================
# Concurrent DAG discovery
# =============================================================================


class TrackedTask(SyncOnlyTask):
    """SyncOnlyTask whose completion check is observable and suspends.

    The suspension is what makes concurrency measurable at all: the
    in-memory target answers synchronously, so without it a "concurrent"
    walk and a serial one are indistinguishable.
    """

    # Class-level because discovery constructs nothing — the tracker has to
    # outlive individual instances and be shared across the whole walk.
    tracker: typing.ClassVar[dict[str, int]] = {}

    async def complete_aio(self) -> bool:
        TrackedTask.tracker["in_flight"] = TrackedTask.tracker.get("in_flight", 0) + 1
        TrackedTask.tracker["max_in_flight"] = max(
            TrackedTask.tracker.get("max_in_flight", 0),
            TrackedTask.tracker["in_flight"],
        )
        TrackedTask.tracker["checks"] = TrackedTask.tracker.get("checks", 0) + 1
        try:
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            return await super().complete_aio()
        finally:
            TrackedTask.tracker["in_flight"] -= 1


async def _serial_discover(
    tasks: TaskStructType,
) -> tuple[list[UUID], list[UUID], list[UUID]]:
    """The pre-concurrency walk, verbatim, as the reference implementation.

    Returns ``(post_order, incomplete, previously_completed)`` as id lists.
    Kept in the test rather than in the module so the concurrent
    implementation has something independent to be identical to.
    """
    post_order: list[BaseTask] = []
    incomplete: dict[UUID, BaseTask] = {}
    previously_completed: list[BaseTask] = []
    seen: set[UUID] = set()

    async def walk(task: BaseTask) -> None:
        if task.id in seen:
            return
        seen.add(task.id)
        if await task.complete_aio():
            previously_completed.append(task)
            post_order.append(task)
            return
        for dep in flatten_task_struct(task.requires()):
            await walk(dep)
        incomplete[task.id] = task
        post_order.append(task)

    for task in flatten_task_struct(tasks):
        await walk(task)
    return (
        [t.id for t in post_order],
        list(incomplete),
        [t.id for t in previously_completed],
    )


def _diamond() -> tuple[BaseTask, list[BaseTask]]:
    """A diamond with a shared leaf, a completed branch, and two roots.

    Shape (arrows point at dependencies)::

        root ─┬─ left  ─┬─ shared ── deep
              └─ right ─┘
              └─ done            (already complete: not recursed into)
    """
    deep = TrackedTask(name="dia-deep")
    shared = TrackedTask(name="dia-shared", deps=(deep,))
    left = TrackedTask(name="dia-left", deps=(shared,))
    right = TrackedTask(name="dia-right", deps=(shared,))
    done_dep = TrackedTask(name="dia-done-dep")
    done = TrackedTask(name="dia-done", deps=(done_dep,))
    done.run()  # complete → its subtree must NOT be walked
    root = TrackedTask(name="dia-root", deps=(left, right, done))
    return root, [deep, shared, left, right, done, done_dep, root]


class TestConcurrentDiscovery:
    async def test_matches_the_serial_walk_exactly_for_a_diamond(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """Same DAG, same DiscoveryResult — element for element, in order.

        Concurrency here buys throughput and nothing else: a walk whose
        whole job is to get an ordering right may not have its output
        depend on which completion check answered first.
        """
        root, _ = _diamond()
        (
            expected_post_order,
            expected_incomplete,
            expected_completed,
        ) = await _serial_discover(root)

        registry = FakeReactiveRegistry(root_task_ids=[str(root.id)])
        result = await discover_and_register_aio(registry, uuid4(), root)

        assert list(result.incomplete) == expected_incomplete
        assert [t.id for t in result.previously_completed] == expected_completed
        assert result.retried == []
        # Registration order is the post-order the bulk endpoint relies on.
        registered = [
            UUID(tid) for (method, tid) in registry.calls if method == "register"
        ]
        assert registered == expected_post_order

    async def test_post_order_holds_under_concurrency(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """Every dependency is registered before the task that needs it —
        which is what keeps the bulk endpoint from creating phantom rows
        while resolving ``dependency_task_ids``."""
        root, all_tasks = _diamond()
        registry = FakeReactiveRegistry(root_task_ids=[str(root.id)])

        await discover_and_register_aio(registry, uuid4(), root)

        order = {
            tid: index
            for index, tid in enumerate(
                [tid for (method, tid) in registry.calls if method == "register"]
            )
            if tid is not None
        }
        by_id = {str(task.id): task for task in all_tasks}
        for tid, index in order.items():
            task = by_id[tid]
            if str(task.id) == str(
                next(t.id for t in all_tasks if getattr(t, "name") == "dia-done")
            ):
                continue  # complete → not recursed into, deps not registered
            for dep in flatten_task_struct(task.requires()):
                assert order[str(dep.id)] < index

    async def test_completion_checks_run_concurrently_within_the_bound(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The bound is pinned, not just "it works": a wide layer's checks
        overlap, and never more than ``max_concurrent_discover`` at once.

        Without the semaphore the peak would be the whole layer; without
        the TaskGroup it would be 1 — the serial wall that made discovery
        50x slower than the resident engine's."""
        width, bound = 120, 6
        leaves = [TrackedTask(name=f"disc-wide-{i}") for i in range(width)]
        root = TrackedTask(name="disc-wide-root", deps=tuple(leaves))
        registry = FakeReactiveRegistry(root_task_ids=[str(root.id)])
        TrackedTask.tracker.clear()

        result = await discover_and_register_aio(
            registry, uuid4(), root, max_concurrent_discover=bound
        )

        assert len(result.incomplete) == width + 1
        assert TrackedTask.tracker["max_in_flight"] <= bound
        assert TrackedTask.tracker["max_in_flight"] == bound

    async def test_shared_dependency_is_checked_and_registered_once(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The dedupe the serial walk got for free from being serial: two
        concurrent walkers reaching the same dep must not double-register
        it, and must not lose the branch either."""
        root, all_tasks = _diamond()
        registry = FakeReactiveRegistry(root_task_ids=[str(root.id)])
        TrackedTask.tracker.clear()

        result = await discover_and_register_aio(registry, uuid4(), root)

        registered = [tid for (method, tid) in registry.calls if method == "register"]
        assert len(registered) == len(set(registered))
        # deep/shared/left/right/root incomplete; done complete; done's own
        # dep never walked (complete subtrees are not recursed into).
        by_name = {typing.cast(typing.Any, t).name: t for t in all_tasks}
        assert set(result.incomplete) == {
            by_name[name].id
            for name in ("dia-deep", "dia-shared", "dia-left", "dia-right", "dia-root")
        }
        assert [t.id for t in result.previously_completed] == [by_name["dia-done"].id]
        assert str(by_name["dia-done-dep"].id) not in registered
        # One completion check per visited task, no more.
        assert TrackedTask.tracker["checks"] == len(registered)

    async def test_retry_failed_preserves_order_and_membership(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """Retries now run concurrently; ``retried`` still comes back in the
        registry's own reporting order, not in completion order."""
        leaves = [SyncOnlyTask(name=f"disc-retry-{i}") for i in range(20)]
        root = SyncOnlyTask(name="disc-retry-root", deps=tuple(leaves))
        registry = FakeReactiveRegistry(root_task_ids=[str(root.id)])
        for task in [*leaves, root]:
            registry.add_task(str(task.id), status="failed")

        result = await discover_and_register_aio(
            registry, uuid4(), root, retry_failed=True
        )

        registered = [
            UUID(tid) for (method, tid) in registry.calls if method == "register"
        ]
        assert [t.id for t in result.retried] == registered
        assert all(registry.statuses[str(t.id)] == "pending" for t in [*leaves, root])
