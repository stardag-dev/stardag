"""Benchmark task definitions for concurrent build comparison.

Three workload types to expose different characteristics:
- IO-bound: Simulated I/O wait - uses asyncio.sleep in run_aio() for true async
- CPU-bound: Actual computation - only multiprocessing bypasses GIL
- Light: Minimal work - exposes overhead differences between approaches

Key insight: IO-bound tasks implement both run() and run_aio():
- run(): Uses time.sleep() - blocks thread, works with threadpool
- run_aio(): Uses asyncio.sleep() - true async, called by asyncio builder

With synthetic sleep, asyncio and threadpool perform similarly because threads
release the GIL during sleep. The async advantage becomes significant with:
- Real network I/O (aiohttp vs requests) - connection pooling, no thread overhead
- Many short-lived I/O operations - thread creation overhead avoided
- Memory-constrained environments - coroutines use less memory than threads
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from typing import TYPE_CHECKING

from stardag._auto_task import AutoTask
from stardag._task import auto_namespace

if TYPE_CHECKING:
    from stardag._task import TaskStruct

auto_namespace(__name__)


# =============================================================================
# Base benchmark task
# =============================================================================


class BenchmarkTask(AutoTask[dict]):
    """Base class for benchmark tasks with timing."""

    task_id: str
    deps: tuple["BenchmarkTask", ...] = ()

    def requires(self) -> TaskStruct:
        return self.deps

    def run(self) -> None:
        start = time.perf_counter()
        self._do_work()
        elapsed = time.perf_counter() - start
        self.output().save({"task_id": self.task_id, "elapsed": elapsed})

    def _do_work(self) -> None:
        """Override in subclasses to do actual work."""
        pass


# =============================================================================
# IO-bound workload (thread blocking)
# =============================================================================


class IOBoundTask(BenchmarkTask):
    """Simulates I/O-bound work with sleep.

    Implements both sync run() and async run_aio() to demonstrate the difference:
    - run(): Uses time.sleep() - blocks thread, works with threadpool
    - run_aio(): Uses asyncio.sleep() - true async, excels with asyncio builder

    The asyncio builder will detect run_aio() and call it directly, achieving
    true concurrent execution without thread overhead.
    """

    sleep_duration: float = 0.1

    def _do_work(self) -> None:
        time.sleep(self.sleep_duration)

    async def run_aio(self) -> None:
        """Async implementation using asyncio.sleep for true async I/O."""
        start = time.perf_counter()
        await asyncio.sleep(self.sleep_duration)
        elapsed = time.perf_counter() - start
        self.output().save({"task_id": self.task_id, "elapsed": elapsed})


# =============================================================================
# CPU-bound workload
# =============================================================================


class CPUBoundTask(BenchmarkTask):
    """CPU-intensive work (hashing iterations).

    Only multiprocessing can truly parallelize this due to the GIL.
    ThreadPool and AsyncIO will be limited to single-core.
    """

    iterations: int = 100_000

    def _do_work(self) -> None:
        # CPU-bound work: hash iterations
        data = b"benchmark"
        for _ in range(self.iterations):
            data = hashlib.sha256(data).digest()


# =============================================================================
# Heavy CPU-bound workload (multiprocess wins here)
# =============================================================================


class HeavyCPUBoundTask(BenchmarkTask):
    """Heavy CPU-intensive work (~1s per task).

    With tasks this heavy, multiprocessing's process spawn overhead
    becomes negligible and true parallelism provides significant speedup.
    ThreadPool/AsyncIO are limited by GIL to sequential execution.
    """

    iterations: int = 5_000_000  # ~1s per task

    def _do_work(self) -> None:
        # Heavy CPU-bound work: many hash iterations
        data = b"benchmark"
        for _ in range(self.iterations):
            data = hashlib.sha256(data).digest()


# =============================================================================
# Light workload
# =============================================================================


class LightTask(BenchmarkTask):
    """Minimal work to expose scheduling overhead.

    Process spawning overhead in multiprocessing should be visible here.
    ThreadPool and AsyncIO should be fast.
    """

    def _do_work(self) -> None:
        # Trivial computation
        _ = sum(range(100))


# =============================================================================
# Dynamic dependency variants
# =============================================================================


class IOBoundDynamicTask(AutoTask[dict]):
    """IO-bound task with dynamic dependencies.

    Note: Dynamic tasks with yield cannot easily implement run_aio() because
    async generators require different handling than sync generators.
    These tasks fall back to asyncio.to_thread(run) in the async builder.
    """

    task_id: str
    sleep_duration: float = 0.1
    static_deps: tuple["IOBoundDynamicTask", ...] = ()
    dynamic_dep_ids: tuple[str, ...] = ()

    def requires(self) -> TaskStruct:
        return self.static_deps

    def run(self):
        # Yield dynamic deps first (as dynamic tasks with no further deps)
        if self.dynamic_dep_ids:
            dynamic_deps = tuple(
                IOBoundDynamicTask(task_id=dep_id, sleep_duration=self.sleep_duration)
                for dep_id in self.dynamic_dep_ids
            )
            yield dynamic_deps

        # Do the actual work
        start = time.perf_counter()
        time.sleep(self.sleep_duration)
        elapsed = time.perf_counter() - start
        self.output().save({"task_id": self.task_id, "elapsed": elapsed})


class CPUBoundDynamicTask(AutoTask[dict]):
    """CPU-bound task with dynamic dependencies."""

    task_id: str
    iterations: int = 100_000
    static_deps: tuple["CPUBoundDynamicTask", ...] = ()
    dynamic_dep_ids: tuple[str, ...] = ()

    def requires(self) -> TaskStruct:
        return self.static_deps

    def run(self):
        # Yield dynamic deps first (as dynamic tasks with no further deps)
        if self.dynamic_dep_ids:
            dynamic_deps = tuple(
                CPUBoundDynamicTask(task_id=dep_id, iterations=self.iterations)
                for dep_id in self.dynamic_dep_ids
            )
            yield dynamic_deps

        # Do the actual work
        start = time.perf_counter()
        data = b"benchmark"
        for _ in range(self.iterations):
            data = hashlib.sha256(data).digest()
        elapsed = time.perf_counter() - start
        self.output().save({"task_id": self.task_id, "elapsed": elapsed})


class LightDynamicTask(AutoTask[dict]):
    """Light task with dynamic dependencies."""

    task_id: str
    static_deps: tuple["LightDynamicTask", ...] = ()
    dynamic_dep_ids: tuple[str, ...] = ()

    def requires(self) -> TaskStruct:
        return self.static_deps

    def run(self):
        # Yield dynamic deps first (as dynamic tasks with no further deps)
        if self.dynamic_dep_ids:
            dynamic_deps = tuple(
                LightDynamicTask(task_id=dep_id) for dep_id in self.dynamic_dep_ids
            )
            yield dynamic_deps

        # Do the actual work
        start = time.perf_counter()
        _ = sum(range(100))
        elapsed = time.perf_counter() - start
        self.output().save({"task_id": self.task_id, "elapsed": elapsed})
