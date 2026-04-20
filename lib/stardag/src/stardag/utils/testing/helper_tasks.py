"""Shared test tasks for build tests."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from stardag import Task, auto_namespace

auto_namespace(__name__)


# ============================================================================
# Test Tasks - Basic
# ============================================================================


class SyncOnlyTask(Task[dict[str, Any]]):
    """Task with only sync run()."""

    name: str
    deps: tuple[Task, ...] = ()

    def requires(self):
        return self.deps

    def run(self):
        self._save({"name": self.name, "mode": "sync"})


class AsyncOnlyTask(Task[dict[str, Any]]):
    """Task with only async run_aio()."""

    name: str
    deps: tuple[Task, ...] = ()

    def requires(self):
        return self.deps

    async def run_aio(self):
        await asyncio.sleep(0.01)  # Small async operation
        self._save({"name": self.name, "mode": "async"})


class DualTask(Task[dict[str, Any]]):
    """Task with both sync run() and async run_aio()."""

    name: str
    prefer_async: bool = True
    deps: tuple[Task, ...] = ()

    def requires(self):
        return self.deps

    def run(self):
        self._save({"name": self.name, "mode": "sync"})

    async def run_aio(self):
        await asyncio.sleep(0.01)
        self._save({"name": self.name, "mode": "async"})


class FailingTask(Task[str]):
    """Task that always fails."""

    error_message: str = "Intentional failure"

    def run(self):
        raise ValueError(self.error_message)


class FailingAsyncTask(Task[str]):
    """Async task that always fails."""

    error_message: str = "Intentional async failure"

    async def run_aio(self):
        await asyncio.sleep(0.001)
        raise ValueError(self.error_message)


class SlowTask(Task[dict[str, Any]]):
    """Task with configurable delay for testing concurrency."""

    name: str
    delay: float = 0.1
    deps: tuple[Task, ...] = ()

    def requires(self):
        return self.deps

    def run(self):
        start = time.time()
        time.sleep(self.delay)
        end = time.time()
        self._save({"name": self.name, "start": start, "end": end})


# ============================================================================
# Test Tasks - Diamond Pattern
# ============================================================================


# Global tracking for diamond tests - reset per test
_execution_counts: dict[str, int] = {}


def reset_execution_counts():
    """Reset the global execution counter for diamond tests."""
    global _execution_counts
    _execution_counts = {}


def get_execution_count(test_id: str, name: str) -> int:
    """Get the execution count for a task."""
    return _execution_counts.get(f"{test_id}:{name}", 0)


class DiamondTask(Task[str]):
    """Task that tracks execution count for diamond pattern tests."""

    name: str
    test_id: str  # Unique per test to isolate task IDs
    deps: tuple[Task, ...] = ()

    def requires(self):
        return self.deps

    def run(self):
        global _execution_counts
        key = f"{self.test_id}:{self.name}"
        _execution_counts[key] = _execution_counts.get(key, 0) + 1
        self._save(f"{self.name}:{_execution_counts[key]}")


class DynamicDiamondTask(Task[str]):
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

        self._save(f"{self.name}:{_execution_counts[key]}")


class AsyncDynamicDiamondTask(Task[str]):
    """Async task with dynamic deps that tracks execution for diamond tests.

    Uses an async generator ``run_aio`` to yield dynamic dependencies.
    """

    name: str
    test_id: str
    static_task_deps: tuple["AsyncDynamicDiamondTask", ...] = ()
    dynamic_task_deps: tuple["AsyncDynamicDiamondTask", ...] = ()

    def requires(self):
        return self.static_task_deps

    async def run_aio(self):  # type: ignore[override]
        global _execution_counts
        key = f"{self.test_id}:{self.name}"
        _execution_counts[key] = _execution_counts.get(key, 0) + 1

        # CONTRACT: static requires() must be complete at the start of run.
        for static_dep in self.static_task_deps:
            if not static_dep.complete():
                raise AssertionError(
                    f"Static requires contract violated! Dep {static_dep} is not "
                    f"complete at the start of {self}.run_aio()."
                )

        for dep in self.dynamic_task_deps:
            yield dep
            if not dep.complete():
                raise AssertionError(
                    f"Dynamic deps contract violated! Dep {dep} is not complete "
                    f"after yield in {self}.run_aio()."
                )

        self._save(f"{self.name}:{_execution_counts[key]}")
