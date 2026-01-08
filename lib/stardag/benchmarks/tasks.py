"""Benchmark task definitions for concurrent build comparison.

Three workload types to expose different characteristics:
- IO-bound: Simulated I/O wait (time.sleep) - parallelizes well in all approaches
- CPU-bound: Actual computation - only multiprocessing bypasses GIL
- Light: Minimal work - exposes overhead differences between approaches
"""

from __future__ import annotations

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

    This workload parallelizes well in all approaches since threads
    release the GIL during sleep.
    """

    sleep_duration: float = 0.1

    def _do_work(self) -> None:
        time.sleep(self.sleep_duration)


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
    """IO-bound task with dynamic dependencies."""

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
