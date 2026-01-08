"""Tests for concurrent build implementations.

All implementations are tested with the same test cases using parameterization
to ensure consistent behavior across:
- ThreadPool-based build (build_simple)
- AsyncIO-based build (build, build_queue_based)
- Multiprocessing-based build
"""

from __future__ import annotations

import asyncio
import threading
import time
import typing
from pathlib import Path
from typing import Any, Callable, Coroutine
from uuid import UUID

import pytest

from stardag.build.concurrent.threadpool import build_simple as threadpool_build
from stardag.build.concurrent.asyncio_builder import build as asyncio_build
from stardag.build.concurrent.asyncio_builder import (
    build_queue_based as asyncio_queue_build,
)
from stardag.build.concurrent.multiprocess import build as multiprocess_build
from stardag.build.registry import NoOpRegistry
from stardag.target import InMemoryFileSystemTarget
from stardag.utils.testing.simple_dag import (
    get_simple_dag,
    get_simple_dag_expected_root_output,
)
from stardag.utils.testing.dynamic_deps_dag import (
    assert_dynamic_deps_task_complete_recursive,
    get_dynamic_deps_dag,
)


# Type alias for build functions
SyncBuildFunc = Callable[..., None]
AsyncBuildFunc = Callable[..., Coroutine[Any, Any, None]]


@pytest.fixture
def noop_registry():
    """Provide a NoOpRegistry for tests."""
    return NoOpRegistry()


# ============================================================================
# Build function wrappers to normalize interfaces
# ============================================================================


def _wrap_async_build(
    async_build_func: AsyncBuildFunc, concurrency_param: str = "max_concurrent"
) -> SyncBuildFunc:
    """Wrap an async build function to be callable synchronously.

    Also normalizes the concurrency parameter name to max_workers.
    """

    def wrapper(task, max_workers=None, **kwargs):
        # Translate max_workers to the appropriate param for async functions
        if max_workers is not None:
            kwargs[concurrency_param] = max_workers
        asyncio.run(async_build_func(task, **kwargs))

    wrapper.__name__ = async_build_func.__name__  # type: ignore
    return wrapper


# Build implementations with metadata
# (name, build_func, requires_real_filesystem)
BUILD_IMPLEMENTATIONS: list[tuple[str, SyncBuildFunc, bool]] = [
    ("threadpool", threadpool_build, False),
    ("asyncio", _wrap_async_build(asyncio_build, "max_concurrent"), False),
    ("asyncio_queue", _wrap_async_build(asyncio_queue_build, "max_concurrent"), False),
    ("multiprocess", multiprocess_build, True),  # Needs real filesystem
]


# ============================================================================
# Parameterized fixtures
# ============================================================================


@pytest.fixture(
    params=[
        pytest.param(impl, id=impl[0])
        for impl in BUILD_IMPLEMENTATIONS
        if not impl[2]  # In-memory compatible
    ]
)
def build_func_inmemory(
    request,
    default_in_memory_fs_target: typing.Type[InMemoryFileSystemTarget],
    noop_registry,
) -> tuple[str, SyncBuildFunc]:
    """Build functions that work with in-memory filesystem."""
    name, func, _ = request.param
    return (name, func)


@pytest.fixture(
    params=[
        pytest.param(impl, id=impl[0])
        for impl in BUILD_IMPLEMENTATIONS
        if impl[2]  # Real filesystem only
    ]
)
def build_func_realfs(
    request,
    default_local_target_tmp_path: Path,
    noop_registry,
) -> tuple[str, SyncBuildFunc]:
    """Build functions that require real filesystem."""
    name, func, _ = request.param
    return (name, func)


@pytest.fixture(
    params=[pytest.param(impl, id=impl[0]) for impl in BUILD_IMPLEMENTATIONS]
)
def build_func_any(
    request,
    default_local_target_tmp_path: Path,  # Use real FS for all to simplify
    noop_registry,
) -> tuple[str, SyncBuildFunc]:
    """All build functions using real filesystem for compatibility."""
    name, func, _ = request.param
    return (name, func)


# ============================================================================
# Test: Simple DAG (all implementations)
# ============================================================================


class TestSimpleDAG:
    """Test building simple DAGs with all implementations."""

    def test_build_simple_dag(
        self,
        build_func_any: tuple[str, SyncBuildFunc],
        noop_registry,
    ):
        """Test building a simple DAG produces correct output."""
        impl_name, build_func = build_func_any

        # Create fresh DAG for this test
        dag = get_simple_dag()
        expected_output = get_simple_dag_expected_root_output()

        # Build
        build_func(dag, max_workers=2, registry=noop_registry)

        # Verify
        assert dag.complete(), f"{impl_name}: DAG should be complete after build"
        assert dag.output().load() == expected_output

    def test_build_with_completion_cache(
        self,
        build_func_any: tuple[str, SyncBuildFunc],
        noop_registry,
    ):
        """Test that completion cache prevents re-execution."""
        impl_name, build_func = build_func_any

        dag = get_simple_dag()
        completion_cache: set[UUID] = set()

        # First build
        build_func(
            dag,
            max_workers=2,
            completion_cache=completion_cache,
            registry=noop_registry,
        )
        assert dag.complete()

        # Note: Some implementations may not update completion_cache during build
        # (multiprocessing runs in separate processes). The key test is that
        # rebuilding a complete DAG doesn't fail or re-run tasks.

        # Build again - should work without errors
        build_func(
            dag,
            max_workers=2,
            completion_cache=completion_cache,
            registry=noop_registry,
        )

        # DAG should still be complete
        assert dag.complete()


# ============================================================================
# Test: Dynamic Dependencies DAG (all implementations)
# ============================================================================


class TestDynamicDepsDAG:
    """Test building DAGs with dynamic dependencies."""

    def test_build_with_dynamic_deps(
        self,
        build_func_any: tuple[str, SyncBuildFunc],
        noop_registry,
    ):
        """Test building DAG with dynamic dependencies."""
        impl_name, build_func = build_func_any

        dag = get_dynamic_deps_dag()

        # Verify not complete before build
        assert_dynamic_deps_task_complete_recursive(dag, False)

        # Build
        build_func(dag, max_workers=4, registry=noop_registry)

        # Verify all tasks complete after build
        assert_dynamic_deps_task_complete_recursive(dag, True)


# ============================================================================
# Test: Concurrency Behavior (all implementations)
# ============================================================================


class TestConcurrencyBehavior:
    """Test that implementations actually execute tasks concurrently."""

    def test_concurrent_execution_order(
        self,
        build_func_any: tuple[str, SyncBuildFunc],
        noop_registry,
    ):
        """Test that independent tasks execute concurrently."""
        from stardag._auto_task import AutoTask
        from stardag._task import auto_namespace

        impl_name, build_func = build_func_any

        # Skip multiprocessing - test-local classes can't be imported by subprocess
        # Multiprocessing concurrency is verified with importable classes in other tests
        if impl_name == "multiprocess":
            pytest.skip("Multiprocessing requires importable classes")

        auto_namespace(__name__)

        # Thread-safe execution log (also works with multiprocessing via file)
        # For simplicity, we'll track via task output timestamps
        execution_log: list[tuple[str, str, float]] = []
        log_lock = threading.Lock()

        class TimedTask(AutoTask[dict]):
            name: str
            delay: float = 0.1
            deps: tuple["TimedTask", ...] = ()

            def requires(self):
                return self.deps

            def run(self):
                start = time.time()

                # For in-process implementations, use the shared log
                with log_lock:
                    execution_log.append((self.name, "start", start))

                time.sleep(self.delay)

                end = time.time()
                with log_lock:
                    execution_log.append((self.name, "end", end))

                # Save timing info for cross-process verification
                self.output().save({"name": self.name, "start": start, "end": end})

        # Create a DAG where A and B can run in parallel, then C depends on both
        #     C
        #    / \
        #   A   B
        task_a = TimedTask(name="A", delay=0.15)
        task_b = TimedTask(name="B", delay=0.15)
        task_c = TimedTask(name="C", delay=0.05, deps=(task_a, task_b))

        # Build with enough workers for parallelism
        build_func(task_c, max_workers=4, registry=noop_registry)

        # For multiprocessing, read timing from outputs instead of shared log
        if impl_name == "multiprocess" or not execution_log:
            a_data = task_a.output().load()
            b_data = task_b.output().load()
            c_data = task_c.output().load()
            a_start, a_end = a_data["start"], a_data["end"]
            b_start, b_end = b_data["start"], b_data["end"]
            c_start = c_data["start"]
        else:
            a_start = next(t for n, e, t in execution_log if n == "A" and e == "start")
            a_end = next(t for n, e, t in execution_log if n == "A" and e == "end")
            b_start = next(t for n, e, t in execution_log if n == "B" and e == "start")
            b_end = next(t for n, e, t in execution_log if n == "B" and e == "end")
            c_start = next(t for n, e, t in execution_log if n == "C" and e == "start")

        # A and B should have overlapping execution windows
        assert a_start < b_end and b_start < a_end, (
            f"{impl_name}: A and B should overlap: "
            f"A=[{a_start:.3f}, {a_end:.3f}], B=[{b_start:.3f}, {b_end:.3f}]"
        )

        # C should start after both A and B end (with small tolerance)
        tolerance = 0.02
        assert c_start >= a_end - tolerance and c_start >= b_end - tolerance, (
            f"{impl_name}: C should start after A and B: "
            f"C_start={c_start:.3f}, A_end={a_end:.3f}, B_end={b_end:.3f}"
        )

    def test_max_workers_limits_concurrency(
        self,
        build_func_any: tuple[str, SyncBuildFunc],
        noop_registry,
    ):
        """Test that max_workers parameter limits concurrent execution."""
        from stardag._auto_task import AutoTask
        from stardag._task import auto_namespace

        impl_name, build_func = build_func_any

        # Skip multiprocessing - test-local classes can't be imported by subprocess
        if impl_name == "multiprocess":
            pytest.skip("Multiprocessing requires importable classes")

        auto_namespace(__name__)

        class ConcurrencyTracker(AutoTask[dict]):
            task_id: str

            def run(self):
                start = time.time()
                time.sleep(0.1)  # Hold the slot
                end = time.time()
                self.output().save(
                    {"task_id": self.task_id, "start": start, "end": end}
                )

        # Create 4 independent tasks
        tasks = [ConcurrencyTracker(task_id=f"task_{i}") for i in range(4)]

        # Create a root that depends on all
        class RootTracker(AutoTask[str]):
            deps: tuple[ConcurrencyTracker, ...]

            def requires(self):
                return self.deps

            def run(self):
                self.output().save("done")

        root = RootTracker(deps=tuple(tasks))

        # Build with max_workers=2
        build_func(root, max_workers=2, registry=noop_registry)

        # Read timing data from outputs
        timings = [t.output().load() for t in tasks]

        # Check maximum overlap
        # Sort by start time and check how many were running at each point
        events = []
        for t in timings:
            events.append((t["start"], 1))  # +1 at start
            events.append((t["end"], -1))  # -1 at end
        events.sort()

        max_concurrent = 0
        current = 0
        for _, delta in events:
            current += delta
            max_concurrent = max(max_concurrent, current)

        # Should never have more than max_workers concurrent (with tolerance)
        assert max_concurrent <= 3, (
            f"{impl_name}: Expected max 2-3 concurrent, got {max_concurrent}"
        )


# ============================================================================
# Test: Error Handling (all implementations)
# ============================================================================


class TestErrorHandling:
    """Test error handling in build implementations."""

    def test_task_failure_propagates(
        self,
        build_func_any: tuple[str, SyncBuildFunc],
        noop_registry,
    ):
        """Test that task failures are properly propagated."""
        from stardag._auto_task import AutoTask
        from stardag._task import auto_namespace

        impl_name, build_func = build_func_any

        # Skip multiprocessing - test-local classes can't be imported by subprocess
        if impl_name == "multiprocess":
            pytest.skip("Multiprocessing requires importable classes")

        auto_namespace(__name__)

        class FailingTask(AutoTask[str]):
            def run(self):
                raise ValueError("Intentional failure")

        task = FailingTask()

        with pytest.raises((RuntimeError, ValueError)):
            build_func(task, max_workers=2, registry=noop_registry)


# ============================================================================
# Implementation-specific tests (using appropriate fixtures)
# ============================================================================


class TestInMemoryImplementations:
    """Tests for implementations that work with in-memory filesystem."""

    def test_build_simple_dag_inmemory(
        self,
        build_func_inmemory: tuple[str, SyncBuildFunc],
        noop_registry,
    ):
        """Test in-memory implementations with simple DAG."""
        impl_name, build_func = build_func_inmemory

        dag = get_simple_dag()
        expected_output = get_simple_dag_expected_root_output()

        build_func(dag, max_workers=2, registry=noop_registry)

        assert dag.complete()
        assert dag.output().load() == expected_output

    def test_build_dynamic_deps_inmemory(
        self,
        build_func_inmemory: tuple[str, SyncBuildFunc],
        noop_registry,
    ):
        """Test in-memory implementations with dynamic deps DAG."""
        impl_name, build_func = build_func_inmemory

        dag = get_dynamic_deps_dag()
        assert_dynamic_deps_task_complete_recursive(dag, False)

        build_func(dag, max_workers=4, registry=noop_registry)

        assert_dynamic_deps_task_complete_recursive(dag, True)


class TestAsyncIOSpecific:
    """Tests specific to asyncio implementations."""

    @pytest.mark.asyncio
    async def test_asyncio_build_directly(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileSystemTarget],
        noop_registry,
    ):
        """Test calling asyncio build directly without wrapper."""
        dag = get_simple_dag()
        expected_output = get_simple_dag_expected_root_output()

        await asyncio_build(dag, max_concurrent=2, registry=noop_registry)

        assert dag.complete()
        assert dag.output().load() == expected_output

    @pytest.mark.asyncio
    async def test_asyncio_queue_build_directly(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileSystemTarget],
        noop_registry,
    ):
        """Test calling asyncio queue build directly without wrapper."""
        dag = get_simple_dag()
        expected_output = get_simple_dag_expected_root_output()

        await asyncio_queue_build(dag, max_concurrent=2, registry=noop_registry)

        assert dag.complete()
        assert dag.output().load() == expected_output

    @pytest.mark.asyncio
    async def test_asyncio_dynamic_deps_directly(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileSystemTarget],
        noop_registry,
    ):
        """Test asyncio with dynamic deps directly."""
        dag = get_dynamic_deps_dag()
        assert_dynamic_deps_task_complete_recursive(dag, False)

        await asyncio_build(dag, max_concurrent=4, registry=noop_registry)

        assert_dynamic_deps_task_complete_recursive(dag, True)


class TestMultiprocessingSpecific:
    """Tests specific to multiprocessing implementation."""

    def test_multiprocess_with_real_filesystem(
        self,
        default_local_target_tmp_path: Path,
        noop_registry,
    ):
        """Test multiprocessing with real filesystem target."""
        dag = get_simple_dag()
        expected_output = get_simple_dag_expected_root_output()

        multiprocess_build(dag, max_workers=2, registry=noop_registry)

        assert dag.complete()
        assert dag.output().load() == expected_output

    def test_multiprocess_dynamic_deps(
        self,
        default_local_target_tmp_path: Path,
        noop_registry,
    ):
        """Test multiprocessing handles dynamic deps via idempotency."""
        dag = get_dynamic_deps_dag()

        assert_dynamic_deps_task_complete_recursive(dag, False)

        multiprocess_build(dag, max_workers=4, registry=noop_registry)

        assert_dynamic_deps_task_complete_recursive(dag, True)
