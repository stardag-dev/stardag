"""Concurrent build: the hybrid executor, and ``build()``.

This module contains:
- HybridConcurrentTaskExecutor: routes tasks to async/thread/process pools
- build(): the sync wrapper of ``build_aio()`` (see ``_resident``), the
  default for production
"""

from __future__ import annotations

import asyncio
import contextvars
import inspect
import logging
import traceback as tb_module
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from enum import StrEnum
from typing import (
    AsyncGenerator,
    Generator,
    Literal,
    Protocol,
    Sequence,
    Union,
)
from uuid import UUID

from stardag import (
    BaseTask,
    TaskStruct,
    flatten_task_struct,
)
from stardag._core.base_task import (
    _has_custom_run,
    _has_custom_run_aio,
)
from stardag.build._base import (
    BuildSummary,
    ClaimConfig,
    FailMode,
    OnRegistryFailure,
    TaskExecutionError,
    TaskExecutorABC,
)
from stardag.build._concurrency import (
    ConcurrencyConfig,
    ConcurrencyLimiter,
)
from stardag.build._resident import build_aio
from stardag.build._session import LimitKeySelector
from stardag.registry import RegistryABC

logger = logging.getLogger(__name__)


# =============================================================================
# Execution Mode Selection
# =============================================================================


class ExecutionMode(StrEnum):
    """Execution mode for a task."""

    SYNC_BLOCKING = "sync_blocking"
    SYNC_THREAD = "sync_thread"
    SYNC_PROCESS = "sync_process"
    ASYNC_MAIN_LOOP = "async_main_loop"


class ExecutionModeSelector(Protocol):
    """Protocol for selecting execution mode for a given task.

    This can be used to customize how tasks are executed based on arbitrary criteria.

    Note: Users can implement custom selectors to enable task-specified execution
    preferences (e.g., via task class attributes) without framework changes. This
    extensibility is intentional - the framework doesn't prescribe how tasks should
    declare their preferred execution mode, but provides the mechanism to support it.
    """

    def __call__(self, task: BaseTask) -> ExecutionMode: ...


class DefaultExecutionModeSelector:
    """Selects execution mode based on the task's implemented run methods.

    Policy:
    - Async-only tasks: ASYNC_MAIN_LOOP
    - Dual tasks: ASYNC_MAIN_LOOP (prefer async)
    - Sync-only tasks: configurable via `sync_run_default`

    Args:
        sync_run_default: Execution mode for sync-only tasks.
            - "thread": Run in thread pool (default, good for I/O-bound)
            - "blocking": Run blocking in current thread (debugging)
            - "process": Run in process pool (good for CPU-bound)
    """

    def __init__(
        self,
        sync_run_default: Literal["thread", "blocking", "process"] = "thread",
    ) -> None:
        self.sync_run_default = sync_run_default

    def __call__(self, task: BaseTask) -> ExecutionMode:
        has_run = _has_custom_run(task)
        has_run_aio = _has_custom_run_aio(task)

        if has_run_aio:
            # Async-only or Dual task - use async
            return ExecutionMode.ASYNC_MAIN_LOOP
        elif has_run:
            # Sync-only task
            if self.sync_run_default == "thread":
                return ExecutionMode.SYNC_THREAD
            elif self.sync_run_default == "process":
                return ExecutionMode.SYNC_PROCESS
            else:
                return ExecutionMode.SYNC_BLOCKING
        else:
            raise ValueError(f"Task {task} has no run method.")


# =============================================================================
# Helper for process pool execution
# =============================================================================


def _run_task_in_process(task: BaseTask) -> TaskStruct | None:
    """Execute task in subprocess, respecting dynamic deps contract.

    This function is called in a subprocess via ProcessPoolExecutor.
    Since generators cannot be pickled, we implement idempotent re-execution:

    1. Execute task.run() to get the generator
    2. Drive generator forward ONLY when yielded deps are COMPLETE
    3. If deps aren't complete, return them as TaskStruct (to be built)
    4. Task will be re-executed from scratch after deps complete
    5. On re-execution, previously incomplete deps should now be complete,
       so generator continues past those yields
    6. Repeat until generator completes

    CONTRACT: The generator is only advanced past a yield when ALL tasks
    yielded in that step are complete. This ensures the task can rely on
    yielded deps being complete after yield returns.

    The build's settings reach the subprocess as environment variables: the
    pool is spawned while they are applied (see ``resident_settings``).

    Args:
        task: The task to execute.

    Returns:
        - None: Task completed (generator finished or no dynamic deps).
        - TaskStruct: Task yielded deps that are NOT complete. These need
            to be built, then the task will be re-executed.
    """
    result = task.run()

    if result is None:
        return None

    # Check if result is a generator (has __next__ method)
    gen = result if hasattr(result, "__next__") else None
    if gen is not None:
        try:
            while True:
                yielded = next(gen)  # type: ignore[arg-type]
                deps = flatten_task_struct(yielded)

                # Check if ALL yielded deps are complete
                # TODO: For many deps with remote targets, check completion concurrently
                # by starting an event loop and using asyncio.gather with complete_aio()
                incomplete_deps = [dep for dep in deps if not dep.complete()]

                if incomplete_deps:
                    # Deps not complete - return them to be built
                    # Task will be re-executed after these are built
                    return tuple(deps)

                # All deps complete - continue to next yield
                # (generator will continue past the yield point)

        except StopIteration:
            # Generator completed - task is done
            pass

        return None

    # Result is already a TaskStruct (shouldn't happen normally, but handle it)
    # This can occur if task.run() returns a tuple/list directly
    return result  # type: ignore[return-value]


# =============================================================================
# Task Executor Implementation
# =============================================================================


class HybridConcurrentTaskExecutor(TaskExecutorABC):
    """Task executor with async, thread, and process pools.

    Routes tasks to appropriate execution context based on ExecutionModeSelector.
    Handles generator suspension for dynamic dependencies.

    Note: This executor does not handle registry calls - those are managed by
    the build() function. The executor only executes tasks and returns results.

    For routing tasks to different executors (e.g., some to Modal, some local),
    use RoutedTaskExecutor to compose multiple executors.

    Alternative: For fully async multiprocessing without thread pools, one could
    implement an AIOMultiprocessingTaskExecutor using libraries like aiomultiprocess.

    Args:
        execution_mode_selector: Callable to select execution mode per task.
        max_async_workers: Maximum concurrent async tasks (semaphore-based).
        max_thread_workers: Maximum concurrent thread pool workers.
        max_process_workers: Maximum concurrent process pool workers.
    """

    def __init__(
        self,
        execution_mode_selector: ExecutionModeSelector | None = None,
        max_async_workers: int = 10,
        max_thread_workers: int = 10,
        max_process_workers: int | None = None,
    ) -> None:
        self.execution_mode_selector = (
            execution_mode_selector or DefaultExecutionModeSelector()
        )
        self.max_async_workers = max_async_workers
        self.max_thread_workers = max_thread_workers
        self.max_process_workers = max_process_workers

        # Pools - initialized in setup()
        self._async_semaphore: asyncio.Semaphore | None = None
        self._thread_pool: ThreadPoolExecutor | None = None
        self._process_pool: ProcessPoolExecutor | None = None

        # Track suspended generators (task_id -> sync or async generator)
        # For in-process execution where we can suspend and resume
        self._suspended_generators: dict[
            UUID,
            Union[
                Generator[TaskStruct, None, None],
                AsyncGenerator[TaskStruct, None],
            ],
        ] = {}

        # Track tasks pending re-execution (task_id -> True)
        # For cross-process/remote execution: when task yields incomplete deps,
        # it's re-executed from scratch after deps complete (idempotent re-execution)
        self._pending_reexecution: set[UUID] = set()

    async def setup(self) -> None:
        """Initialize worker pools."""
        import multiprocessing as mp

        self._async_semaphore = asyncio.Semaphore(self.max_async_workers)
        self._thread_pool = ThreadPoolExecutor(max_workers=self.max_thread_workers)
        if self.max_process_workers:
            # Use 'spawn' explicitly for cross-platform compatibility.
            # Python 3.14 changed the default from 'fork' to 'forkserver' on Linux,
            # which can cause issues with environment variable inheritance.
            # 'spawn' is the safest option and works consistently across platforms.
            self._process_pool = ProcessPoolExecutor(
                max_workers=self.max_process_workers,
                mp_context=mp.get_context("spawn"),
            )

    async def teardown(self) -> None:
        """Shutdown worker pools."""
        if self._thread_pool:
            self._thread_pool.shutdown(wait=True)
            self._thread_pool = None
        if self._process_pool:
            self._process_pool.shutdown(wait=True)
            self._process_pool = None
        self._async_semaphore = None
        self._suspended_generators.clear()
        self._pending_reexecution.clear()

    async def submit(self, task: BaseTask) -> None | TaskStruct | TaskExecutionError:
        """Execute a task and return result.

        Note: This method does not make any registry calls. The build function
        is responsible for calling start_task, complete_task, and fail_task.
        """
        # Check if we're resuming a suspended generator (in-process dynamic deps)
        if task.id in self._suspended_generators:
            gen = self._suspended_generators[task.id]
            if hasattr(gen, "__anext__"):
                return await self._resume_generator_aio(task)
            return self._resume_generator(task)

        # Check if task is pending re-execution (cross-process dynamic deps)
        # Task yielded incomplete deps, deps are now built, re-execute task
        if task.id in self._pending_reexecution:
            self._pending_reexecution.discard(task.id)

        mode = self.execution_mode_selector(task)

        try:
            result = await self._execute_task(task, mode)
            return await self._handle_result(task, result)
        except Exception as e:
            return TaskExecutionError(
                exception=e,
                traceback="".join(tb_module.format_exception(e)),
            )

    async def _execute_task(
        self, task: BaseTask, mode: ExecutionMode
    ) -> (
        Generator[TaskStruct, None, None]
        | AsyncGenerator[TaskStruct, None]
        | TaskStruct
        | None
    ):
        """Execute task in appropriate context.

        Returns:
            - None: Task completed with no dynamic dependencies.
            - Generator: Task has sync dynamic deps and is suspended in-process.
            - AsyncGenerator: Task has async dynamic deps and is suspended in-process.
            - TaskStruct: Task has dynamic deps but cannot be suspended (e.g., ran
                in subprocess). Task will be re-executed when deps complete.
        """
        if mode == ExecutionMode.ASYNC_MAIN_LOOP:
            assert self._async_semaphore is not None
            async with self._async_semaphore:
                # Async generator functions (dynamic deps in run_aio) must not
                # be awaited — calling the bound method returns the generator.
                if inspect.isasyncgenfunction(type(task).run_aio):
                    return task.run_aio()  # type: ignore[return-value]
                return await task.run_aio()

        elif mode == ExecutionMode.SYNC_THREAD:
            assert self._thread_pool is not None
            loop = asyncio.get_running_loop()
            # ``run_in_executor`` does not carry the calling context into
            # the pool thread; run it in a copy of this one, so the build
            # context (and anything a task reads from context) is the same.
            context = contextvars.copy_context()
            return await loop.run_in_executor(self._thread_pool, context.run, task.run)

        elif mode == ExecutionMode.SYNC_PROCESS:
            assert self._process_pool is not None
            loop = asyncio.get_running_loop()
            # Use helper that handles generators by collecting all yielded deps
            # and returning TaskStruct (which IS picklable, unlike generators)
            return await loop.run_in_executor(
                self._process_pool, _run_task_in_process, task
            )

        elif mode == ExecutionMode.SYNC_BLOCKING:
            # Block the event loop (debugging only)
            return task.run()

        else:
            raise ValueError(f"Unsupported execution mode: {mode}")

    async def _handle_result(
        self,
        task: BaseTask,
        result: Generator[TaskStruct, None, None]
        | AsyncGenerator[TaskStruct, None]
        | TaskStruct
        | None,
    ) -> None | TaskStruct:
        """Handle task execution result.

        Handles four cases:
        1. None: Task completed normally.
        2. Generator: Task has sync dynamic deps and is suspended (in-process).
           Store generator and return first yielded deps.
        3. AsyncGenerator: Task has async dynamic deps and is suspended (in-process).
           Store generator and return first yielded deps.
        4. TaskStruct: Task has dynamic deps but cannot be suspended (cross-process
           or remote execution). Return deps directly; task will be re-executed
           when deps complete (idempotent re-execution).

        Note: This method does not make any registry calls.
        """
        if result is None:
            return None

        # Async generator takes precedence (also has __aiter__, not __next__)
        if hasattr(result, "__anext__"):
            agen: AsyncGenerator[TaskStruct, None] = result  # type: ignore[assignment]
            return await self._handle_generator_aio(task, agen)

        if hasattr(result, "__next__"):
            gen: Generator[TaskStruct, None, None] = result  # type: ignore[assignment]
            return self._handle_generator(task, gen)

        # Result is TaskStruct (dynamic deps from process/remote execution)
        # Task yielded these deps but they weren't complete, so the task
        # returned early (idempotent re-execution pattern). Mark task as pending
        # re-execution - it will be re-executed from scratch after deps complete.
        # On re-execution, the generator will drive forward past the yield
        # because the deps are now complete.
        self._pending_reexecution.add(task.id)
        task_struct: TaskStruct = result  # type: ignore[assignment]
        return task_struct

    def _handle_generator(
        self, task: BaseTask, gen: Generator[TaskStruct, None, None]
    ) -> None | TaskStruct:
        """Handle a sync generator from task execution."""
        try:
            yielded = next(gen)
            self._suspended_generators[task.id] = gen
            return yielded
        except StopIteration:
            return None

    async def _handle_generator_aio(
        self, task: BaseTask, agen: AsyncGenerator[TaskStruct, None]
    ) -> None | TaskStruct:
        """Handle an async generator from task execution."""
        try:
            yielded = await agen.__anext__()
            self._suspended_generators[task.id] = agen
            return yielded
        except StopAsyncIteration:
            return None

    def _resume_generator(
        self, task: BaseTask
    ) -> None | TaskStruct | TaskExecutionError:
        """Resume a suspended sync generator."""
        gen = self._suspended_generators[task.id]

        try:
            yielded = next(gen)  # type: ignore[arg-type]
            return yielded
        except StopIteration:
            del self._suspended_generators[task.id]
            return None
        except Exception as e:
            del self._suspended_generators[task.id]
            return TaskExecutionError(
                exception=e,
                traceback="".join(tb_module.format_exception(e)),
            )

    async def _resume_generator_aio(
        self, task: BaseTask
    ) -> None | TaskStruct | TaskExecutionError:
        """Resume a suspended async generator."""
        agen = self._suspended_generators[task.id]

        try:
            yielded = await agen.__anext__()  # type: ignore[union-attr]
            return yielded
        except StopAsyncIteration:
            del self._suspended_generators[task.id]
            return None
        except Exception as e:
            del self._suspended_generators[task.id]
            return TaskExecutionError(
                exception=e,
                traceback="".join(tb_module.format_exception(e)),
            )


# =============================================================================
# Convenience wrapper for sync callers
# =============================================================================


def build(
    tasks: Sequence[BaseTask] | BaseTask,
    task_executor: TaskExecutorABC | None = None,
    fail_mode: FailMode = FailMode.FAIL_FAST,
    registry: RegistryABC | None = None,
    max_concurrent_discover: int = 50,
    resume_build_id: UUID | None = None,
    register_all: bool = False,
    on_registry_failure: OnRegistryFailure = "raise",
    concurrency_config: ConcurrencyConfig | None = None,
    concurrency_limiter: ConcurrencyLimiter | None = None,
    claim_config: ClaimConfig | None = None,
    settings: dict[str, str] | None = None,
    limit_key_selector: LimitKeySelector | None = None,
    description: str | None = None,
) -> BuildSummary:
    """Build tasks concurrently (sync wrapper for build_aio).

    This is the recommended entry point for building tasks from synchronous code.
    Wraps the async build_aio() function; see it for the arguments.

    Note:
        This function cannot be called from within an already running event loop.
        If you're in an async context (e.g., inside an async function, or using
        frameworks like Playwright, FastAPI, etc.), use `await build_aio()` instead.
    """
    try:
        return asyncio.run(
            build_aio(
                tasks,
                task_executor=task_executor,
                fail_mode=fail_mode,
                registry=registry,
                max_concurrent_discover=max_concurrent_discover,
                resume_build_id=resume_build_id,
                register_all=register_all,
                on_registry_failure=on_registry_failure,
                concurrency_config=concurrency_config,
                concurrency_limiter=concurrency_limiter,
                claim_config=claim_config,
                settings=settings,
                limit_key_selector=limit_key_selector,
                description=description,
            )
        )
    except RuntimeError as e:
        if "cannot be called from a running event loop" in str(e):
            raise RuntimeError(
                "build() cannot be used from within an already running event loop. "
                "Use 'await build_aio()' instead, or 'build_sequential()' if you "
                "need synchronous execution without an event loop."
            ) from e
        raise


__all__ = [
    "ConcurrencyConfig",
    "ConcurrencyLimiter",
    "DefaultExecutionModeSelector",
    "ExecutionMode",
    "ExecutionModeSelector",
    "HybridConcurrentTaskExecutor",
    "build",
    "build_aio",
]
