"""Tests for concurrent build implementation.

Tests the new unified build() function from stardag.build._concurrent.
"""

from __future__ import annotations

import asyncio
import threading
import time
import typing

import pytest

from stardag._task import auto_namespace
from stardag.build import build, build_aio
from stardag.registry import NoOpRegistry
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
# Test: Simple DAG
# ============================================================================


class TestSimpleDAG:
    """Test building simple DAGs."""

    def test_build_simple_dag(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileSystemTarget],
        noop_registry,
    ):
        """Test building a simple DAG produces correct output."""
        dag = get_simple_dag()
        expected_output = get_simple_dag_expected_root_output()

        build([dag], registry=noop_registry)

        assert dag.complete(), "DAG should be complete after build"
        assert dag.output().load() == expected_output

    @pytest.mark.asyncio
    async def test_build_simple_dag_async(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileSystemTarget],
        noop_registry,
    ):
        """Test building a simple DAG asynchronously."""
        dag = get_simple_dag()
        expected_output = get_simple_dag_expected_root_output()

        await build_aio([dag], registry=noop_registry)

        assert dag.complete()
        assert dag.output().load() == expected_output

    def test_build_with_completion_cache(
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
# Test: Dynamic Dependencies DAG
# ============================================================================


class TestDynamicDepsDAG:
    """Test building DAGs with dynamic dependencies."""

    def test_build_with_dynamic_deps(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileSystemTarget],
        noop_registry,
    ):
        """Test building DAG with dynamic dependencies."""
        dag = get_dynamic_deps_dag()

        # Verify not complete before build
        assert_dynamic_deps_task_complete_recursive(dag, False)

        # Build
        build([dag], registry=noop_registry)

        # Verify all tasks complete after build
        assert_dynamic_deps_task_complete_recursive(dag, True)

    @pytest.mark.asyncio
    async def test_build_with_dynamic_deps_async(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileSystemTarget],
        noop_registry,
    ):
        """Test building DAG with dynamic dependencies asynchronously."""
        dag = get_dynamic_deps_dag()

        assert_dynamic_deps_task_complete_recursive(dag, False)

        await build_aio([dag], registry=noop_registry)

        assert_dynamic_deps_task_complete_recursive(dag, True)


# ============================================================================
# Test: Concurrency Behavior
# ============================================================================


class TestConcurrencyBehavior:
    """Test that tasks execute concurrently."""

    @pytest.mark.asyncio
    async def test_concurrent_execution_order(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileSystemTarget],
        noop_registry,
    ):
        """Test that independent tasks execute concurrently."""
        from stardag._auto_task import AutoTask

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


# ============================================================================
# Test: Error Handling
# ============================================================================


class TestErrorHandling:
    """Test error handling in build implementation."""

    @pytest.mark.asyncio
    async def test_task_failure_propagates(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileSystemTarget],
        noop_registry,
    ):
        """Test that task failures are properly propagated."""
        from stardag._auto_task import AutoTask

        class FailingTask(AutoTask[str]):
            async def run_aio(self):
                raise ValueError("Intentional failure")

        task = FailingTask()

        summary = await build_aio([task], registry=noop_registry)
        assert summary.error is not None
        assert isinstance(summary.error, ValueError)
