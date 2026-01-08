"""Tests for concurrent build implementations."""

import json
import typing
from uuid import UUID

import pytest

from stardag.build.concurrent.threadpool import build_simple as threadpool_build
from stardag.build.concurrent.asyncio_builder import build as asyncio_build
from stardag.build.concurrent.asyncio_builder import build_queue_based
from stardag.build.registry import NoOpRegistry
from stardag.target import InMemoryFileSystemTarget
from stardag.utils.testing.simple_dag import RootTask, RootTaskLoadedT
from stardag.utils.testing.dynamic_deps_dag import (
    assert_dynamic_deps_task_complete_recursive,
    get_dynamic_deps_dag,
)


@pytest.fixture
def noop_registry():
    """Provide a NoOpRegistry for tests."""
    return NoOpRegistry()


class TestThreadPoolBuild:
    """Tests for ThreadPool-based concurrent build."""

    def test_build_simple_dag(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileSystemTarget],
        simple_dag: RootTask,
        simple_dag_expected_root_output: RootTaskLoadedT,
        noop_registry,
    ):
        """Test building a simple DAG with threadpool."""
        threadpool_build(simple_dag, max_workers=2, registry=noop_registry)

        assert simple_dag.output().load() == simple_dag_expected_root_output
        expected_root_path = f"in-memory://{simple_dag._relpath}"
        assert (
            InMemoryFileSystemTarget.path_to_bytes[expected_root_path]
            == json.dumps(
                simple_dag_expected_root_output, separators=(",", ":")
            ).encode()
        )

    def test_build_simple_dag_with_completion_cache(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileSystemTarget],
        simple_dag: RootTask,
        simple_dag_expected_root_output: RootTaskLoadedT,
        noop_registry,
    ):
        """Test that completion cache prevents re-execution."""
        completion_cache: set[UUID] = set()

        # First build
        threadpool_build(
            simple_dag,
            max_workers=2,
            completion_cache=completion_cache,
            registry=noop_registry,
        )
        assert simple_dag.complete()

        # Build again - should use cache
        initial_cache_size = len(completion_cache)
        threadpool_build(
            simple_dag,
            max_workers=2,
            completion_cache=completion_cache,
            registry=noop_registry,
        )

        # Cache should be same size (no new tasks run)
        assert len(completion_cache) == initial_cache_size

    def test_build_with_dynamic_deps(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileSystemTarget],
        noop_registry,
    ):
        """Test building DAG with dynamic dependencies."""
        dag = get_dynamic_deps_dag()
        assert_dynamic_deps_task_complete_recursive(dag, False)

        threadpool_build(dag, max_workers=4, registry=noop_registry)

        assert_dynamic_deps_task_complete_recursive(dag, True)


class TestAsyncIOBuild:
    """Tests for AsyncIO-based concurrent build."""

    @pytest.mark.asyncio
    async def test_build_simple_dag(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileSystemTarget],
        simple_dag: RootTask,
        simple_dag_expected_root_output: RootTaskLoadedT,
        noop_registry,
    ):
        """Test building a simple DAG with asyncio."""
        await asyncio_build(simple_dag, max_concurrent=2, registry=noop_registry)

        assert simple_dag.output().load() == simple_dag_expected_root_output
        expected_root_path = f"in-memory://{simple_dag._relpath}"
        assert (
            InMemoryFileSystemTarget.path_to_bytes[expected_root_path]
            == json.dumps(
                simple_dag_expected_root_output, separators=(",", ":")
            ).encode()
        )

    @pytest.mark.asyncio
    async def test_build_with_dynamic_deps(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileSystemTarget],
        noop_registry,
    ):
        """Test building DAG with dynamic dependencies using asyncio."""
        dag = get_dynamic_deps_dag()
        assert_dynamic_deps_task_complete_recursive(dag, False)

        await asyncio_build(dag, max_concurrent=4, registry=noop_registry)

        assert_dynamic_deps_task_complete_recursive(dag, True)

    @pytest.mark.asyncio
    async def test_build_queue_based_simple_dag(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileSystemTarget],
        simple_dag: RootTask,
        simple_dag_expected_root_output: RootTaskLoadedT,
        noop_registry,
    ):
        """Test queue-based async build with simple DAG."""
        await build_queue_based(simple_dag, max_concurrent=2, registry=noop_registry)

        assert simple_dag.output().load() == simple_dag_expected_root_output

    @pytest.mark.asyncio
    async def test_build_queue_based_with_dynamic_deps(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileSystemTarget],
        noop_registry,
    ):
        """Test queue-based async build with dynamic dependencies."""
        dag = get_dynamic_deps_dag()
        assert_dynamic_deps_task_complete_recursive(dag, False)

        await build_queue_based(dag, max_concurrent=4, registry=noop_registry)

        assert_dynamic_deps_task_complete_recursive(dag, True)


class TestMultiprocessingBuild:
    """Tests for multiprocessing-based concurrent build."""

    def test_build_simple_dag(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileSystemTarget],
        simple_dag: RootTask,
        simple_dag_expected_root_output: RootTaskLoadedT,
        noop_registry,
    ):
        """Test building a simple DAG with multiprocessing.

        NOTE: This test may fail with InMemoryFileSystemTarget because
        the in-memory storage is not shared across processes.
        Multiprocessing works best with real filesystem or S3 targets.
        """
        # Skip if using in-memory target (not shared across processes)
        pytest.skip(
            "Multiprocessing doesn't work with InMemoryFileSystemTarget "
            "(not shared across processes)"
        )

    def test_dynamic_deps_not_supported(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileSystemTarget],
        noop_registry,
    ):
        """Test that dynamic deps raise NotImplementedError."""
        from stardag.build.concurrent.multiprocess import build as mp_build

        dag = get_dynamic_deps_dag()

        with pytest.raises(NotImplementedError, match="dynamic dependencies"):
            mp_build(dag, max_workers=2, registry=noop_registry)


class TestConcurrencyBehavior:
    """Tests to verify concurrent execution behavior."""

    def test_concurrent_execution_order(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileSystemTarget],
        noop_registry,
    ):
        """Test that independent tasks can execute concurrently."""
        from stardag._auto_task import AutoTask
        from stardag._task import auto_namespace
        import time
        import threading

        auto_namespace(__name__)

        execution_log: list[tuple[str, str, float]] = []
        log_lock = threading.Lock()

        class TimedTask(AutoTask[str]):
            name: str
            delay: float = 0.1
            deps: tuple["TimedTask", ...] = ()

            def requires(self):
                return self.deps

            def run(self):
                start = time.time()
                with log_lock:
                    execution_log.append((self.name, "start", start))

                time.sleep(self.delay)

                with log_lock:
                    execution_log.append((self.name, "end", time.time()))

                self.output().save(self.name)

        # Create a DAG where A and B can run in parallel, then C depends on both
        task_a = TimedTask(name="A", delay=0.1)
        task_b = TimedTask(name="B", delay=0.1)
        task_c = TimedTask(name="C", delay=0.05, deps=(task_a, task_b))

        threadpool_build(task_c, max_workers=4, registry=noop_registry)

        # Check that A and B overlapped in time
        a_start = next(t for n, e, t in execution_log if n == "A" and e == "start")
        a_end = next(t for n, e, t in execution_log if n == "A" and e == "end")
        b_start = next(t for n, e, t in execution_log if n == "B" and e == "start")
        b_end = next(t for n, e, t in execution_log if n == "B" and e == "end")
        c_start = next(t for n, e, t in execution_log if n == "C" and e == "start")

        # A and B should have overlapping execution windows
        # (either A started before B ended, or B started before A ended)
        assert a_start < b_end and b_start < a_end, (
            f"A and B should overlap: A=[{a_start}, {a_end}], B=[{b_start}, {b_end}]"
        )

        # C should start after both A and B end
        assert c_start >= a_end and c_start >= b_end, (
            f"C should start after A and B: C_start={c_start}, A_end={a_end}, B_end={b_end}"
        )
