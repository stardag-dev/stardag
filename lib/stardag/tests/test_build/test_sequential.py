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
    build,
    build_aio,
    build_sequential,
    build_sequential_aio,
)
from uuid import UUID

from stardag.artifact import Artifact, MarkdownArtifact
from stardag.registry import NoOpRegistry, registry_provider
from stardag.target import InMemoryFileTarget
from stardag.utils.testing.dynamic_deps_dag import (
    DynamicDepsTask,
    assert_dynamic_deps_task_complete_recursive,
)
from stardag.utils.testing.helper_tasks import (
    AsyncDynamicDiamondTask,
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
from stardag.utils.testing.simple_dag import (
    RootTask,
    RootTaskLoadedT,
    get_simple_dag,
    get_simple_dag_expected_root_output,
)

# ============================================================================
# Test: build_sequential
# ============================================================================


class TestBuildSequential:
    """Tests for build_sequential() - sync debugging build."""

    def test_simple_dag(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
        noop_registry,
    ):
        """Test building a simple DAG sequentially."""
        dag = get_simple_dag()
        expected_output = get_simple_dag_expected_root_output()

        summary = build_sequential([dag], registry=noop_registry)

        assert summary.status == BuildExitStatus.SUCCESS
        assert dag.complete()
        assert dag.target().load() == expected_output

    def test_resume_build_id_calls_build_resume(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
    ):
        """Sequential resume fires build_resume (not build_start).

        Mirrors the concurrent test for the sync code path. Verifies that
        the regression fix (silent reuse → explicit BUILD_RESUMED event)
        applies to build_sequential as well.
        """
        from tests.test_build.conftest import RecordingRegistry

        recording_registry = RecordingRegistry()
        existing_build_id = UUID("66666666-7777-8888-9999-aaaaaaaaaaaa")
        task = SyncOnlyTask(name="resumed-task-seq")

        summary = build_sequential(
            [task],
            registry=recording_registry,
            resume_build_id=existing_build_id,
        )
        assert summary.status == BuildExitStatus.SUCCESS
        assert summary.build_id == existing_build_id

        method_calls = [c[0] for c in recording_registry.calls]
        assert "build_resume" in method_calls, (
            f"Expected build_resume fired on resume; got {method_calls}"
        )
        assert "build_start" not in method_calls, (
            "build_start must NOT fire when resuming"
        )

    def test_uses_registry_provider_when_registry_not_passed(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
    ):
        """Test that build_sequential uses registry_provider.get() when registry=None."""
        task = SyncOnlyTask(name="provider-test")
        noop = NoOpRegistry()

        with registry_provider.override(noop):
            summary = build_sequential([task])

        assert summary.status == BuildExitStatus.SUCCESS
        assert task.target().load()["mode"] == "sync"

    def test_simple_dag_output_serialization(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
        simple_dag: RootTask,
        simple_dag_expected_root_output: RootTaskLoadedT,
    ):
        """Test that build output is correctly serialized."""
        build_sequential([simple_dag], registry=NoOpRegistry())
        assert simple_dag.target().load() == simple_dag_expected_root_output
        expected_root_path = f"in-memory://{simple_dag._relpath}"
        assert (
            InMemoryFileTarget.uri_to_bytes[expected_root_path]
            == json.dumps(
                simple_dag_expected_root_output, separators=(",", ":")
            ).encode()
        )

    def test_sync_only_task(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
        noop_registry,
    ):
        """Test sync-only tasks execute via run()."""
        task = SyncOnlyTask(name="test")

        summary = build_sequential([task], registry=noop_registry)

        assert summary.status == BuildExitStatus.SUCCESS
        assert task.target().load()["mode"] == "sync"

    def test_dual_task_uses_sync_by_default(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
        noop_registry,
    ):
        """Test dual tasks use sync by default in build_sequential."""
        task = DualTask(name="test")

        summary = build_sequential([task], registry=noop_registry)

        assert summary.status == BuildExitStatus.SUCCESS
        assert task.target().load()["mode"] == "sync"

    def test_dual_task_uses_async_when_configured(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
        noop_registry,
    ):
        """Test dual tasks can use async when configured."""
        task = DualTask(name="test")

        summary = build_sequential(
            [task], registry=noop_registry, dual_run_default="async"
        )

        assert summary.status == BuildExitStatus.SUCCESS
        assert task.target().load()["mode"] == "async"

    def test_fail_fast_mode(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
        noop_registry,
    ):
        """Test FAIL_FAST mode raises the task exception to the caller."""
        task1 = SyncOnlyTask(name="task1")
        task2 = FailingTask()
        task3 = SyncOnlyTask(name="task3", deps=(task2,))

        with pytest.raises(ValueError, match="Intentional failure"):
            build_sequential(
                [task1, task3], registry=noop_registry, fail_mode=FailMode.FAIL_FAST
            )

    def test_continue_mode(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
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
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
        noop_registry,
    ):
        """Test that already complete tasks are not re-executed."""
        task = SyncOnlyTask(name="test")
        # Pre-complete the task
        task.target().save({"name": "test", "mode": "pre-existing"})

        summary = build_sequential([task], registry=noop_registry)

        assert summary.status == BuildExitStatus.SUCCESS
        assert summary.task_count.previously_completed == 1
        assert summary.task_count.succeeded == 0
        # Output should be unchanged
        assert task.target().load()["mode"] == "pre-existing"

    def test_multiple_root_tasks(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
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
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
        noop_registry,
    ):
        """Test building a simple DAG sequentially async."""
        dag = get_simple_dag()
        expected_output = get_simple_dag_expected_root_output()

        summary = await build_sequential_aio([dag], registry=noop_registry)

        assert summary.status == BuildExitStatus.SUCCESS
        assert dag.complete()
        assert dag.target().load() == expected_output

    @pytest.mark.asyncio
    async def test_async_only_task(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
        noop_registry,
    ):
        """Test async-only tasks execute via run_aio()."""
        task = AsyncOnlyTask(name="test")

        summary = await build_sequential_aio([task], registry=noop_registry)

        assert summary.status == BuildExitStatus.SUCCESS
        assert task.target().load()["mode"] == "async"

    @pytest.mark.asyncio
    async def test_sync_task_in_thread_by_default(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
        noop_registry,
    ):
        """Test sync tasks run in thread by default in async sequential."""
        task = SyncOnlyTask(name="test")

        # With blocking mode, sync tasks run in current thread
        summary = await build_sequential_aio(
            [task], registry=noop_registry, sync_run_default="blocking"
        )

        assert summary.status == BuildExitStatus.SUCCESS
        assert task.target().load()["mode"] == "sync"

    @pytest.mark.asyncio
    async def test_fail_fast_mode(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
        noop_registry,
    ):
        """Test FAIL_FAST mode raises the task exception to the caller."""
        task1 = AsyncOnlyTask(name="task1")
        failing = FailingAsyncTask()
        task2 = AsyncOnlyTask(name="task2", deps=(failing,))

        with pytest.raises(ValueError, match="Intentional"):
            await build_sequential_aio(
                [task1, task2], registry=noop_registry, fail_mode=FailMode.FAIL_FAST
            )


# ============================================================================
# Test: Diamond DAG Patterns (Sequential)
# ============================================================================


class TestDiamondPatternsSequential:
    """Tests for diamond DAG patterns with sequential build."""

    def test_static_diamond(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
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
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
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

    def test_dynamic_deps_discovered_count(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
        noop_registry,
    ):
        """task_count.discovered should include dynamically discovered tasks.

        parent (static: [])
           |  (dynamic: [dyn_child])
        dyn_child (static: [])

        Discovery finds only parent (1). After parent runs and yields dyn_child,
        dyn_child should also be counted as discovered (total: 2).
        """
        reset_execution_counts()

        dyn_child = DynamicDiamondTask(name="dyn_child", test_id="disc_count")
        parent = DynamicDiamondTask(
            name="parent",
            test_id="disc_count",
            dynamic_task_deps=(dyn_child,),
        )

        summary = build_sequential([parent], registry=noop_registry)

        assert summary.status == BuildExitStatus.SUCCESS
        # Both parent and dyn_child should be discovered
        assert summary.task_count.discovered == 2, (
            f"Expected 2 discovered (parent + dyn_child), got {summary.task_count.discovered}"
        )
        assert summary.task_count.succeeded == 2

    @pytest.mark.asyncio
    async def test_dynamic_deps_discovered_count_aio(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
        noop_registry,
    ):
        """Async: task_count.discovered should include dynamically discovered tasks."""
        reset_execution_counts()

        dyn_child = DynamicDiamondTask(name="dyn_child_aio", test_id="disc_count_aio")
        parent = DynamicDiamondTask(
            name="parent_aio",
            test_id="disc_count_aio",
            dynamic_task_deps=(dyn_child,),
        )

        summary = await build_sequential_aio([parent], registry=noop_registry)

        assert summary.status == BuildExitStatus.SUCCESS
        assert summary.task_count.discovered == 2, (
            f"Expected 2 discovered, got {summary.task_count.discovered}"
        )
        assert summary.task_count.succeeded == 2

    def test_complex_dynamic_diamond(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
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


# ============================================================================
# Test: Dynamically-yielded deps with their own requires() chain (issue #118)
#
# Parameterized across build_sequential and build (and the aio variants) so the
# same contract is verified for both executors. Before the fix for issue #118,
# build_sequential ran yielded tasks directly without first resolving their
# requires(), while build (concurrent) did not.
#
# DynamicDepsTask enforces (via assertions in run()) that static requires are
# complete at the start of run, so these tests fail loudly on the bug.
# ============================================================================


@pytest.mark.parametrize(
    "build_fn",
    [build_sequential, build],
    ids=["sequential", "concurrent"],
)
class TestDynamicDepsWithRequiresSync:
    def test_yielded_dep_with_static_requires(
        self,
        build_fn,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
        noop_registry,
    ):
        """Reproduces issue #118.

        Orchestrator yields Middle, and Middle has Leaf as a static require.
        Build systems must resolve Middle.requires() (i.e. build Leaf) before
        running Middle.
        """
        leaf = DynamicDepsTask(value="leaf")
        middle = DynamicDepsTask(value="middle", static_deps=(leaf,))
        orchestrator = DynamicDepsTask(value="orch", dynamic_deps=(middle,))

        summary = build_fn([orchestrator], registry=noop_registry)

        assert summary.status == BuildExitStatus.SUCCESS
        assert_dynamic_deps_task_complete_recursive(orchestrator, True)

    def test_yielded_dep_with_nested_requires(
        self,
        build_fn,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
        noop_registry,
    ):
        """Yielded task has a multi-level static requires chain."""
        grand = DynamicDepsTask(value="grand")
        leaf = DynamicDepsTask(value="leaf", static_deps=(grand,))
        middle = DynamicDepsTask(value="middle", static_deps=(leaf,))
        orchestrator = DynamicDepsTask(value="orch", dynamic_deps=(middle,))

        summary = build_fn([orchestrator], registry=noop_registry)

        assert summary.status == BuildExitStatus.SUCCESS
        assert_dynamic_deps_task_complete_recursive(orchestrator, True)


@pytest.mark.parametrize(
    "build_aio_fn",
    [build_sequential_aio, build_aio],
    ids=["sequential", "concurrent"],
)
class TestDynamicDepsWithRequiresAio:
    @pytest.mark.asyncio
    async def test_yielded_dep_with_static_requires(
        self,
        build_aio_fn,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
        noop_registry,
    ):
        """Async variant of the issue #118 regression test."""
        leaf = DynamicDepsTask(value="leaf_aio")
        middle = DynamicDepsTask(value="middle_aio", static_deps=(leaf,))
        orchestrator = DynamicDepsTask(value="orch_aio", dynamic_deps=(middle,))

        summary = await build_aio_fn([orchestrator], registry=noop_registry)

        assert summary.status == BuildExitStatus.SUCCESS
        assert_dynamic_deps_task_complete_recursive(orchestrator, True)


class TestDynamicDepsWithRequiresRegistryBookkeeping:
    """Registry bookkeeping for static deps newly discovered via a dynamic-dep chain.

    When a task is yielded dynamically and its ``requires()`` references a task
    that was already complete on disk before the build started, that static
    dep is only discovered at runtime. It should still get a ``task_register``
    + ``task_complete`` event so it shows up in the build's task list — mirrors
    the existing handling for dynamically yielded previously-complete tasks.
    """

    def test_static_dep_of_dynamic_dep_pre_complete(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
    ):
        # Pre-build leaf so it's already complete on disk.
        leaf = DynamicDepsTask(value="pre_leaf")
        build_sequential([leaf], registry=NoOpRegistry())
        assert leaf.complete()

        middle = DynamicDepsTask(value="pre_middle", static_deps=(leaf,))
        orchestrator = DynamicDepsTask(value="pre_orch", dynamic_deps=(middle,))

        tracking = TrackingRegistry()
        summary = build_sequential([orchestrator], registry=tracking)
        assert summary.status == BuildExitStatus.SUCCESS

        leaf_calls = tracking.calls_for(leaf.id)
        assert "task_register" in leaf_calls, (
            f"Pre-complete static dep of a dynamic dep was not registered; "
            f"calls: {leaf_calls}"
        )
        assert "task_complete" in leaf_calls

    @pytest.mark.asyncio
    async def test_static_dep_of_dynamic_dep_pre_complete_aio(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
    ):
        leaf = DynamicDepsTask(value="pre_leaf_aio")
        await build_sequential_aio([leaf], registry=NoOpRegistry())
        assert leaf.complete()

        middle = DynamicDepsTask(value="pre_middle_aio", static_deps=(leaf,))
        orchestrator = DynamicDepsTask(value="pre_orch_aio", dynamic_deps=(middle,))

        tracking = TrackingRegistry()
        summary = await build_sequential_aio([orchestrator], registry=tracking)
        assert summary.status == BuildExitStatus.SUCCESS

        leaf_calls = tracking.calls_for(leaf.id)
        assert "task_register" in leaf_calls, (
            f"Pre-complete static dep of a dynamic dep was not registered; "
            f"calls: {leaf_calls}"
        )
        assert "task_complete" in leaf_calls


# ============================================================================
# Test: Async-generator dynamic deps (yield from `async def run_aio`)
#
# Parameterized across build_sequential_aio and build_aio so the async-gen
# handling is exercised for both executors.
# ============================================================================


@pytest.mark.parametrize(
    "build_aio_fn",
    [build_sequential_aio, build_aio],
    ids=["sequential", "concurrent"],
)
class TestAsyncDynamicDeps:
    @pytest.mark.asyncio
    async def test_async_dynamic_diamond(
        self,
        build_aio_fn,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
        noop_registry,
    ):
        """Async dynamic diamond pattern.

        parent (static: [dyn_task, shared])
           |
        dyn_task (dynamic: [shared])
           |
        shared (appears in both paths)
        """
        reset_execution_counts()
        test_id = f"async_dyn_{build_aio_fn.__name__}"

        shared = AsyncDynamicDiamondTask(name="shared", test_id=test_id)
        dyn_task = AsyncDynamicDiamondTask(
            name="dyn_task", test_id=test_id, dynamic_task_deps=(shared,)
        )
        parent = AsyncDynamicDiamondTask(
            name="parent", test_id=test_id, static_task_deps=(dyn_task, shared)
        )

        summary = await build_aio_fn([parent], registry=noop_registry)

        assert summary.status == BuildExitStatus.SUCCESS
        assert get_execution_count(test_id, "shared") == 1
        assert get_execution_count(test_id, "dyn_task") == 1
        assert get_execution_count(test_id, "parent") == 1

    @pytest.mark.asyncio
    async def test_async_yielded_dep_with_static_requires(
        self,
        build_aio_fn,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
        noop_registry,
    ):
        """Async generator: yielded dep's requires() chain must be resolved.

        Same contract as issue #118 but exercised through ``async def run_aio: yield``.
        """
        reset_execution_counts()
        test_id = f"async_req_{build_aio_fn.__name__}"

        leaf = AsyncDynamicDiamondTask(name="leaf", test_id=test_id)
        middle = AsyncDynamicDiamondTask(
            name="middle", test_id=test_id, static_task_deps=(leaf,)
        )
        orch = AsyncDynamicDiamondTask(
            name="orch", test_id=test_id, dynamic_task_deps=(middle,)
        )

        summary = await build_aio_fn([orch], registry=noop_registry)

        assert summary.status == BuildExitStatus.SUCCESS
        assert leaf.complete()
        assert middle.complete()
        assert orch.complete()
        assert get_execution_count(test_id, "leaf") == 1
        assert get_execution_count(test_id, "middle") == 1
        assert get_execution_count(test_id, "orch") == 1


# ============================================================================
# Test: Registry communication for previously-completed tasks
# ============================================================================


class TrackingRegistry(NoOpRegistry):
    """A NoOpRegistry that records task lifecycle calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, UUID]] = []
        self.dynamic_dep_edges: list[tuple[UUID, UUID]] = []

    def task_register(self, build_id: UUID, task) -> None:
        self.calls.append(("task_register", task.id))

    def task_start(self, build_id: UUID, task) -> None:
        self.calls.append(("task_start", task.id))

    def task_complete(self, build_id: UUID, task) -> None:
        self.calls.append(("task_complete", task.id))

    def task_fail(self, build_id: UUID, task, error_message=None) -> None:
        self.calls.append(("task_fail", task.id))

    def task_add_dependencies(
        self, build_id: UUID, task, upstream_tasks, is_dynamic=True
    ) -> None:
        for upstream in upstream_tasks:
            self.dynamic_dep_edges.append((upstream.id, task.id))

    def calls_for(self, task_id: UUID) -> list[str]:
        return [method for method, tid in self.calls if tid == task_id]


class TestPreviouslyCompletedRegistryCommunication:
    """Test that previously-completed tasks are properly registered AND completed."""

    def test_previously_completed_task_gets_task_complete(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
    ):
        """Previously-completed tasks should get both task_register and task_complete."""
        tracking = TrackingRegistry()

        task = SyncOnlyTask(name="pre_complete")
        # First build: task gets executed
        build_sequential([task], registry=tracking)
        assert task.complete()

        # Reset tracking
        tracking.calls.clear()

        # Second build: task is already complete, should be registered + completed
        summary = build_sequential([task], registry=tracking)

        assert summary.status == BuildExitStatus.SUCCESS
        assert summary.task_count.previously_completed == 1
        assert summary.task_count.succeeded == 0

        calls = tracking.calls_for(task.id)
        assert "task_register" in calls
        assert "task_complete" in calls
        # Should NOT have task_start (it was not executed)
        assert "task_start" not in calls

    @pytest.mark.asyncio
    async def test_previously_completed_task_gets_task_complete_aio(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
    ):
        """Async: previously-completed tasks get both task_register and task_complete."""
        tracking = TrackingRegistry()

        task = SyncOnlyTask(name="pre_complete_aio")
        # First build: task gets executed
        await build_sequential_aio([task], registry=tracking)
        assert task.complete()

        # Reset tracking
        tracking.calls.clear()

        # Second build: task is already complete
        summary = await build_sequential_aio([task], registry=tracking)

        assert summary.status == BuildExitStatus.SUCCESS
        assert summary.task_count.previously_completed == 1

        calls = tracking.calls_for(task.id)
        assert "task_register" in calls
        assert "task_complete" in calls
        assert "task_start" not in calls

    def test_mixed_completed_and_new_tasks(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
    ):
        """Build with mix of previously-completed deps and new tasks."""
        tracking = TrackingRegistry()

        dep = SyncOnlyTask(name="dep_mixed")
        # Build the dep first
        build_sequential([dep], registry=tracking)
        assert dep.complete()
        tracking.calls.clear()

        # Now build a new task that depends on the completed dep
        parent = SyncOnlyTask(name="parent_mixed", deps=(dep,))
        summary = build_sequential([parent], registry=tracking)

        assert summary.status == BuildExitStatus.SUCCESS

        # dep should be registered + completed (previously complete)
        dep_calls = tracking.calls_for(dep.id)
        assert "task_register" in dep_calls
        assert "task_complete" in dep_calls
        assert "task_start" not in dep_calls

        # parent should go through normal lifecycle
        parent_calls = tracking.calls_for(parent.id)
        assert "task_start" in parent_calls
        assert "task_complete" in parent_calls


# ============================================================================
# Test: Registry errors don't mask task errors
# ============================================================================


class FailingOnTaskFailRegistry(NoOpRegistry):
    """A registry that raises when task_fail is called."""

    def task_fail(self, build_id, task, error_message=None):
        raise ConnectionError("Registry unavailable")


class TestRegistryErrorResilience:
    """Test that registry errors in task_fail don't mask the original task error."""

    def test_registry_task_fail_error_does_not_mask_task_error(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
    ):
        """If registry.task_fail raises, the original task error still propagates."""
        registry = FailingOnTaskFailRegistry()
        task = FailingTask(error_message="task broke")

        # FAIL_FAST with warn mode: should raise the original ValueError, not ConnectionError
        with pytest.raises(ValueError, match="task broke"):
            build_sequential(
                [task],
                registry=registry,
                fail_mode=FailMode.FAIL_FAST,
                on_registry_failure="warn",
            )

    def test_registry_task_fail_error_does_not_mask_task_error_continue(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
    ):
        """In CONTINUE mode, registry.task_fail error is swallowed gracefully."""
        registry = FailingOnTaskFailRegistry()
        task = FailingTask(error_message="task broke")

        # CONTINUE mode with warn: should return a summary, not crash
        summary = build_sequential(
            [task],
            registry=registry,
            fail_mode=FailMode.CONTINUE,
            on_registry_failure="warn",
        )

        assert summary.status == BuildExitStatus.FAILURE
        assert summary.task_count.failed == 1

    @pytest.mark.asyncio
    async def test_registry_task_fail_error_does_not_mask_task_error_aio(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
    ):
        """Async: registry.task_fail error doesn't mask the original task error."""
        registry = FailingOnTaskFailRegistry()
        task = FailingAsyncTask()

        with pytest.raises(ValueError, match="Intentional"):
            await build_sequential_aio(
                [task],
                registry=registry,
                fail_mode=FailMode.FAIL_FAST,
                on_registry_failure="warn",
            )


# ============================================================================
# Test: Deadlock detection in sequential builds
# ============================================================================


class TestSequentialDeadlockDetection:
    """Test that sequential builds detect when tasks can't proceed."""

    def test_continue_mode_reports_all_failures(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
        noop_registry,
    ):
        """In CONTINUE mode, build should complete and report the error when
        no more tasks can run due to failed dependencies."""
        # Create: task_a (fails), task_b depends on task_a, task_c is independent
        task_a = FailingTask(error_message="task_a broke")
        task_b = SyncOnlyTask(name="depends_on_failing", deps=(task_a,))
        task_c = SyncOnlyTask(name="independent")

        summary = build_sequential(
            [task_b, task_c],
            registry=noop_registry,
            fail_mode=FailMode.CONTINUE,
        )

        assert summary.status == BuildExitStatus.FAILURE
        assert summary.task_count.failed >= 1
        # task_c should still succeed
        assert task_c.complete()
        # task_b should not have run (dep failed)
        assert not task_b.complete()
        # pending should reflect task_b being blocked
        assert summary.task_count.pending == 1

    @pytest.mark.asyncio
    async def test_continue_mode_reports_all_failures_aio(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
        noop_registry,
    ):
        """Async: CONTINUE mode should complete and report blocked tasks."""
        task_a = FailingTask(error_message="task_a broke")
        task_b = SyncOnlyTask(name="depends_on_failing_aio", deps=(task_a,))
        task_c = SyncOnlyTask(name="independent_aio")

        summary = await build_sequential_aio(
            [task_b, task_c],
            registry=noop_registry,
            fail_mode=FailMode.CONTINUE,
        )

        assert summary.status == BuildExitStatus.FAILURE
        assert summary.task_count.failed >= 1
        assert task_c.complete()
        assert not task_b.complete()
        assert summary.task_count.pending == 1

    def test_deadlock_detection_with_circular_deps(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
        noop_registry,
    ):
        """Circular dependency creates a true deadlock - should raise RuntimeError.

        Task A depends on Task B and Task B depends on Task A. Both are discovered
        (discover() guards against revisiting), but neither can ever become ready.
        """
        from stardag import Task, auto_namespace
        from typing import Any

        auto_namespace(__name__)

        # We can't create Pydantic circular refs directly, but we can use a
        # class whose requires() creates a mutual dependency at runtime.
        # The trick: create a shared list that we mutate after both tasks exist.
        dep_holder_a: list[Task] = []
        dep_holder_b: list[Task] = []

        class CircularDepTask(Task[dict[str, Any]]):
            name: str
            _dep_holder: list[Task] = []

            def requires(self):
                if self.name == "circ_a":
                    return tuple(dep_holder_a)
                return tuple(dep_holder_b)

            def run(self):
                self._save({"name": self.name})

        task_a = CircularDepTask(name="circ_a")
        task_b = CircularDepTask(name="circ_b")

        # Set up circular deps
        dep_holder_a.append(task_b)
        dep_holder_b.append(task_a)

        # Both tasks discovered, but neither can proceed
        with pytest.raises(RuntimeError, match="[Dd]eadlock"):
            build_sequential([task_a], registry=noop_registry)

    @pytest.mark.asyncio
    async def test_deadlock_detection_with_circular_deps_aio(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
        noop_registry,
    ):
        """Async: circular deps should raise RuntimeError for deadlock."""
        from stardag import Task, auto_namespace
        from typing import Any

        auto_namespace(__name__)

        dep_holder_a: list[Task] = []
        dep_holder_b: list[Task] = []

        class CircularDepTaskAio(Task[dict[str, Any]]):
            name: str

            def requires(self):
                if self.name == "circ_a_aio":
                    return tuple(dep_holder_a)
                return tuple(dep_holder_b)

            def run(self):
                self._save({"name": self.name})

        task_a = CircularDepTaskAio(name="circ_a_aio")
        task_b = CircularDepTaskAio(name="circ_b_aio")

        dep_holder_a.append(task_b)
        dep_holder_b.append(task_a)

        with pytest.raises(RuntimeError, match="[Dd]eadlock"):
            await build_sequential_aio([task_a], registry=noop_registry)


# ============================================================================
# Test: on_registry_failure parameter
# ============================================================================


class FailOnTaskCompleteRegistry(NoOpRegistry):
    """Registry that fails on task_complete."""

    def task_register(self, build_id: UUID, task) -> None:
        pass

    def task_complete(self, build_id: UUID, task) -> None:
        raise ConnectionError("Registry unavailable for task_complete")


class TestOnRegistryFailure:
    """Test the on_registry_failure parameter."""

    def test_warn_mode_logs_warning_on_registry_failure(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
    ):
        """Default 'warn' mode should log and continue."""
        registry = FailOnTaskCompleteRegistry()
        task = SyncOnlyTask(name="reg_fail_warn")
        # Pre-complete so it triggers the previously-completed registration path
        task.target().save({"name": "reg_fail_warn", "mode": "pre-existing"})

        # Should NOT raise
        summary = build_sequential(
            [task], registry=registry, on_registry_failure="warn"
        )
        assert summary.status == BuildExitStatus.SUCCESS

    def test_raise_mode_propagates_registry_failure(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
    ):
        """'raise' mode should propagate registry errors."""
        registry = FailOnTaskCompleteRegistry()
        task = SyncOnlyTask(name="reg_fail_raise")
        # Pre-complete so it triggers the previously-completed registration path
        task.target().save({"name": "reg_fail_raise", "mode": "pre-existing"})

        with pytest.raises(ConnectionError, match="Registry unavailable"):
            build_sequential([task], registry=registry, on_registry_failure="raise")

    @pytest.mark.asyncio
    async def test_raise_mode_propagates_registry_failure_aio(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
    ):
        """Async: 'raise' mode should propagate registry errors."""
        registry = FailOnTaskCompleteRegistry()
        task = SyncOnlyTask(name="reg_fail_raise_aio")
        task.target().save({"name": "reg_fail_raise_aio", "mode": "pre-existing"})

        with pytest.raises(ConnectionError, match="Registry unavailable"):
            await build_sequential_aio(
                [task], registry=registry, on_registry_failure="raise"
            )


class ArtifactTrackingRegistry(NoOpRegistry):
    """A registry that records artifact uploads."""

    def __init__(self) -> None:
        self.uploaded_artifacts: list[tuple[UUID, typing.Sequence]] = []

    def task_register(self, build_id: UUID, task) -> None:
        pass

    def task_upload_artifacts(self, build_id: UUID, task, artifacts) -> None:
        self.uploaded_artifacts.append((task.id, artifacts))

    async def task_upload_artifacts_aio(self, build_id: UUID, task, artifacts) -> None:
        self.uploaded_artifacts.append((task.id, artifacts))


class TestAsyncArtifactCollection:
    """Test that artifacts_aio is properly awaited in async builds."""

    @pytest.mark.asyncio
    async def test_artifacts_aio_is_awaited(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
    ):
        """Verify that artifacts_aio() is awaited (is async) in async sequential build."""
        from stardag import Task, auto_namespace
        from typing import Any

        auto_namespace(__name__)

        class TaskWithArtifacts(Task[dict[str, Any]]):
            name: str

            def run(self):
                self._save({"name": self.name})

            async def artifacts_aio(self) -> list[Artifact]:
                return [
                    MarkdownArtifact(name="report", body=f"# Report for {self.name}")
                ]

        registry = ArtifactTrackingRegistry()
        task = TaskWithArtifacts(name="artifact_test")

        summary = await build_sequential_aio([task], registry=registry)

        assert summary.status == BuildExitStatus.SUCCESS
        assert len(registry.uploaded_artifacts) == 1
        task_id, artifacts = registry.uploaded_artifacts[0]
        assert task_id == task.id
        assert len(artifacts) == 1
        assert artifacts[0].name == "report"


# ============================================================================
# Test: Dynamic dep edges reach the registry
# ============================================================================


@pytest.mark.parametrize(
    "build_fn",
    [build_sequential, build],
    ids=["sequential", "concurrent"],
)
class TestDynamicDepEdgesRegistrySync:
    """When a task yields a dep, the edge parent -> yielded_dep must be
    reported to the registry so the DAG view can render the upstream
    relationship. Previously only the yielded dep's own ``requires()``
    chain reached the registry.
    """

    def test_yielded_dep_registers_edge(
        self,
        build_fn,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
    ):
        reset_execution_counts()
        tracking = TrackingRegistry()

        test_id = f"edges_{build_fn.__name__}"
        dyn = DynamicDiamondTask(name="dyn", test_id=test_id)
        parent = DynamicDiamondTask(
            name="parent", test_id=test_id, dynamic_task_deps=(dyn,)
        )

        summary = build_fn([parent], registry=tracking)

        assert summary.status == BuildExitStatus.SUCCESS
        # Edge dyn -> parent (upstream=dyn, downstream=parent), stored as
        # (upstream_id, downstream_id) in TrackingRegistry.dynamic_dep_edges.
        assert (dyn.id, parent.id) in tracking.dynamic_dep_edges, (
            f"Dynamic edge not reported; saw: {tracking.dynamic_dep_edges}"
        )


@pytest.mark.parametrize(
    "build_aio_fn",
    [build_sequential_aio, build_aio],
    ids=["sequential", "concurrent"],
)
class TestDynamicDepEdgesRegistryAio:
    @pytest.mark.asyncio
    async def test_yielded_dep_registers_edge(
        self,
        build_aio_fn,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
    ):
        reset_execution_counts()
        tracking = TrackingRegistry()

        test_id = f"edges_aio_{build_aio_fn.__name__}"
        dyn = AsyncDynamicDiamondTask(name="dyn", test_id=test_id)
        parent = AsyncDynamicDiamondTask(
            name="parent", test_id=test_id, dynamic_task_deps=(dyn,)
        )

        summary = await build_aio_fn([parent], registry=tracking)

        assert summary.status == BuildExitStatus.SUCCESS
        assert (dyn.id, parent.id) in tracking.dynamic_dep_edges, (
            f"Dynamic edge not reported; saw: {tracking.dynamic_dep_edges}"
        )


# ============================================================================
# Test: Discover-time task registration
#
# All discovered tasks must be registered with the registry before any task
# starts executing (so the full DAG is visible in the UI immediately, not
# leaves-first as tasks become runnable). Sequential build must register in
# deterministic DFS order from the roots.
# ============================================================================


class OrderedTrackingRegistry(NoOpRegistry):
    """A NoOpRegistry that records the order of every lifecycle call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, UUID]] = []

    def task_register(self, build_id: UUID, task) -> None:
        self.calls.append(("task_register", task.id))

    def task_start(self, build_id: UUID, task) -> None:
        self.calls.append(("task_start", task.id))

    def task_complete(self, build_id: UUID, task) -> None:
        self.calls.append(("task_complete", task.id))

    def task_fail(self, build_id: UUID, task, error_message=None) -> None:
        self.calls.append(("task_fail", task.id))

    async def task_register_aio(self, build_id: UUID, task) -> None:
        self.calls.append(("task_register", task.id))

    async def task_start_aio(self, build_id: UUID, task) -> None:
        self.calls.append(("task_start", task.id))

    async def task_complete_aio(self, build_id: UUID, task) -> None:
        self.calls.append(("task_complete", task.id))

    async def task_fail_aio(self, build_id: UUID, task, error_message=None) -> None:
        self.calls.append(("task_fail", task.id))


class TestDiscoverTimeRegistrationSequential:
    """Sequential build registers all discovered tasks before any task starts."""

    def test_all_tasks_registered_before_any_start(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
    ):
        """Every discovered task should be registered before the first task_start."""
        tracking = OrderedTrackingRegistry()

        leaf_a = SyncOnlyTask(name="reg_leaf_a")
        leaf_b = SyncOnlyTask(name="reg_leaf_b")
        root = SyncOnlyTask(name="reg_root", deps=(leaf_a, leaf_b))

        summary = build_sequential([root], registry=tracking)
        assert summary.status == BuildExitStatus.SUCCESS

        first_start_idx = next(
            i for i, (m, _) in enumerate(tracking.calls) if m == "task_start"
        )
        prefix = tracking.calls[:first_start_idx]
        registered_before_start = {tid for m, tid in prefix if m == "task_register"}

        assert {leaf_a.id, leaf_b.id, root.id}.issubset(registered_before_start), (
            f"Expected all 3 tasks registered before first task_start; "
            f"prefix calls: {prefix}"
        )

    def test_each_task_registered_exactly_once(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
    ):
        """task_register must be called exactly once per task per build (no
        duplicate-from-task_start_aio noise)."""
        tracking = OrderedTrackingRegistry()

        leaf = SyncOnlyTask(name="once_leaf")
        root = SyncOnlyTask(name="once_root", deps=(leaf,))

        build_sequential([root], registry=tracking)

        for task in (leaf, root):
            register_calls = [
                c for c in tracking.calls if c == ("task_register", task.id)
            ]
            assert len(register_calls) == 1, (
                f"Expected exactly 1 task_register for {task.id}, got {register_calls}"
            )

    def test_registration_order_is_post_order_dfs(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
    ):
        """Sequential build registers in post-order DFS: leaves first, then
        their parents. This ensures every dep already exists in the registry
        by the time its parent is registered, so the API never has to
        phantom-create a dep row.
        """
        tracking = OrderedTrackingRegistry()

        leaf_a = SyncOnlyTask(name="dfs_leaf_a")
        leaf_b = SyncOnlyTask(name="dfs_leaf_b")
        root = SyncOnlyTask(name="dfs_root", deps=(leaf_a, leaf_b))

        build_sequential([root], registry=tracking)

        register_order = [tid for m, tid in tracking.calls if m == "task_register"]
        # Post-order DFS: leaf_a, leaf_b, root.
        assert register_order == [leaf_a.id, leaf_b.id, root.id], (
            f"Expected post-order DFS (leaves before parents); got {register_order}"
        )

    @pytest.mark.asyncio
    async def test_all_tasks_registered_before_start_aio(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
    ):
        tracking = OrderedTrackingRegistry()

        leaf = SyncOnlyTask(name="aio_leaf")
        root = SyncOnlyTask(name="aio_root", deps=(leaf,))

        await build_sequential_aio([root], registry=tracking)

        first_start_idx = next(
            i for i, (m, _) in enumerate(tracking.calls) if m == "task_start"
        )
        prefix = tracking.calls[:first_start_idx]
        registered_before_start = {tid for m, tid in prefix if m == "task_register"}
        assert {leaf.id, root.id}.issubset(registered_before_start)

    @pytest.mark.asyncio
    async def test_each_task_registered_exactly_once_aio(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
    ):
        tracking = OrderedTrackingRegistry()
        leaf = SyncOnlyTask(name="once_leaf_aio")
        root = SyncOnlyTask(name="once_root_aio", deps=(leaf,))
        await build_sequential_aio([root], registry=tracking)
        for task in (leaf, root):
            register_calls = [
                c for c in tracking.calls if c == ("task_register", task.id)
            ]
            assert len(register_calls) == 1

    def test_dynamic_deps_registered_when_discovered(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
    ):
        """Tasks first discovered through a dynamic-deps yield must also be
        registered (and only once)."""
        tracking = OrderedTrackingRegistry()

        dyn = DynamicDepsTask(value="dyn_reg")
        orchestrator = DynamicDepsTask(value="orch_reg", dynamic_deps=(dyn,))

        summary = build_sequential([orchestrator], registry=tracking)
        assert summary.status == BuildExitStatus.SUCCESS

        for task in (orchestrator, dyn):
            register_calls = [
                c for c in tracking.calls if c == ("task_register", task.id)
            ]
            assert len(register_calls) == 1, (
                f"Expected exactly 1 task_register for {task.id}, got {register_calls}"
            )


@pytest.mark.parametrize(
    "build_aio_fn",
    [build_aio, build_sequential_aio],
    ids=["concurrent", "sequential"],
)
class TestDiscoverTimeRegistrationAio:
    """Both async builds register every discovered task once, before any
    task starts. Concurrent build doesn't guarantee a deterministic order
    between siblings, but parents-before-deps holds and start-after-register
    holds for every task."""

    @pytest.mark.asyncio
    async def test_all_registered_before_any_start(
        self,
        build_aio_fn,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
    ):
        tracking = OrderedTrackingRegistry()
        leaf_a = SyncOnlyTask(name=f"common_a_{build_aio_fn.__name__}")
        leaf_b = SyncOnlyTask(name=f"common_b_{build_aio_fn.__name__}")
        root = SyncOnlyTask(
            name=f"common_root_{build_aio_fn.__name__}", deps=(leaf_a, leaf_b)
        )
        await build_aio_fn([root], registry=tracking)

        first_start_idx = next(
            i for i, (m, _) in enumerate(tracking.calls) if m == "task_start"
        )
        prefix = tracking.calls[:first_start_idx]
        registered_before_start = {tid for m, tid in prefix if m == "task_register"}
        assert {leaf_a.id, leaf_b.id, root.id}.issubset(registered_before_start)

    @pytest.mark.asyncio
    async def test_each_task_registered_exactly_once(
        self,
        build_aio_fn,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
    ):
        tracking = OrderedTrackingRegistry()
        leaf = SyncOnlyTask(name=f"once_leaf_{build_aio_fn.__name__}")
        root = SyncOnlyTask(name=f"once_root_{build_aio_fn.__name__}", deps=(leaf,))
        await build_aio_fn([root], registry=tracking)
        for task in (leaf, root):
            register_calls = [
                c for c in tracking.calls if c == ("task_register", task.id)
            ]
            assert len(register_calls) == 1, (
                f"Expected exactly 1 task_register for {task.id}, got {register_calls}"
            )

    @pytest.mark.asyncio
    async def test_previously_completed_registered_in_discover(
        self,
        build_aio_fn,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
    ):
        """Previously-completed tasks should also be registered exactly once
        and never get task_start."""
        # Pre-build to make the task previously-complete.
        pre = SyncOnlyTask(name=f"pre_{build_aio_fn.__name__}")
        await build_aio_fn([pre], registry=NoOpRegistry())
        assert pre.complete()

        tracking = OrderedTrackingRegistry()
        await build_aio_fn([pre], registry=tracking)

        register_calls = [c for c in tracking.calls if c == ("task_register", pre.id)]
        complete_calls = [c for c in tracking.calls if c == ("task_complete", pre.id)]
        start_calls = [c for c in tracking.calls if c == ("task_start", pre.id)]
        assert len(register_calls) == 1
        assert len(complete_calls) == 1
        assert start_calls == []


# ============================================================================
# Test: Bulk register
#
# Build engine should emit one bulk-register call per discover walk
# instead of N per-task calls. Verifies the registration order within the
# batch is post-order (deps before parents), so the API never has to
# phantom-create a row.
# ============================================================================


class BulkTrackingRegistry(NoOpRegistry):
    """A registry that records bulk_register batches *and* per-task calls."""

    def __init__(self) -> None:
        self.bulk_batches: list[list[UUID]] = []
        self.per_task_register_calls: list[UUID] = []
        self.task_complete_calls: list[UUID] = []
        self.task_start_calls: list[UUID] = []

    def task_register(self, build_id: UUID, task) -> None:
        self.per_task_register_calls.append(task.id)

    async def task_register_aio(self, build_id: UUID, task) -> None:
        self.per_task_register_calls.append(task.id)

    def task_register_bulk(self, build_id: UUID, tasks) -> None:
        self.bulk_batches.append([t.id for t in tasks])

    async def task_register_bulk_aio(self, build_id: UUID, tasks) -> None:
        self.bulk_batches.append([t.id for t in tasks])

    def task_start(self, build_id: UUID, task) -> None:
        self.task_start_calls.append(task.id)

    async def task_start_aio(self, build_id: UUID, task) -> None:
        self.task_start_calls.append(task.id)

    def task_complete(self, build_id: UUID, task) -> None:
        self.task_complete_calls.append(task.id)

    async def task_complete_aio(self, build_id: UUID, task) -> None:
        self.task_complete_calls.append(task.id)


class TestBulkRegister:
    """Verify the build engine batches discover-time registrations."""

    def test_sequential_emits_one_bulk_call_for_static_dag(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
    ):
        registry = BulkTrackingRegistry()

        leaf_a = SyncOnlyTask(name="bulk_seq_leaf_a")
        leaf_b = SyncOnlyTask(name="bulk_seq_leaf_b")
        root = SyncOnlyTask(name="bulk_seq_root", deps=(leaf_a, leaf_b))

        build_sequential([root], registry=registry)

        # Exactly one bulk batch with all three tasks. No per-task
        # task_register calls (would mean the bulk path fell through).
        assert len(registry.bulk_batches) == 1, registry.bulk_batches
        assert set(registry.bulk_batches[0]) == {leaf_a.id, leaf_b.id, root.id}
        assert registry.per_task_register_calls == []

    def test_sequential_bulk_order_is_post_order(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
    ):
        registry = BulkTrackingRegistry()

        leaf_a = SyncOnlyTask(name="po_leaf_a")
        leaf_b = SyncOnlyTask(name="po_leaf_b")
        root = SyncOnlyTask(name="po_root", deps=(leaf_a, leaf_b))

        build_sequential([root], registry=registry)

        order = registry.bulk_batches[0]
        # Leaves come before the root. Within siblings the SDK preserves
        # the order returned by ``flatten_task_struct(requires())``.
        assert order == [leaf_a.id, leaf_b.id, root.id], (
            f"Expected post-order DFS, got {order}"
        )

    @pytest.mark.parametrize(
        "build_aio_fn",
        [build_aio, build_sequential_aio],
        ids=["concurrent", "sequential"],
    )
    @pytest.mark.asyncio
    async def test_aio_emits_one_bulk_call_for_static_dag(
        self,
        build_aio_fn,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
    ):
        registry = BulkTrackingRegistry()
        leaf_a = SyncOnlyTask(name=f"bulk_aio_la_{build_aio_fn.__name__}")
        leaf_b = SyncOnlyTask(name=f"bulk_aio_lb_{build_aio_fn.__name__}")
        root = SyncOnlyTask(
            name=f"bulk_aio_root_{build_aio_fn.__name__}", deps=(leaf_a, leaf_b)
        )

        await build_aio_fn([root], registry=registry)

        # One bulk call covering the whole DAG. The concurrent build may
        # interleave sibling subtrees but each subtree is post-order, so
        # parents always come after their deps within the batch.
        assert len(registry.bulk_batches) == 1
        batch = registry.bulk_batches[0]
        assert set(batch) == {leaf_a.id, leaf_b.id, root.id}
        # Root comes after both leaves regardless of sibling interleaving.
        leaf_indices = [batch.index(leaf_a.id), batch.index(leaf_b.id)]
        assert batch.index(root.id) > max(leaf_indices)
        assert registry.per_task_register_calls == []

    def test_dynamic_deps_trigger_separate_bulk_call(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
    ):
        """Each dynamic-deps yield triggers its own bulk-register call so
        the new tasks are registered before the edge is recorded."""
        registry = BulkTrackingRegistry()

        dyn = DynamicDepsTask(value="dyn_bulk")
        orchestrator = DynamicDepsTask(value="orch_bulk", dynamic_deps=(dyn,))

        build_sequential([orchestrator], registry=registry)

        # First batch: orchestrator (root has no static deps so it's the
        # only thing in the initial walk). Second batch: dyn (yielded at
        # runtime). At least 2 separate bulk calls.
        assert len(registry.bulk_batches) >= 2, registry.bulk_batches
        first_batch_ids = set(registry.bulk_batches[0])
        assert orchestrator.id in first_batch_ids
        # dyn must show up in some later batch.
        dyn_in_later_batch = any(dyn.id in batch for batch in registry.bulk_batches[1:])
        assert dyn_in_later_batch, (
            f"dyn task not found in any post-initial batch: {registry.bulk_batches}"
        )


class TestConcurrentDiscoverRaceFreedom:
    """The concurrent discover walk used a fast-path
    ``if task.id in task_states: return`` that could let a sibling
    discoverer for a shared dep return *before* the original discoverer
    appended that dep to ``pending_registrations`` — letting a parent
    append ahead of its dep and re-introducing phantom-creation. Fixed
    by waiting on a per-task ``discover_done`` event in the fast-path.

    A diamond DAG (parent → mid_a, mid_b; mid_a, mid_b → leaf) exercises
    this: both mid_a and mid_b race on ``discover(leaf)``.
    """

    @pytest.mark.asyncio
    async def test_diamond_dag_orders_shared_dep_before_all_parents(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
    ):
        registry = BulkTrackingRegistry()

        leaf = SyncOnlyTask(name="diamond_leaf")
        mid_a = SyncOnlyTask(name="diamond_mid_a", deps=(leaf,))
        mid_b = SyncOnlyTask(name="diamond_mid_b", deps=(leaf,))
        parent = SyncOnlyTask(name="diamond_parent", deps=(mid_a, mid_b))

        await build_aio([parent], registry=registry)

        assert len(registry.bulk_batches) == 1
        order = registry.bulk_batches[0]
        positions = {tid: i for i, tid in enumerate(order)}

        # Shared dep must appear before *both* its parents, even when
        # they're discovered concurrently and one of them hits the
        # fast-path on the second discover(leaf) call.
        assert positions[leaf.id] < positions[mid_a.id]
        assert positions[leaf.id] < positions[mid_b.id]
        # Parent comes last.
        assert positions[parent.id] == len(order) - 1


class TestBulkRegisterChunking:
    """Discovery batches over the API cap must be chunked into
    <=cap-sized bulk calls — otherwise the server 400s and we silently
    fall back to per-task registration in warn mode (defeating the
    bulk-register optimisation).
    """

    def test_sequential_chunks_large_batch(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
        monkeypatch,
    ):
        """Force chunking by lowering the chunk constant to 3, build a
        DAG of 7 tasks, assert 3 chunked bulk calls of sizes 3, 3, 1."""
        from stardag.build import _sequential

        monkeypatch.setattr(_sequential, "_BULK_REGISTER_CHUNK_SIZE", 3)

        registry = BulkTrackingRegistry()
        leaves = [SyncOnlyTask(name=f"chunk_leaf_{i}") for i in range(6)]
        root = SyncOnlyTask(name="chunk_root", deps=tuple(leaves))

        build_sequential([root], registry=registry)

        # 7 tasks total → 3 chunks at chunk_size=3 (3+3+1).
        chunk_sizes = [len(b) for b in registry.bulk_batches]
        assert chunk_sizes == [3, 3, 1], (
            f"Expected chunks of [3, 3, 1]; got {chunk_sizes}"
        )
        # All tasks accounted for, in post-order — root last.
        flat = [tid for chunk in registry.bulk_batches for tid in chunk]
        assert set(flat) == {root.id, *(leaf.id for leaf in leaves)}
        assert flat[-1] == root.id

    @pytest.mark.asyncio
    async def test_concurrent_chunks_large_batch(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
        monkeypatch,
    ):
        from stardag.build import _concurrent

        monkeypatch.setattr(_concurrent, "_BULK_REGISTER_CHUNK_SIZE", 3)

        registry = BulkTrackingRegistry()
        leaves = [SyncOnlyTask(name=f"async_chunk_leaf_{i}") for i in range(6)]
        root = SyncOnlyTask(name="async_chunk_root", deps=tuple(leaves))

        await build_aio([root], registry=registry)

        chunk_sizes = [len(b) for b in registry.bulk_batches]
        assert chunk_sizes == [3, 3, 1], chunk_sizes
        flat = [tid for chunk in registry.bulk_batches for tid in chunk]
        assert set(flat) == {root.id, *(leaf.id for leaf in leaves)}
        # Root must come after every leaf even with concurrent sibling
        # interleaving.
        leaf_positions = [flat.index(leaf.id) for leaf in leaves]
        assert flat.index(root.id) > max(leaf_positions)


class TestNoPhantomsHappyPath:
    """In normal operation (post-order discover + bulk register) every
    task is registered with its real data before any edge referencing it
    is emitted. The simplest way to verify this is to see that
    ``dependency_task_ids`` only ever contains ids for tasks that
    appeared *earlier* in the same bulk batch — so the API resolves them
    without phantom creation.
    """

    def test_bulk_array_orders_deps_before_parents(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
    ):
        registry = BulkTrackingRegistry()

        leaf_a = SyncOnlyTask(name="np_leaf_a")
        leaf_b = SyncOnlyTask(name="np_leaf_b")
        mid = SyncOnlyTask(name="np_mid", deps=(leaf_a,))
        root = SyncOnlyTask(name="np_root", deps=(mid, leaf_b))

        build_sequential([root], registry=registry)

        order = registry.bulk_batches[0]
        # For every task in the batch, all of its *registration-time*
        # deps must appear earlier in the array.
        positions = {tid: i for i, tid in enumerate(order)}
        for task in (leaf_a, leaf_b, mid, root):
            for dep in task.deps:
                assert positions[dep.id] < positions[task.id], (
                    f"Dep {dep.id} should appear before {task.id} in bulk order; "
                    f"got positions {positions}"
                )


# ============================================================================
# Test: Discover-time registration error handling
# ============================================================================


class FailOnStartRegistry(NoOpRegistry):
    """Always 404s task_start (e.g. registration didn't land in warn mode)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, UUID]] = []

    def task_register(self, build_id: UUID, task) -> None:
        self.calls.append(("task_register", task.id))

    def task_start(self, build_id: UUID, task) -> None:
        self.calls.append(("task_start", task.id))
        raise ConnectionError("would-be 404 on /start")

    def task_complete(self, build_id: UUID, task) -> None:
        self.calls.append(("task_complete", task.id))

    async def task_register_aio(self, build_id: UUID, task) -> None:
        self.calls.append(("task_register", task.id))

    async def task_start_aio(self, build_id: UUID, task) -> None:
        self.calls.append(("task_start", task.id))
        raise ConnectionError("would-be 404 on /start")

    async def task_complete_aio(self, build_id: UUID, task) -> None:
        self.calls.append(("task_complete", task.id))


class TestDiscoverTimeRegistrationErrorHandling:
    """In `warn` mode, registry hiccups during discover-time registration —
    or the resulting 404 from /start when registration didn't land — must
    not abort the build. The task still executes locally."""

    def test_warn_mode_tolerates_start_failure_sequential(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
    ):
        registry = FailOnStartRegistry()
        task = SyncOnlyTask(name="warn_start_seq")
        summary = build_sequential(
            [task], registry=registry, on_registry_failure="warn"
        )
        assert summary.status == BuildExitStatus.SUCCESS
        # Task ran (so its target exists) and the build didn't blow up.
        assert task.complete()

    def test_raise_mode_propagates_start_failure_sequential(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
    ):
        registry = FailOnStartRegistry()
        task = SyncOnlyTask(name="raise_start_seq")
        with pytest.raises(ConnectionError, match="would-be 404"):
            build_sequential([task], registry=registry, on_registry_failure="raise")

    @pytest.mark.parametrize(
        "build_aio_fn",
        [build_aio, build_sequential_aio],
        ids=["concurrent", "sequential"],
    )
    @pytest.mark.asyncio
    async def test_warn_mode_tolerates_start_failure_aio(
        self,
        build_aio_fn,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
    ):
        registry = FailOnStartRegistry()
        task = SyncOnlyTask(name=f"warn_start_aio_{build_aio_fn.__name__}")
        summary = await build_aio_fn(
            [task], registry=registry, on_registry_failure="warn"
        )
        assert summary.status == BuildExitStatus.SUCCESS
        assert task.complete()

    def test_warn_mode_skips_start_when_registration_failed_concurrent(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
    ):
        """Concurrent build: when discover-time register fails (and the
        retry inside submit_with_lock also fails), task_start_aio should
        be skipped rather than 404-ing the build."""

        class AlwaysFailRegisterRegistry(NoOpRegistry):
            def __init__(self) -> None:
                self.calls: list[tuple[str, UUID]] = []

            def task_register(self, build_id, task) -> None:
                self.calls.append(("task_register", task.id))
                raise ConnectionError("register down")

            def task_start(self, build_id, task) -> None:
                self.calls.append(("task_start", task.id))

            def task_complete(self, build_id, task) -> None:
                self.calls.append(("task_complete", task.id))

            async def task_register_aio(self, build_id, task) -> None:
                self.calls.append(("task_register", task.id))
                raise ConnectionError("register down")

            async def task_start_aio(self, build_id, task) -> None:
                self.calls.append(("task_start", task.id))

            async def task_complete_aio(self, build_id, task) -> None:
                self.calls.append(("task_complete", task.id))

        registry = AlwaysFailRegisterRegistry()
        task = SyncOnlyTask(name="skip_start_concurrent")
        summary = build([task], registry=registry, on_registry_failure="warn")

        assert summary.status == BuildExitStatus.SUCCESS
        assert task.complete()
        # No start event should have been attempted — registration never
        # landed, so /start would have 404'd.
        start_calls = [c for c in registry.calls if c[0] == "task_start"]
        assert start_calls == [], (
            f"Expected /start to be skipped when registration didn't land; "
            f"got {registry.calls}"
        )


# ============================================================================
# Test: build_fail emitted when discover() raises
# ============================================================================


class _FailingRequiresTask(SyncOnlyTask):
    """A task whose requires() raises, simulating a discovery-time crash."""

    def requires(self):
        raise RuntimeError("requires() exploded during discovery")


class BuildFailTrackingRegistry(NoOpRegistry):
    def __init__(self) -> None:
        self.build_started_with: UUID | None = None
        self.build_failed_with: UUID | None = None
        self.build_completed_with: UUID | None = None

    def build_start(self, root_tasks=None, description=None):
        from uuid import uuid4

        self.build_started_with = uuid4()
        return self.build_started_with

    async def build_start_aio(self, root_tasks=None, description=None):
        return self.build_start(root_tasks, description)

    def build_fail(self, build_id, error_message=None):
        self.build_failed_with = build_id

    async def build_fail_aio(self, build_id, error_message=None):
        self.build_fail(build_id, error_message)

    def build_complete(self, build_id):
        self.build_completed_with = build_id

    async def build_complete_aio(self, build_id):
        self.build_complete(build_id)

    def task_register(self, build_id, task):
        pass

    async def task_register_aio(self, build_id, task):
        pass


class TestDiscoveryFailureBuildFail:
    """If discovery raises, build_fail must reach the registry — otherwise
    the build is left RUNNING forever."""

    def test_sequential_emits_build_fail_when_discover_raises(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
    ):
        registry = BuildFailTrackingRegistry()
        with pytest.raises(RuntimeError, match="requires.*exploded"):
            build_sequential([_FailingRequiresTask(name="boom")], registry=registry)
        assert registry.build_started_with is not None
        assert registry.build_failed_with == registry.build_started_with
        assert registry.build_completed_with is None

    @pytest.mark.parametrize(
        "build_aio_fn",
        [build_aio, build_sequential_aio],
        ids=["concurrent", "sequential"],
    )
    @pytest.mark.asyncio
    async def test_aio_emits_build_fail_when_discover_raises(
        self,
        build_aio_fn,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
    ):
        registry = BuildFailTrackingRegistry()
        # build_aio's TaskGroup wraps the RuntimeError in an ExceptionGroup;
        # build_sequential_aio re-raises directly. Both subclass `Exception`,
        # so a single Exception catch works on Python 3.10+ without
        # name-resolving BaseExceptionGroup at parse time.
        with pytest.raises(Exception):
            await build_aio_fn(
                [_FailingRequiresTask(name=f"boom_{build_aio_fn.__name__}")],
                registry=registry,
            )
        assert registry.build_started_with is not None
        assert registry.build_failed_with == registry.build_started_with
        assert registry.build_completed_with is None
