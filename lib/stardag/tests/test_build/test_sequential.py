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
from uuid import UUID

from stardag.artifact import Artifact, MarkdownArtifact
from stardag.registry import NoOpRegistry
from stardag.target import InMemoryFileTarget
from stardag.utils.testing.helper_tasks import (
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
# Test: Registry communication for previously-completed tasks
# ============================================================================


class TrackingRegistry(NoOpRegistry):
    """A NoOpRegistry that records task lifecycle calls."""

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

        # FAIL_FAST: should raise the original ValueError, not ConnectionError
        with pytest.raises(ValueError, match="task broke"):
            build_sequential([task], registry=registry, fail_mode=FailMode.FAIL_FAST)

    def test_registry_task_fail_error_does_not_mask_task_error_continue(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
    ):
        """In CONTINUE mode, registry.task_fail error is swallowed gracefully."""
        registry = FailingOnTaskFailRegistry()
        task = FailingTask(error_message="task broke")

        # CONTINUE mode: should return a summary, not crash
        summary = build_sequential(
            [task], registry=registry, fail_mode=FailMode.CONTINUE
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
                [task], registry=registry, fail_mode=FailMode.FAIL_FAST
            )


# ============================================================================
# Test: Async artifact collection
# ============================================================================


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


class ArtifactTrackingRegistry(NoOpRegistry):
    """A registry that records artifact uploads."""

    def __init__(self) -> None:
        self.uploaded_artifacts: list[tuple[UUID, list]] = []

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
