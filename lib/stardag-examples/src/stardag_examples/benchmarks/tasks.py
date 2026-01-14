"""Benchmark task definitions.

Three workload types to expose different characteristics:
- IO-bound: Simulated I/O wait - uses asyncio.sleep in run_aio() for true async
- CPU-bound: Actual computation - only multiprocessing bypasses GIL
- Light: Minimal work - exposes overhead differences between approaches

Key insight: IO-bound tasks implement both run() and run_aio():
- run(): Uses time.sleep() - blocks thread, works with threadpool
- run_aio(): Uses asyncio.sleep() - true async, called by async builder

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

import stardag as sd

sd.auto_namespace(__name__)


# =============================================================================
# Base benchmark task
# =============================================================================


class BenchmarkTask(sd.AutoTask[dict]):
    """Base class for benchmark tasks with timing."""

    key: str  # User-provided label for this task instance (not the actual task.id)
    deps: tuple[sd.SubClass[sd.Task], ...] = ()

    def requires(self) -> sd.TaskStruct:
        return self.deps

    def run(self) -> None:
        start = time.perf_counter()
        self._do_work()
        elapsed = time.perf_counter() - start
        self.output().save({"key": self.key, "elapsed": elapsed})

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

    The build system will detect run_aio() and call it directly, achieving
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
        self.output().save({"key": self.key, "elapsed": elapsed})


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


class IOBoundDynamicTask(sd.AutoTask[dict]):
    """IO-bound task with dynamic dependencies.

    Note: Dynamic tasks with yield cannot easily implement run_aio() because
    async generators require different handling than sync generators.
    These tasks fall back to asyncio.to_thread(run) in the async builder.
    """

    key: str  # User-provided label for this task instance (not the actual task.id)
    sleep_duration: float = 0.1
    static_deps: tuple["IOBoundDynamicTask", ...] = ()
    dynamic_dep_ids: tuple[str, ...] = ()

    def requires(self) -> sd.TaskStruct:
        return self.static_deps

    def run(self):
        # Yield dynamic deps first (as dynamic tasks with no further deps)
        if self.dynamic_dep_ids:
            dynamic_deps = tuple(
                IOBoundDynamicTask(key=dep_id, sleep_duration=self.sleep_duration)
                for dep_id in self.dynamic_dep_ids
            )
            yield dynamic_deps

        # Do the actual work
        start = time.perf_counter()
        time.sleep(self.sleep_duration)
        elapsed = time.perf_counter() - start
        self.output().save({"key": self.key, "elapsed": elapsed})


class CPUBoundDynamicTask(sd.AutoTask[dict]):
    """CPU-bound task with dynamic dependencies."""

    key: str  # User-provided label for this task instance (not the actual task.id)
    iterations: int = 100_000
    static_deps: tuple["CPUBoundDynamicTask", ...] = ()
    dynamic_dep_ids: tuple[str, ...] = ()

    def requires(self) -> sd.TaskStruct:
        return self.static_deps

    def run(self):
        # Yield dynamic deps first (as dynamic tasks with no further deps)
        if self.dynamic_dep_ids:
            dynamic_deps = tuple(
                CPUBoundDynamicTask(key=dep_id, iterations=self.iterations)
                for dep_id in self.dynamic_dep_ids
            )
            yield dynamic_deps

        # Do the actual work
        start = time.perf_counter()
        data = b"benchmark"
        for _ in range(self.iterations):
            data = hashlib.sha256(data).digest()
        elapsed = time.perf_counter() - start
        self.output().save({"key": self.key, "elapsed": elapsed})


class LightDynamicTask(sd.AutoTask[dict]):
    """Light task with dynamic dependencies."""

    key: str  # User-provided label for this task instance (not the actual task.id)
    static_deps: tuple["LightDynamicTask", ...] = ()
    dynamic_dep_ids: tuple[str, ...] = ()

    def requires(self) -> sd.TaskStruct:
        return self.static_deps

    def run(self):
        # Yield dynamic deps first (as dynamic tasks with no further deps)
        if self.dynamic_dep_ids:
            dynamic_deps = tuple(
                LightDynamicTask(key=dep_id) for dep_id in self.dynamic_dep_ids
            )
            yield dynamic_deps

        # Do the actual work
        start = time.perf_counter()
        _ = sum(range(100))
        elapsed = time.perf_counter() - start
        self.output().save({"key": self.key, "elapsed": elapsed})


# =============================================================================
# File I/O workload (real async I/O)
# =============================================================================


class FileIOTask(BenchmarkTask):
    """Real file I/O task using target's async interface.

    This demonstrates the actual async file I/O capabilities:
    - run(): Uses synchronous file operations
    - run_aio(): Uses async file operations via target.open_aio()

    Unlike sleep-based tests, this shows real async I/O benefits where
    the OS can overlap multiple file operations efficiently.
    """

    data_size: int = 10000  # bytes to write/read

    def _do_work(self) -> None:
        # Write data synchronously
        data = "x" * self.data_size
        with self.output().open("w") as f:
            f.write(data)

    async def run_aio(self) -> None:
        """Async implementation using target.open_aio() for real async file I/O."""
        start = time.perf_counter()

        # Write data asynchronously
        data = "x" * self.data_size
        async with self.output().open_aio("w") as f:
            await f.write(data)

        elapsed = time.perf_counter() - start
        # Re-save with timing info (overwrites the x's)
        self.output().save({"key": self.key, "elapsed": elapsed})


class FileIOReadWriteTask(BenchmarkTask):
    """File I/O task that does both write and read operations.

    Demonstrates async advantages with multiple I/O operations per task.
    """

    data_size: int = 50000  # bytes to write/read
    iterations: int = 3  # number of write/read cycles

    def _do_work(self) -> None:
        data = "y" * self.data_size
        for _ in range(self.iterations):
            with self.output().open("w") as f:
                f.write(data)
            with self.output().open("r") as f:
                _ = f.read()

    async def run_aio(self) -> None:
        """Async implementation with multiple write/read cycles."""
        start = time.perf_counter()

        data = "y" * self.data_size
        for _ in range(self.iterations):
            async with self.output().open_aio("w") as f:
                await f.write(data)
            async with self.output().open_aio("r") as f:
                _ = await f.read()

        elapsed = time.perf_counter() - start
        self.output().save({"key": self.key, "elapsed": elapsed})
