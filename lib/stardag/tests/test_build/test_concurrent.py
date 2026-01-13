"""Tests for concurrent build implementation.

Tests build() and build_aio() from stardag.build._concurrent,
including HybridConcurrentTaskExecutor.
"""

from __future__ import annotations

import asyncio
import threading
import time
import typing

import pytest

from stardag._auto_task import AutoTask
from stardag._task import _has_custom_run_aio, auto_namespace
from stardag.build import (
    BuildExitStatus,
    DefaultExecutionModeSelector,
    FailMode,
    HybridConcurrentTaskExecutor,
    build,
    build_aio,
)
from stardag.registry import NoOpRegistry
from stardag.target import InMemoryFileSystemTarget
from stardag.utils.testing.dynamic_deps_dag import (
    assert_dynamic_deps_task_complete_recursive,
    get_dynamic_deps_dag,
)
from stardag.utils.testing.helper_tasks import (
    AsyncOnlyTask,
    DiamondTask,
    DynamicDiamondTask,
    FailingTask,
    SlowTask,
    SyncOnlyTask,
    get_execution_count,
    reset_execution_counts,
)
from stardag.utils.testing.simple_dag import (
    get_simple_dag,
    get_simple_dag_expected_root_output,
)

auto_namespace(__name__)


# ============================================================================
# Test: build (sync wrapper)
# ============================================================================


class TestBuildSyncWrapper:
    """Tests for build() - sync wrapper for concurrent build (default)."""

    def test_simple_dag(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileSystemTarget],
        noop_registry,
    ):
        """Test build with simple DAG."""
        dag = get_simple_dag()
        expected_output = get_simple_dag_expected_root_output()

        summary = build([dag], registry=noop_registry)

        assert summary.status == BuildExitStatus.SUCCESS
        assert dag.complete()
        assert dag.output().load() == expected_output

    def test_dynamic_deps(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileSystemTarget],
        noop_registry,
    ):
        """Test build with dynamic dependencies."""
        dag = get_dynamic_deps_dag()
        assert_dynamic_deps_task_complete_recursive(dag, False)

        summary = build([dag], registry=noop_registry)

        assert summary.status == BuildExitStatus.SUCCESS
        assert_dynamic_deps_task_complete_recursive(dag, True)

    def test_with_completion_cache(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileSystemTarget],
        noop_registry,
    ):
        """Test that previously completed tasks are not re-executed."""
        dag = get_simple_dag()

        # First build
        build([dag], registry=noop_registry)
        assert dag.complete()

        # Build again - should work without errors
        build([dag], registry=noop_registry)

        # DAG should still be complete
        assert dag.complete()


# ============================================================================
# Test: build_aio (concurrent hybrid)
# ============================================================================


class TestBuildAio:
    """Tests for build_aio() - async concurrent build."""

    @pytest.mark.asyncio
    async def test_simple_dag(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileSystemTarget],
        noop_registry,
    ):
        """Test building a simple DAG concurrently."""
        dag = get_simple_dag()
        expected_output = get_simple_dag_expected_root_output()

        summary = await build_aio([dag], registry=noop_registry)

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

        summary = await build_aio([dag], registry=noop_registry)

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
        summary = await build_aio([root], registry=noop_registry)
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
    async def test_concurrent_execution_timing(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileSystemTarget],
        noop_registry,
    ):
        """Test that independent tasks execute concurrently with detailed timing."""
        # Thread-safe execution log
        execution_log: list[tuple[str, str, float]] = []
        log_lock = threading.Lock()

        class TimedTask(AutoTask[dict]):
            name: str
            delay: float = 0.1
            deps: tuple["TimedTask", ...] = ()

            def requires(self):
                return self.deps

            async def run_aio(self):
                start = time.time()

                with log_lock:
                    execution_log.append((self.name, "start", start))

                await asyncio.sleep(self.delay)

                end = time.time()
                with log_lock:
                    execution_log.append((self.name, "end", end))

                self.output().save({"name": self.name, "start": start, "end": end})

        # Create a DAG where A and B can run in parallel, then C depends on both
        #     C
        #    / \
        #   A   B
        task_a = TimedTask(name="A", delay=0.15)
        task_b = TimedTask(name="B", delay=0.15)
        task_c = TimedTask(name="C", delay=0.05, deps=(task_a, task_b))

        await build_aio([task_c], registry=noop_registry)

        # Extract timing from log
        a_start = next(t for n, e, t in execution_log if n == "A" and e == "start")
        a_end = next(t for n, e, t in execution_log if n == "A" and e == "end")
        b_start = next(t for n, e, t in execution_log if n == "B" and e == "start")
        b_end = next(t for n, e, t in execution_log if n == "B" and e == "end")
        c_start = next(t for n, e, t in execution_log if n == "C" and e == "start")

        # A and B should have overlapping execution windows
        assert a_start < b_end and b_start < a_end, (
            f"A and B should overlap: "
            f"A=[{a_start:.3f}, {a_end:.3f}], B=[{b_start:.3f}, {b_end:.3f}]"
        )

        # C should start after both A and B end (with small tolerance)
        tolerance = 0.02
        assert c_start >= a_end - tolerance and c_start >= b_end - tolerance, (
            f"C should start after A and B: "
            f"C_start={c_start:.3f}, A_end={a_end:.3f}, B_end={b_end:.3f}"
        )

    @pytest.mark.asyncio
    async def test_fail_fast_mode(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileSystemTarget],
        noop_registry,
    ):
        """Test FAIL_FAST mode stops build on first failure."""
        failing = FailingTask()
        dependent = SyncOnlyTask(name="dependent", deps=(failing,))

        summary = await build_aio(
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

        summary = await build_aio(
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

        summary = await build_aio([task1, task2, task3], registry=noop_registry)

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

        summary = await build_aio([task], registry=noop_registry)

        assert summary.status == BuildExitStatus.SUCCESS
        assert task.output().load()["mode"] == "async"

    @pytest.mark.asyncio
    async def test_task_failure_propagates(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileSystemTarget],
        noop_registry,
    ):
        """Test that task failures are properly propagated."""

        class FailingAsyncTask(AutoTask[str]):
            async def run_aio(self):
                raise ValueError("Intentional failure")

        task = FailingAsyncTask()

        summary = await build_aio([task], registry=noop_registry)
        assert summary.error is not None
        assert isinstance(summary.error, ValueError)


# ============================================================================
# Test: HybridConcurrentTaskExecutor
# ============================================================================


class TestHybridConcurrentTaskExecutor:
    """Tests for HybridConcurrentTaskExecutor."""

    @pytest.mark.asyncio
    async def test_sync_task_runs_in_thread(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileSystemTarget],
        noop_registry,
    ):
        """Test sync tasks run in thread pool."""
        runner = HybridConcurrentTaskExecutor(
            registry=noop_registry, max_thread_workers=2
        )
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
        runner = HybridConcurrentTaskExecutor(
            registry=noop_registry, max_async_workers=2
        )
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
        runner = HybridConcurrentTaskExecutor(registry=noop_registry)
        task = FailingTask(error_message="test error")

        await runner.setup()
        try:
            result = await runner.submit(task)
            assert isinstance(result, Exception)
            assert "test error" in str(result)
        finally:
            await runner.teardown()


# ============================================================================
# Test: Async Interface Detection
# ============================================================================


class TestAsyncInterface:
    """Tests for async task interface detection and execution."""

    def test_has_custom_run_aio_detection(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileSystemTarget],
    ):
        """Test that we correctly detect custom run_aio implementations."""

        class SyncOnlyTaskLocal(AutoTask[int]):
            def run(self):
                self.output().save(42)

        class AsyncTaskLocal(AutoTask[int]):
            def run(self):
                self.output().save(42)

            async def run_aio(self):
                await asyncio.sleep(0)
                self.output().save(42)

        sync_task = SyncOnlyTaskLocal()
        async_task = AsyncTaskLocal()

        assert not _has_custom_run_aio(sync_task)
        assert _has_custom_run_aio(async_task)

    @pytest.mark.asyncio
    async def test_async_task_execution(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileSystemTarget],
    ):
        """Test that tasks with custom run_aio() execute correctly."""
        execution_log: list[tuple[str, str]] = []

        class AsyncIOTask(AutoTask[dict]):
            name: str
            delay: float = 0.1
            deps: tuple["AsyncIOTask", ...] = ()

            def requires(self):
                return self.deps

            def run(self):
                # Sync fallback - should NOT be used
                execution_log.append((self.name, "sync"))
                time.sleep(self.delay)
                self.output().save({"name": self.name, "mode": "sync"})

            async def run_aio(self):
                # Async implementation - SHOULD be used
                execution_log.append((self.name, "async"))
                await asyncio.sleep(self.delay)
                self.output().save({"name": self.name, "mode": "async"})

        task_a = AsyncIOTask(name="A", delay=0.1)
        task_b = AsyncIOTask(name="B", delay=0.1)
        task_c = AsyncIOTask(name="C", delay=0.05, deps=(task_a, task_b))

        registry = NoOpRegistry()
        await build_aio([task_c], registry=registry)

        # Verify async methods were used
        assert all(mode == "async" for _, mode in execution_log)
        assert len(execution_log) == 3

        # Verify outputs
        assert task_a.output().load()["mode"] == "async"
        assert task_b.output().load()["mode"] == "async"
        assert task_c.output().load()["mode"] == "async"

    @pytest.mark.asyncio
    async def test_concurrent_async_execution(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileSystemTarget],
    ):
        """Test that async tasks execute concurrently."""

        start_times: dict[str, float] = {}
        end_times: dict[str, float] = {}

        class ConcurrentAsyncTask(AutoTask[dict]):
            name: str
            delay: float = 0.15
            deps: tuple["ConcurrentAsyncTask", ...] = ()

            def requires(self):
                return self.deps

            def run(self):
                self.output().save({})

            async def run_aio(self):
                start_times[self.name] = time.time()
                await asyncio.sleep(self.delay)
                end_times[self.name] = time.time()
                self.output().save({"name": self.name})

        # DAG: C depends on A and B (A and B can run in parallel)
        task_a = ConcurrentAsyncTask(name="A")
        task_b = ConcurrentAsyncTask(name="B")
        task_c = ConcurrentAsyncTask(name="C", delay=0.05, deps=(task_a, task_b))

        registry = NoOpRegistry()
        await build_aio([task_c], registry=registry)

        # A and B should have overlapping execution windows (true concurrency)
        assert start_times["A"] < end_times["B"], "A should start before B ends"
        assert start_times["B"] < end_times["A"], "B should start before A ends"

        # C should start after both A and B complete
        assert start_times["C"] >= end_times["A"], "C should start after A"
        assert start_times["C"] >= end_times["B"], "C should start after B"


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

        summary = build([root], registry=noop_registry)

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

        summary = build([root], registry=noop_registry)

        assert summary.status == BuildExitStatus.SUCCESS
        assert summary.task_count.discovered == 2
        assert summary.task_count.previously_completed == 1
        assert summary.task_count.succeeded == 1


# ============================================================================
# Test: Diamond DAG Patterns (Concurrent)
# ============================================================================


class TestDiamondPatternsConcurrent:
    """Tests for diamond DAG patterns with concurrent build."""

    @pytest.mark.asyncio
    async def test_static_diamond(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileSystemTarget],
        noop_registry,
    ):
        """
        Static diamond pattern with concurrent build.
        Tests that leaf only executes once even when mid1/mid2 could race.
        """
        reset_execution_counts()

        leaf = DiamondTask(name="leaf", test_id="conc1")
        mid1 = DiamondTask(name="mid1", test_id="conc1", deps=(leaf,))
        mid2 = DiamondTask(name="mid2", test_id="conc1", deps=(leaf,))
        root = DiamondTask(name="root", test_id="conc1", deps=(mid1, mid2))

        summary = await build_aio([root], registry=noop_registry)

        assert summary.status == BuildExitStatus.SUCCESS
        assert summary.task_count.discovered == 4
        assert summary.task_count.succeeded == 4

        # Each task should execute exactly once
        assert get_execution_count("conc1", "leaf") == 1
        assert get_execution_count("conc1", "mid1") == 1
        assert get_execution_count("conc1", "mid2") == 1
        assert get_execution_count("conc1", "root") == 1

    @pytest.mark.asyncio
    async def test_dynamic_diamond(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileSystemTarget],
        noop_registry,
    ):
        """
        Diamond pattern with dynamic deps in concurrent build.
        Tests the exact pattern from get_dynamic_deps_dag().
        """
        reset_execution_counts()

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

        summary = await build_aio([parent], registry=noop_registry)

        assert summary.status == BuildExitStatus.SUCCESS

        # Each task should execute exactly once
        assert get_execution_count("dyn_conc1", "shared") == 1
        assert get_execution_count("dyn_conc1", "dyn_task") == 1
        assert get_execution_count("dyn_conc1", "parent") == 1

    @pytest.mark.asyncio
    async def test_complex_dynamic_diamond(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileSystemTarget],
        noop_registry,
    ):
        """
        Complex pattern matching get_dynamic_deps_dag() with concurrent build.
        """
        reset_execution_counts()

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

        summary = await build_aio([parent], registry=noop_registry)

        assert summary.status == BuildExitStatus.SUCCESS

        # Each task should execute exactly once
        assert get_execution_count("complex_conc", "31") == 1
        assert get_execution_count("complex_conc", "30") == 1
        assert get_execution_count("complex_conc", "20") == 1
        assert get_execution_count("complex_conc", "21") == 1
        assert get_execution_count("complex_conc", "1") == 1
        assert get_execution_count("complex_conc", "0") == 1


# ============================================================================
# Test: Process Pool Execution with Dynamic Dependencies
# ============================================================================


class TestProcessPoolDynamicDeps:
    """Tests for dynamic dependencies with process pool execution.

    These tests verify that tasks with dynamic dependencies (generators that yield)
    work correctly when executed in a separate process via ProcessPoolExecutor.

    The challenge: generators cannot be pickled, so when a task yields dynamic
    dependencies in a subprocess, we implement idempotent re-execution:
    1. Task runs in subprocess, generator is driven until it yields incomplete deps
    2. Yielded deps (TaskStruct) are returned to main process
    3. Main process builds the deps
    4. Task is re-executed from scratch in subprocess
    5. Generator is driven again - since deps are now complete, it continues past yield

    Note: These tests use local file system targets (via default_local_target_tmp_path)
    because InMemoryFileSystemTarget doesn't work with multiprocessing (each process
    has its own memory space).
    """

    @pytest.mark.asyncio
    async def test_dynamic_deps_with_process_pool(
        self,
        default_local_target_tmp_path,
        noop_registry,
    ):
        """Test that dynamic dependencies work with process pool execution."""
        # Use the standard dynamic deps DAG from testing utilities
        dag = get_dynamic_deps_dag()
        assert_dynamic_deps_task_complete_recursive(dag, False)

        # Create executor with process pool
        executor = HybridConcurrentTaskExecutor(
            registry=noop_registry,
            execution_mode_selector=DefaultExecutionModeSelector(
                sync_run_default="process"
            ),
            max_process_workers=2,
        )

        summary = await build_aio([dag], task_executor=executor)

        assert summary.status == BuildExitStatus.SUCCESS
        assert_dynamic_deps_task_complete_recursive(dag, True)

    @pytest.mark.asyncio
    async def test_dynamic_diamond_with_process_pool(
        self,
        default_local_target_tmp_path,
        noop_registry,
    ):
        """Test diamond pattern with dynamic deps in process pool.

        Tests the exact pattern from get_dynamic_deps_dag() where a task
        appears in both static deps of parent AND dynamic deps of another task.
        Build should succeed with all tasks completing exactly once.
        """
        shared = DynamicDiamondTask(name="shared", test_id="proc_dyn")
        dyn_task = DynamicDiamondTask(
            name="dyn_task",
            test_id="proc_dyn",
            dynamic_task_deps=(shared,),
        )
        parent = DynamicDiamondTask(
            name="parent",
            test_id="proc_dyn",
            static_task_deps=(dyn_task, shared),
        )

        executor = HybridConcurrentTaskExecutor(
            registry=noop_registry,
            execution_mode_selector=DefaultExecutionModeSelector(
                sync_run_default="process"
            ),
            max_process_workers=2,
        )

        summary = await build_aio([parent], task_executor=executor)

        assert summary.status == BuildExitStatus.SUCCESS

        # All tasks should be complete
        assert parent.complete()
        assert dyn_task.complete()
        assert shared.complete()
