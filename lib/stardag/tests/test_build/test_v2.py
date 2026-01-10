"""Tests for the v2 build system.

Tests the unified build system with:
- build_sequential(): Sync debugging build
- build_sequential_aio(): Async debugging build
- build(): Concurrent hybrid build
- build_sync(): Sync wrapper for concurrent build
"""

from __future__ import annotations

import asyncio
import time
import typing
from typing import Any

import pytest

from stardag._auto_task import AutoTask
from stardag._task import auto_namespace
from stardag.build._v2 import (
    BuildExitStatus,
    DefaultTaskRunner,
    FailMode,
    build,
    build_sequential,
    build_sequential_aio,
    build_sync,
)
from stardag.build.registry import NoOpRegistry
from stardag.target import InMemoryFileSystemTarget
from stardag.utils.testing.dynamic_deps_dag import (
    assert_dynamic_deps_task_complete_recursive,
    get_dynamic_deps_dag,
)
from stardag.utils.testing.simple_dag import (
    get_simple_dag,
    get_simple_dag_expected_root_output,
)

auto_namespace(__name__)


@pytest.fixture
def noop_registry():
    """Provide a NoOpRegistry for tests."""
    return NoOpRegistry()


# ============================================================================
# Test Tasks
# ============================================================================


class SyncOnlyTask(AutoTask[dict[str, Any]]):
    """Task with only sync run()."""

    name: str
    deps: tuple[AutoTask, ...] = ()

    def requires(self):
        return self.deps

    def run(self):
        self.output().save({"name": self.name, "mode": "sync"})


class AsyncOnlyTask(AutoTask[dict[str, Any]]):
    """Task with only async run_aio()."""

    name: str
    deps: tuple[AutoTask, ...] = ()

    def requires(self):
        return self.deps

    async def run_aio(self):
        await asyncio.sleep(0.01)  # Small async operation
        self.output().save({"name": self.name, "mode": "async"})


class DualTask(AutoTask[dict[str, Any]]):
    """Task with both sync run() and async run_aio()."""

    name: str
    prefer_async: bool = True
    deps: tuple[AutoTask, ...] = ()

    def requires(self):
        return self.deps

    def run(self):
        self.output().save({"name": self.name, "mode": "sync"})

    async def run_aio(self):
        await asyncio.sleep(0.01)
        self.output().save({"name": self.name, "mode": "async"})


class FailingTask(AutoTask[str]):
    """Task that always fails."""

    error_message: str = "Intentional failure"

    def run(self):
        raise ValueError(self.error_message)


class FailingAsyncTask(AutoTask[str]):
    """Async task that always fails."""

    error_message: str = "Intentional async failure"

    async def run_aio(self):
        await asyncio.sleep(0.001)
        raise ValueError(self.error_message)


class SlowTask(AutoTask[dict[str, Any]]):
    """Task with configurable delay for testing concurrency."""

    name: str
    delay: float = 0.1
    deps: tuple[AutoTask, ...] = ()

    def requires(self):
        return self.deps

    def run(self):
        start = time.time()
        time.sleep(self.delay)
        end = time.time()
        self.output().save({"name": self.name, "start": start, "end": end})


# ============================================================================
# Test: build_sequential
# ============================================================================


class TestBuildSequential:
    """Tests for build_sequential() - sync debugging build."""

    def test_simple_dag(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileSystemTarget],
        noop_registry,
    ):
        """Test building a simple DAG sequentially."""
        dag = get_simple_dag()
        expected_output = get_simple_dag_expected_root_output()

        summary = build_sequential([dag], registry=noop_registry)

        assert summary.status == BuildExitStatus.SUCCESS
        assert dag.complete()
        assert dag.output().load() == expected_output

    def test_sync_only_task(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileSystemTarget],
        noop_registry,
    ):
        """Test sync-only tasks execute via run()."""
        task = SyncOnlyTask(name="test")

        summary = build_sequential([task], registry=noop_registry)

        assert summary.status == BuildExitStatus.SUCCESS
        assert task.output().load()["mode"] == "sync"

    def test_dual_task_uses_sync_by_default(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileSystemTarget],
        noop_registry,
    ):
        """Test dual tasks use sync by default in build_sequential."""
        task = DualTask(name="test")

        summary = build_sequential([task], registry=noop_registry)

        assert summary.status == BuildExitStatus.SUCCESS
        assert task.output().load()["mode"] == "sync"

    def test_dual_task_uses_async_when_configured(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileSystemTarget],
        noop_registry,
    ):
        """Test dual tasks can use async when configured."""
        task = DualTask(name="test")

        summary = build_sequential(
            [task], registry=noop_registry, dual_run_default="async"
        )

        assert summary.status == BuildExitStatus.SUCCESS
        assert task.output().load()["mode"] == "async"

    def test_fail_fast_mode(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileSystemTarget],
        noop_registry,
    ):
        """Test FAIL_FAST mode stops at first failure."""
        task1 = SyncOnlyTask(name="task1")
        task2 = FailingTask()
        task3 = SyncOnlyTask(name="task3", deps=(task2,))

        summary = build_sequential(
            [task1, task3], registry=noop_registry, fail_mode=FailMode.FAIL_FAST
        )

        assert summary.status == BuildExitStatus.FAILURE
        assert summary.error is not None
        assert summary.task_count.failed == 1
        # task1 should have completed, task3 depends on failed task2
        assert task1.complete()

    def test_continue_mode(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileSystemTarget],
        noop_registry,
    ):
        """Test CONTINUE mode runs all possible tasks."""
        good_task = SyncOnlyTask(name="good")
        failing = FailingTask()
        dependent = SyncOnlyTask(name="dependent", deps=(failing,))

        summary = build_sequential(
            [good_task, dependent],
            registry=noop_registry,
            fail_mode=FailMode.CONTINUE,
        )

        assert summary.status == BuildExitStatus.FAILURE
        assert summary.task_count.failed >= 1
        # good_task should still complete
        assert good_task.complete()

    def test_already_complete_tasks_skipped(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileSystemTarget],
        noop_registry,
    ):
        """Test that already complete tasks are not re-executed."""
        task = SyncOnlyTask(name="test")
        # Pre-complete the task
        task.output().save({"name": "test", "mode": "pre-existing"})

        summary = build_sequential([task], registry=noop_registry)

        assert summary.status == BuildExitStatus.SUCCESS
        assert summary.task_count.previously_completed == 1
        assert summary.task_count.succeeded == 0
        # Output should be unchanged
        assert task.output().load()["mode"] == "pre-existing"

    def test_multiple_root_tasks(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileSystemTarget],
        noop_registry,
    ):
        """Test building multiple independent root tasks."""
        task1 = SyncOnlyTask(name="root1")
        task2 = SyncOnlyTask(name="root2")
        task3 = SyncOnlyTask(name="root3")

        summary = build_sequential([task1, task2, task3], registry=noop_registry)

        assert summary.status == BuildExitStatus.SUCCESS
        assert task1.complete()
        assert task2.complete()
        assert task3.complete()
        assert summary.task_count.succeeded == 3


# ============================================================================
# Test: build_sequential_aio
# ============================================================================


class TestBuildSequentialAio:
    """Tests for build_sequential_aio() - async debugging build."""

    @pytest.mark.asyncio
    async def test_simple_dag(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileSystemTarget],
        noop_registry,
    ):
        """Test building a simple DAG sequentially async."""
        dag = get_simple_dag()
        expected_output = get_simple_dag_expected_root_output()

        summary = await build_sequential_aio([dag], registry=noop_registry)

        assert summary.status == BuildExitStatus.SUCCESS
        assert dag.complete()
        assert dag.output().load() == expected_output

    @pytest.mark.asyncio
    async def test_async_only_task(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileSystemTarget],
        noop_registry,
    ):
        """Test async-only tasks execute via run_aio()."""
        task = AsyncOnlyTask(name="test")

        summary = await build_sequential_aio([task], registry=noop_registry)

        assert summary.status == BuildExitStatus.SUCCESS
        assert task.output().load()["mode"] == "async"

    @pytest.mark.asyncio
    async def test_sync_task_in_thread_by_default(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileSystemTarget],
        noop_registry,
    ):
        """Test sync tasks run in thread by default in async sequential."""
        task = SyncOnlyTask(name="test")

        # With blocking mode, sync tasks run in current thread
        summary = await build_sequential_aio(
            [task], registry=noop_registry, sync_run_default="blocking"
        )

        assert summary.status == BuildExitStatus.SUCCESS
        assert task.output().load()["mode"] == "sync"

    @pytest.mark.asyncio
    async def test_fail_fast_mode(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileSystemTarget],
        noop_registry,
    ):
        """Test FAIL_FAST mode in async build."""
        task1 = AsyncOnlyTask(name="task1")
        failing = FailingAsyncTask()
        task2 = AsyncOnlyTask(name="task2", deps=(failing,))

        summary = await build_sequential_aio(
            [task1, task2], registry=noop_registry, fail_mode=FailMode.FAIL_FAST
        )

        assert summary.status == BuildExitStatus.FAILURE
        assert summary.task_count.failed >= 1


# ============================================================================
# Test: build (concurrent hybrid)
# ============================================================================


class TestBuildConcurrent:
    """Tests for build() - concurrent hybrid build."""

    @pytest.mark.asyncio
    async def test_simple_dag(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileSystemTarget],
        noop_registry,
    ):
        """Test building a simple DAG concurrently."""
        dag = get_simple_dag()
        expected_output = get_simple_dag_expected_root_output()

        summary = await build([dag], registry=noop_registry)

        assert summary.status == BuildExitStatus.SUCCESS
        assert dag.complete()
        assert dag.output().load() == expected_output

    @pytest.mark.asyncio
    async def test_dynamic_deps(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileSystemTarget],
        noop_registry,
    ):
        """Test building DAG with dynamic dependencies."""
        dag = get_dynamic_deps_dag()
        assert_dynamic_deps_task_complete_recursive(dag, False)

        summary = await build([dag], registry=noop_registry)

        assert summary.status == BuildExitStatus.SUCCESS
        assert_dynamic_deps_task_complete_recursive(dag, True)

    @pytest.mark.asyncio
    async def test_concurrent_execution(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileSystemTarget],
        noop_registry,
    ):
        """Test that independent tasks execute concurrently."""
        # Create two independent slow tasks
        task_a = SlowTask(name="A", delay=0.1)
        task_b = SlowTask(name="B", delay=0.1)
        root = SlowTask(name="root", delay=0.01, deps=(task_a, task_b))

        start = time.time()
        summary = await build([root], registry=noop_registry)
        elapsed = time.time() - start

        assert summary.status == BuildExitStatus.SUCCESS
        # If sequential: ~0.21s. If concurrent: ~0.12s
        assert elapsed < 0.18, f"Expected concurrent execution, took {elapsed:.2f}s"

        # Verify tasks ran (approximately) in parallel
        a_data = task_a.output().load()
        b_data = task_b.output().load()
        # Their execution windows should overlap
        assert a_data["start"] < b_data["end"] and b_data["start"] < a_data["end"]

    @pytest.mark.asyncio
    async def test_fail_fast_mode(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileSystemTarget],
        noop_registry,
    ):
        """Test FAIL_FAST mode stops build on first failure."""
        failing = FailingTask()
        dependent = SyncOnlyTask(name="dependent", deps=(failing,))

        summary = await build(
            [dependent], registry=noop_registry, fail_mode=FailMode.FAIL_FAST
        )

        assert summary.status == BuildExitStatus.FAILURE
        assert summary.task_count.failed >= 1

    @pytest.mark.asyncio
    async def test_continue_mode(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileSystemTarget],
        noop_registry,
    ):
        """Test CONTINUE mode runs all possible tasks despite failures."""
        good_task = SyncOnlyTask(name="good")
        failing = FailingTask()
        dependent = SyncOnlyTask(name="dependent", deps=(failing,))

        summary = await build(
            [good_task, dependent],
            registry=noop_registry,
            fail_mode=FailMode.CONTINUE,
        )

        assert summary.status == BuildExitStatus.FAILURE
        # good_task should still complete
        assert good_task.complete()

    @pytest.mark.asyncio
    async def test_multiple_root_tasks(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileSystemTarget],
        noop_registry,
    ):
        """Test building multiple independent root tasks."""
        task1 = SyncOnlyTask(name="root1")
        task2 = SyncOnlyTask(name="root2")
        task3 = SyncOnlyTask(name="root3")

        summary = await build([task1, task2, task3], registry=noop_registry)

        assert summary.status == BuildExitStatus.SUCCESS
        assert task1.complete()
        assert task2.complete()
        assert task3.complete()

    @pytest.mark.asyncio
    async def test_async_tasks_use_main_loop(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileSystemTarget],
        noop_registry,
    ):
        """Test async-only tasks execute in main event loop."""
        task = AsyncOnlyTask(name="async_test")

        summary = await build([task], registry=noop_registry)

        assert summary.status == BuildExitStatus.SUCCESS
        assert task.output().load()["mode"] == "async"


# ============================================================================
# Test: build_sync
# ============================================================================


class TestBuildSync:
    """Tests for build_sync() - sync wrapper for concurrent build."""

    def test_simple_dag(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileSystemTarget],
        noop_registry,
    ):
        """Test build_sync with simple DAG."""
        dag = get_simple_dag()
        expected_output = get_simple_dag_expected_root_output()

        summary = build_sync([dag], registry=noop_registry)

        assert summary.status == BuildExitStatus.SUCCESS
        assert dag.complete()
        assert dag.output().load() == expected_output

    def test_dynamic_deps(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileSystemTarget],
        noop_registry,
    ):
        """Test build_sync with dynamic dependencies."""
        dag = get_dynamic_deps_dag()
        assert_dynamic_deps_task_complete_recursive(dag, False)

        summary = build_sync([dag], registry=noop_registry)

        assert summary.status == BuildExitStatus.SUCCESS
        assert_dynamic_deps_task_complete_recursive(dag, True)


# ============================================================================
# Test: DefaultTaskRunner
# ============================================================================


class TestDefaultTaskRunner:
    """Tests for DefaultTaskRunner."""

    @pytest.mark.asyncio
    async def test_sync_task_runs_in_thread(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileSystemTarget],
        noop_registry,
    ):
        """Test sync tasks run in thread pool."""
        runner = DefaultTaskRunner(registry=noop_registry, max_thread_workers=2)
        task = SyncOnlyTask(name="test")

        await runner.setup()
        try:
            result = await runner.submit(task)
            assert result is None  # Success
            assert task.complete()
        finally:
            await runner.teardown()

    @pytest.mark.asyncio
    async def test_async_task_runs_in_loop(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileSystemTarget],
        noop_registry,
    ):
        """Test async tasks run in main event loop."""
        runner = DefaultTaskRunner(registry=noop_registry, max_async_workers=2)
        task = AsyncOnlyTask(name="test")

        await runner.setup()
        try:
            result = await runner.submit(task)
            assert result is None  # Success
            assert task.complete()
        finally:
            await runner.teardown()

    @pytest.mark.asyncio
    async def test_failing_task_returns_exception(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileSystemTarget],
        noop_registry,
    ):
        """Test failing tasks return exception."""
        runner = DefaultTaskRunner(registry=noop_registry)
        task = FailingTask(error_message="test error")

        await runner.setup()
        try:
            result = await runner.submit(task)
            assert isinstance(result, Exception)
            assert "test error" in str(result)
        finally:
            await runner.teardown()


# ============================================================================
# Test: BuildSummary and TaskCount
# ============================================================================


class TestBuildSummary:
    """Tests for BuildSummary and TaskCount."""

    def test_task_count_pending(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileSystemTarget],
        noop_registry,
    ):
        """Test TaskCount.pending property."""
        # Build a DAG to get real counts
        leaf1 = SyncOnlyTask(name="leaf1")
        leaf2 = SyncOnlyTask(name="leaf2")
        root = SyncOnlyTask(name="root", deps=(leaf1, leaf2))

        summary = build_sequential([root], registry=noop_registry)

        assert summary.task_count.discovered == 3
        assert summary.task_count.succeeded == 3
        assert summary.task_count.pending == 0

    def test_summary_with_partial_completion(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileSystemTarget],
        noop_registry,
    ):
        """Test summary with previously completed tasks."""
        leaf = SyncOnlyTask(name="leaf")
        root = SyncOnlyTask(name="root", deps=(leaf,))

        # Pre-complete leaf
        leaf.output().save({"name": "leaf", "mode": "pre"})

        summary = build_sequential([root], registry=noop_registry)

        assert summary.status == BuildExitStatus.SUCCESS
        assert summary.task_count.discovered == 2
        assert summary.task_count.previously_completed == 1
        assert summary.task_count.succeeded == 1


# ============================================================================
# Test: Diamond DAG Patterns
# ============================================================================


# Global tracking for diamond tests - reset per test
_execution_counts: dict[str, int] = {}


class DiamondTask(AutoTask[str]):
    """Task that tracks execution count for diamond pattern tests."""

    name: str
    test_id: str  # Unique per test to isolate task IDs
    deps: tuple[AutoTask, ...] = ()

    def requires(self):
        return self.deps

    def run(self):
        global _execution_counts
        key = f"{self.test_id}:{self.name}"
        _execution_counts[key] = _execution_counts.get(key, 0) + 1
        self.output().save(f"{self.name}:{_execution_counts[key]}")


class DynamicDiamondTask(AutoTask[str]):
    """Task with dynamic deps that tracks execution for diamond tests."""

    name: str
    test_id: str
    static_task_deps: tuple["DynamicDiamondTask", ...] = ()
    dynamic_task_deps: tuple["DynamicDiamondTask", ...] = ()

    def requires(self):
        return self.static_task_deps

    def run(self):
        global _execution_counts
        key = f"{self.test_id}:{self.name}"
        _execution_counts[key] = _execution_counts.get(key, 0) + 1

        # Yield dynamic deps
        for dep in self.dynamic_task_deps:
            yield dep

        self.output().save(f"{self.name}:{_execution_counts[key]}")


class TestDiamondPatterns:
    """Tests for diamond DAG patterns - ensure tasks execute exactly once."""

    def test_static_diamond_sequential(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileSystemTarget],
        noop_registry,
    ):
        """
        Static diamond pattern with sequential build.

               root
              /    \\
            mid1   mid2
              \\    /
               leaf
        """
        global _execution_counts
        _execution_counts = {}

        leaf = DiamondTask(name="leaf", test_id="seq1")
        mid1 = DiamondTask(name="mid1", test_id="seq1", deps=(leaf,))
        mid2 = DiamondTask(name="mid2", test_id="seq1", deps=(leaf,))
        root = DiamondTask(name="root", test_id="seq1", deps=(mid1, mid2))

        summary = build_sequential([root], registry=noop_registry)

        assert summary.status == BuildExitStatus.SUCCESS
        assert summary.task_count.discovered == 4
        assert summary.task_count.succeeded == 4

        # Each task should execute exactly once
        assert _execution_counts.get("seq1:leaf") == 1
        assert _execution_counts.get("seq1:mid1") == 1
        assert _execution_counts.get("seq1:mid2") == 1
        assert _execution_counts.get("seq1:root") == 1

    @pytest.mark.asyncio
    async def test_static_diamond_concurrent(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileSystemTarget],
        noop_registry,
    ):
        """
        Static diamond pattern with concurrent build.
        Tests that leaf only executes once even when mid1/mid2 could race.
        """
        global _execution_counts
        _execution_counts = {}

        leaf = DiamondTask(name="leaf", test_id="conc1")
        mid1 = DiamondTask(name="mid1", test_id="conc1", deps=(leaf,))
        mid2 = DiamondTask(name="mid2", test_id="conc1", deps=(leaf,))
        root = DiamondTask(name="root", test_id="conc1", deps=(mid1, mid2))

        summary = await build([root], registry=noop_registry)

        assert summary.status == BuildExitStatus.SUCCESS
        assert summary.task_count.discovered == 4
        assert summary.task_count.succeeded == 4

        # Each task should execute exactly once
        assert _execution_counts.get("conc1:leaf") == 1
        assert _execution_counts.get("conc1:mid1") == 1
        assert _execution_counts.get("conc1:mid2") == 1
        assert _execution_counts.get("conc1:root") == 1

    def test_dynamic_diamond_sequential(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileSystemTarget],
        noop_registry,
    ):
        """
        Diamond pattern where shared task is both static and dynamic dep.

        parent (static: [dyn_task, shared])
           |
        dyn_task (dynamic: [shared])
           |
        shared (appears in both paths)
        """
        global _execution_counts
        _execution_counts = {}

        shared = DynamicDiamondTask(name="shared", test_id="dyn_seq1")
        dyn_task = DynamicDiamondTask(
            name="dyn_task",
            test_id="dyn_seq1",
            dynamic_task_deps=(shared,),
        )
        parent = DynamicDiamondTask(
            name="parent",
            test_id="dyn_seq1",
            static_task_deps=(dyn_task, shared),
        )

        summary = build_sequential([parent], registry=noop_registry)

        assert summary.status == BuildExitStatus.SUCCESS

        # shared appears as both static dep of parent AND dynamic dep of dyn_task
        # It should still only execute once
        assert _execution_counts.get("dyn_seq1:shared") == 1
        assert _execution_counts.get("dyn_seq1:dyn_task") == 1
        assert _execution_counts.get("dyn_seq1:parent") == 1

    @pytest.mark.asyncio
    async def test_dynamic_diamond_concurrent(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileSystemTarget],
        noop_registry,
    ):
        """
        Diamond pattern with dynamic deps in concurrent build.
        Tests the exact pattern from get_dynamic_deps_dag().
        """
        global _execution_counts
        _execution_counts = {}

        shared = DynamicDiamondTask(name="shared", test_id="dyn_conc1")
        dyn_task = DynamicDiamondTask(
            name="dyn_task",
            test_id="dyn_conc1",
            dynamic_task_deps=(shared,),
        )
        parent = DynamicDiamondTask(
            name="parent",
            test_id="dyn_conc1",
            static_task_deps=(dyn_task, shared),
        )

        summary = await build([parent], registry=noop_registry)

        assert summary.status == BuildExitStatus.SUCCESS

        # Each task should execute exactly once
        assert _execution_counts.get("dyn_conc1:shared") == 1
        assert _execution_counts.get("dyn_conc1:dyn_task") == 1
        assert _execution_counts.get("dyn_conc1:parent") == 1

    def test_complex_dynamic_diamond_sequential(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileSystemTarget],
        noop_registry,
    ):
        """
        Complex pattern matching get_dynamic_deps_dag() exactly.

        parent (static: [dyn_and_static, shared_31])
           |
        dyn_and_static (static: [t20, t21], dynamic: [t30, shared_31])
           |
        shared_31 appears in both parent's static and dyn_and_static's dynamic
        """
        global _execution_counts
        _execution_counts = {}

        t20 = DynamicDiamondTask(name="20", test_id="complex_seq")
        t21 = DynamicDiamondTask(name="21", test_id="complex_seq")
        t30 = DynamicDiamondTask(name="30", test_id="complex_seq")
        shared_31 = DynamicDiamondTask(name="31", test_id="complex_seq")

        dyn_and_static = DynamicDiamondTask(
            name="1",
            test_id="complex_seq",
            static_task_deps=(t20, t21),
            dynamic_task_deps=(t30, shared_31),
        )
        parent = DynamicDiamondTask(
            name="0",
            test_id="complex_seq",
            static_task_deps=(dyn_and_static, shared_31),
        )

        # Verify IDs match for the shared task
        parent_static_31 = parent.static_task_deps[1]
        dyn_dynamic_31 = dyn_and_static.dynamic_task_deps[1]
        assert parent_static_31.id == dyn_dynamic_31.id, (
            "Shared task should have same ID"
        )

        summary = build_sequential([parent], registry=noop_registry)

        assert summary.status == BuildExitStatus.SUCCESS

        # shared_31 should execute exactly once despite appearing in both places
        assert _execution_counts.get("complex_seq:31") == 1
        assert _execution_counts.get("complex_seq:30") == 1
        assert _execution_counts.get("complex_seq:20") == 1
        assert _execution_counts.get("complex_seq:21") == 1
        assert _execution_counts.get("complex_seq:1") == 1
        assert _execution_counts.get("complex_seq:0") == 1

    @pytest.mark.asyncio
    async def test_complex_dynamic_diamond_concurrent(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileSystemTarget],
        noop_registry,
    ):
        """
        Complex pattern matching get_dynamic_deps_dag() with concurrent build.
        """
        global _execution_counts
        _execution_counts = {}

        t20 = DynamicDiamondTask(name="20", test_id="complex_conc")
        t21 = DynamicDiamondTask(name="21", test_id="complex_conc")
        t30 = DynamicDiamondTask(name="30", test_id="complex_conc")
        shared_31 = DynamicDiamondTask(name="31", test_id="complex_conc")

        dyn_and_static = DynamicDiamondTask(
            name="1",
            test_id="complex_conc",
            static_task_deps=(t20, t21),
            dynamic_task_deps=(t30, shared_31),
        )
        parent = DynamicDiamondTask(
            name="0",
            test_id="complex_conc",
            static_task_deps=(dyn_and_static, shared_31),
        )

        summary = await build([parent], registry=noop_registry)

        assert summary.status == BuildExitStatus.SUCCESS

        # Each task should execute exactly once
        assert _execution_counts.get("complex_conc:31") == 1
        assert _execution_counts.get("complex_conc:30") == 1
        assert _execution_counts.get("complex_conc:20") == 1
        assert _execution_counts.get("complex_conc:21") == 1
        assert _execution_counts.get("complex_conc:1") == 1
        assert _execution_counts.get("complex_conc:0") == 1
