"""Shared test tasks for build tests."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from stardag import AutoTask, auto_namespace

auto_namespace(__name__)


# ============================================================================
# Test Tasks - Basic
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
