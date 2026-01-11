"""Tests for sequential build implementation.

Tests build_sequential() and build_sequential_aio() from stardag.build._sequential.
"""

from __future__ import annotations

import json
import typing

import pytest

from stardag.build import (
    BuildExitStatus,
    FailMode,
    build_sequential,
    build_sequential_aio,
)
from stardag.registry import NoOpRegistry
from stardag.target import InMemoryFileSystemTarget
from stardag.utils.testing.simple_dag import (
    RootTask,
    RootTaskLoadedT,
    get_simple_dag,
    get_simple_dag_expected_root_output,
)

from ._helpers import (
    AsyncOnlyTask,
    DiamondTask,
    DualTask,
    DynamicDiamondTask,
    FailingAsyncTask,
    FailingTask,
    SyncOnlyTask,
    get_execution_count,
    reset_execution_counts,
)


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

    def test_simple_dag_output_serialization(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileSystemTarget],
        simple_dag: RootTask,
        simple_dag_expected_root_output: RootTaskLoadedT,
    ):
        """Test that build output is correctly serialized."""
        build_sequential([simple_dag], registry=NoOpRegistry())
        assert simple_dag.output().load() == simple_dag_expected_root_output
        expected_root_path = f"in-memory://{simple_dag._relpath}"
        assert (
            InMemoryFileSystemTarget.path_to_bytes[expected_root_path]
            == json.dumps(
                simple_dag_expected_root_output, separators=(",", ":")
            ).encode()
        )

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
# Test: Diamond DAG Patterns (Sequential)
# ============================================================================


class TestDiamondPatternsSequential:
    """Tests for diamond DAG patterns with sequential build."""

    def test_static_diamond(
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
        reset_execution_counts()

        leaf = DiamondTask(name="leaf", test_id="seq1")
        mid1 = DiamondTask(name="mid1", test_id="seq1", deps=(leaf,))
        mid2 = DiamondTask(name="mid2", test_id="seq1", deps=(leaf,))
        root = DiamondTask(name="root", test_id="seq1", deps=(mid1, mid2))

        summary = build_sequential([root], registry=noop_registry)

        assert summary.status == BuildExitStatus.SUCCESS
        assert summary.task_count.discovered == 4
        assert summary.task_count.succeeded == 4

        # Each task should execute exactly once
        assert get_execution_count("seq1", "leaf") == 1
        assert get_execution_count("seq1", "mid1") == 1
        assert get_execution_count("seq1", "mid2") == 1
        assert get_execution_count("seq1", "root") == 1

    def test_dynamic_diamond(
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
        reset_execution_counts()

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
        assert get_execution_count("dyn_seq1", "shared") == 1
        assert get_execution_count("dyn_seq1", "dyn_task") == 1
        assert get_execution_count("dyn_seq1", "parent") == 1

    def test_complex_dynamic_diamond(
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
        reset_execution_counts()

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
        assert get_execution_count("complex_seq", "31") == 1
        assert get_execution_count("complex_seq", "30") == 1
        assert get_execution_count("complex_seq", "20") == 1
        assert get_execution_count("complex_seq", "21") == 1
        assert get_execution_count("complex_seq", "1") == 1
        assert get_execution_count("complex_seq", "0") == 1
