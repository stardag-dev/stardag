"""Tests for async task interface.

These tests verify that tasks with custom run_aio() implementations
work correctly with the async builder.
"""

import asyncio
import time
import typing

import pytest

from stardag._auto_task import AutoTask
from stardag._task import _has_custom_run_aio, auto_namespace
from stardag.build import build_aio
from stardag.registry import NoOpRegistry
from stardag.target import InMemoryFileSystemTarget

auto_namespace(__name__)


class TestAsyncInterface:
    """Tests for async task interface."""

    def test_has_custom_run_aio_detection(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileSystemTarget],
    ):
        """Test that we correctly detect custom run_aio implementations."""

        class SyncOnlyTask(AutoTask[int]):
            def run(self):
                self.output().save(42)

        class AsyncTask(AutoTask[int]):
            def run(self):
                self.output().save(42)

            async def run_aio(self):
                await asyncio.sleep(0)
                self.output().save(42)

        sync_task = SyncOnlyTask()
        async_task = AsyncTask()

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
