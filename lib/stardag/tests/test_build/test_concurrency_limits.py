"""Tests for granular concurrency limiting in build_aio / build.

Covers ConcurrencyConfig + LocalConcurrencyLimiter:
- overall cap (max_concurrent_tasks)
- named limits (key_selector -> str)
- multiple keys per task (sorted, deadlock-free acquisition)
- slot released across dynamic-deps suspension (no deadlock under tight cap)
- unknown limit name raises
- invalid limit value raises at construction
- composition with the global lock
"""

from __future__ import annotations

import asyncio
import typing
from collections import defaultdict

import pytest

from stardag import Task, auto_namespace
from stardag.build import (
    BuildExitStatus,
    ConcurrencyConfig,
    GlobalLockConfig,
    LocalConcurrencyLimiter,
    build_aio,
)
from stardag.build._base import (
    GlobalConcurrencyLockManager,
    LockAcquisitionResult,
    LockAcquisitionStatus,
)
from stardag.target import InMemoryFileTarget
from stardag.utils.testing.dynamic_deps_dag import (
    assert_dynamic_deps_task_complete_recursive,
    get_dynamic_deps_dag,
)

auto_namespace(__name__)


# ============================================================================
# Concurrency tracking
# ============================================================================


class _ConcurrencyTracker:
    """Records concurrent-execution counts per group (asyncio: no locking)."""

    def __init__(self) -> None:
        self.current: dict[str, int] = defaultdict(int)
        self.max_seen: dict[str, int] = defaultdict(int)
        self.total_current = 0
        self.total_max = 0

    def enter(self, group: str) -> None:
        self.total_current += 1
        self.total_max = max(self.total_max, self.total_current)
        self.current[group] += 1
        self.max_seen[group] = max(self.max_seen[group], self.current[group])

    def exit(self, group: str) -> None:
        self.current[group] -= 1
        self.total_current -= 1


# Reset per test by the ``tracker`` fixture.
_TRACKER = _ConcurrencyTracker()


@pytest.fixture
def tracker() -> _ConcurrencyTracker:
    global _TRACKER
    _TRACKER = _ConcurrencyTracker()
    return _TRACKER


class TrackedTask(Task[dict]):
    """Async task that records its concurrent-execution window in _TRACKER."""

    name: str
    group: str = "default"
    # Limit key(s) returned by the key_selector for this task; None = unlimited.
    limit_key: str | tuple[str, ...] | None = None
    delay: float = 0.05
    deps: tuple["TrackedTask", ...] = ()

    def requires(self):
        return self.deps

    async def run_aio(self):
        _TRACKER.enter(self.group)
        try:
            await asyncio.sleep(self.delay)
        finally:
            _TRACKER.exit(self.group)
        self._save({"name": self.name})


def _key_selector(task) -> str | tuple[str, ...] | None:
    return getattr(task, "limit_key", None)


# ============================================================================
# Overall cap
# ============================================================================


async def test_overall_cap_throttles_total_concurrency(
    default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
    tracker: _ConcurrencyTracker,
    noop_registry,
):
    tasks = [TrackedTask(name=f"t{i}", group="g") for i in range(6)]

    summary = await build_aio(
        tasks,
        registry=noop_registry,
        concurrency_config=ConcurrencyConfig(max_concurrent_tasks=2),
    )

    assert summary.status == BuildExitStatus.SUCCESS
    assert tracker.total_max == 2, f"expected cap of 2, saw {tracker.total_max}"


async def test_no_config_does_not_throttle(
    default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
    tracker: _ConcurrencyTracker,
    noop_registry,
):
    tasks = [TrackedTask(name=f"t{i}", group="g") for i in range(5)]

    summary = await build_aio(tasks, registry=noop_registry)

    assert summary.status == BuildExitStatus.SUCCESS
    # All five are independent and async -> run concurrently on the main loop.
    assert tracker.total_max == 5


# ============================================================================
# Named limits
# ============================================================================


async def test_named_limit_throttles_only_its_group(
    default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
    tracker: _ConcurrencyTracker,
    noop_registry,
):
    limited = [
        TrackedTask(name=f"l{i}", group="limited", limit_key="limited")
        for i in range(4)
    ]
    free = [TrackedTask(name=f"f{i}", group="free") for i in range(4)]

    summary = await build_aio(
        limited + free,
        registry=noop_registry,
        concurrency_config=ConcurrencyConfig(
            limits={"limited": 2},
            key_selector=_key_selector,
        ),
    )

    assert summary.status == BuildExitStatus.SUCCESS
    assert tracker.max_seen["limited"] == 2
    # The un-keyed group is unaffected and runs fully concurrently.
    assert tracker.max_seen["free"] == 4


async def test_multiple_keys_respects_tightest_and_no_deadlock(
    default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
    tracker: _ConcurrencyTracker,
    noop_registry,
):
    # Each task is bound to both "x" (limit 1) and "y" (limit 5); the key
    # order is intentionally unsorted to exercise sorted acquisition.
    tasks = [
        TrackedTask(name=f"m{i}", group="m", limit_key=("y", "x")) for i in range(4)
    ]

    summary = await build_aio(
        tasks,
        registry=noop_registry,
        concurrency_config=ConcurrencyConfig(
            limits={"x": 1, "y": 5},
            key_selector=_key_selector,
        ),
    )

    assert summary.status == BuildExitStatus.SUCCESS
    # "x" caps at 1 -> these tasks are fully serialized.
    assert tracker.max_seen["m"] == 1


# ============================================================================
# Dynamic deps: slot released on suspend (no deadlock under tight cap)
# ============================================================================


async def test_dynamic_deps_under_tight_cap_does_not_deadlock(
    default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
    noop_registry,
):
    # With max_concurrent_tasks=1, a parent that suspends on its own dynamic
    # deps must release its slot, or the dep could never acquire one. Wrap in
    # wait_for so a regression fails fast instead of hanging.
    dag = get_dynamic_deps_dag()
    assert_dynamic_deps_task_complete_recursive(dag, False)

    summary = await asyncio.wait_for(
        build_aio(
            [dag],
            registry=noop_registry,
            concurrency_config=ConcurrencyConfig(max_concurrent_tasks=1),
        ),
        timeout=10,
    )

    assert summary.status == BuildExitStatus.SUCCESS
    assert_dynamic_deps_task_complete_recursive(dag, True)


# ============================================================================
# Validation / error handling
# ============================================================================


async def test_unknown_limit_key_raises(
    default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
    noop_registry,
):
    task = TrackedTask(name="t", limit_key="not-defined")

    with pytest.raises(ValueError, match="not defined"):
        await build_aio(
            [task],
            registry=noop_registry,
            concurrency_config=ConcurrencyConfig(
                limits={"known": 1},
                key_selector=_key_selector,
            ),
        )


def test_invalid_limit_value_raises_at_construction():
    with pytest.raises(ValueError, match=">= 1"):
        LocalConcurrencyLimiter(ConcurrencyConfig(limits={"bad": 0}))

    with pytest.raises(ValueError, match="max_concurrent_tasks"):
        LocalConcurrencyLimiter(ConcurrencyConfig(max_concurrent_tasks=0))


# ============================================================================
# Composition with the global lock
# ============================================================================


class _AlwaysAcquireLockManager:
    """Minimal lock manager that always grants the lock (no real locking)."""

    async def acquire(self, task_id: str) -> LockAcquisitionResult:
        return LockAcquisitionResult(
            status=LockAcquisitionStatus.ACQUIRED, acquired=True
        )

    async def release(self, task_id: str, task_completed: bool = False) -> bool:
        return True

    def lock(self, task_id: str):  # pragma: no cover - unused by build_aio
        raise NotImplementedError


async def test_cap_composes_with_global_lock(
    default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
    tracker: _ConcurrencyTracker,
    noop_registry,
):
    lock_manager: GlobalConcurrencyLockManager = (
        _AlwaysAcquireLockManager()  # type: ignore[assignment]
    )
    tasks = [TrackedTask(name=f"t{i}", group="g") for i in range(6)]

    summary = await build_aio(
        tasks,
        registry=noop_registry,
        global_lock_manager=lock_manager,
        global_lock_config=GlobalLockConfig(enabled=True),
        concurrency_config=ConcurrencyConfig(max_concurrent_tasks=2),
    )

    assert summary.status == BuildExitStatus.SUCCESS
    assert tracker.total_max == 2
