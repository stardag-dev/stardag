"""Concurrent build implementation.

This module contains:
- HybridConcurrentTaskExecutor: Routes tasks to async/thread/process based on policy
- build_aio(): Async concurrent build function
- build(): Sync wrapper for build_aio() (the default for production)
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from typing import Generator
from uuid import UUID

from stardag._task import (
    BaseTask,
    TaskStruct,
    flatten_task_struct,
)
from stardag.build._base import (
    BuildExitStatus,
    BuildSummary,
    DefaultExecutionModeSelector,
    ExecutionMode,
    ExecutionModeSelector,
    FailMode,
    TaskCount,
    TaskExecutionState,
    TaskExecutorABC,
)
from stardag.registry import RegistryABC, init_registry

logger = logging.getLogger(__name__)


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

        # Track suspended generators (task_id -> generator)
        # For in-process execution where we can suspend and resume
        self._suspended_generators: dict[UUID, Generator[TaskStruct, None, None]] = {}

        # Track tasks pending re-execution (task_id -> True)
        # For cross-process/remote execution: when task yields incomplete deps,
        # it's re-executed from scratch after deps complete (idempotent re-execution)
        self._pending_reexecution: set[UUID] = set()

    async def setup(self) -> None:
        """Initialize worker pools."""
        self._async_semaphore = asyncio.Semaphore(self.max_async_workers)
        self._thread_pool = ThreadPoolExecutor(max_workers=self.max_thread_workers)
        if self.max_process_workers:
            self._process_pool = ProcessPoolExecutor(
                max_workers=self.max_process_workers
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

    async def submit(self, task: BaseTask) -> None | TaskStruct | Exception:
        """Execute a task and return result.

        Note: This method does not make any registry calls. The build function
        is responsible for calling start_task, complete_task, and fail_task.
        """
        # Check if we're resuming a suspended generator (in-process dynamic deps)
        if task.id in self._suspended_generators:
            return self._resume_generator(task)

        # Check if task is pending re-execution (cross-process dynamic deps)
        # Task yielded incomplete deps, deps are now built, re-execute task
        if task.id in self._pending_reexecution:
            self._pending_reexecution.discard(task.id)

        mode = self.execution_mode_selector(task)

        try:
            result = await self._execute_task(task, mode)
            return self._handle_result(task, result)
        except Exception as e:
            return e

    async def _execute_task(
        self, task: BaseTask, mode: ExecutionMode
    ) -> Generator[TaskStruct, None, None] | TaskStruct | None:
        """Execute task in appropriate context.

        Returns:
            - None: Task completed with no dynamic dependencies.
            - Generator: Task has dynamic deps and is suspended in current process.
            - TaskStruct: Task has dynamic deps but cannot be suspended (e.g., ran
                in subprocess). Task will be re-executed when deps complete.
        """
        if mode == ExecutionMode.ASYNC_MAIN_LOOP:
            assert self._async_semaphore is not None
            async with self._async_semaphore:
                return await task.run_aio()

        elif mode == ExecutionMode.SYNC_THREAD:
            assert self._thread_pool is not None
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(self._thread_pool, task.run)

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

    def _handle_result(
        self,
        task: BaseTask,
        result: Generator[TaskStruct, None, None] | TaskStruct | None,
    ) -> None | TaskStruct:
        """Handle task execution result.

        Handles three cases:
        1. None: Task completed normally.
        2. Generator: Task has dynamic deps and is suspended (in-process execution).
           Store generator and return first yielded deps.
        3. TaskStruct: Task has dynamic deps but cannot be suspended (cross-process
           or remote execution). Return deps directly; task will be re-executed
           when deps complete (idempotent re-execution).

        Note: This method does not make any registry calls.
        """
        if result is None:
            # Task completed normally
            return None

        # Check if result is a generator (dynamic deps, in-process)
        # Use hasattr to check for generator protocol
        if hasattr(result, "__next__"):
            # Cast to Generator for type checker - we've verified it has __next__
            gen: Generator[TaskStruct, None, None] = result  # type: ignore[assignment]
            return self._handle_generator(task, gen)

        # Result is TaskStruct (dynamic deps from process/remote execution)
        # Task yielded these deps but they weren't complete, so the task
        # returned early (idempotent re-execution pattern). Mark task as pending
        # re-execution - it will be re-executed from scratch after deps complete.
        # On re-execution, the generator will drive forward past the yield
        # because the deps are now complete.
        self._pending_reexecution.add(task.id)
        # Cast to TaskStruct for type checker - we've verified it's not a generator
        task_struct: TaskStruct = result  # type: ignore[assignment]
        return task_struct

    def _handle_generator(
        self, task: BaseTask, gen: Generator[TaskStruct, None, None]
    ) -> None | TaskStruct:
        """Handle a generator from task execution."""
        try:
            yielded = next(gen)
            # Store generator for resumption
            self._suspended_generators[task.id] = gen
            return yielded
        except StopIteration:
            # Generator completed without yielding
            return None

    def _resume_generator(self, task: BaseTask) -> None | TaskStruct | Exception:
        """Resume a suspended generator."""
        gen = self._suspended_generators[task.id]

        try:
            yielded = next(gen)
            # Still more dynamic deps
            return yielded
        except StopIteration:
            # Generator completed
            del self._suspended_generators[task.id]
            return None
        except Exception as e:
            del self._suspended_generators[task.id]
            return e


# =============================================================================
# Concurrent Build Function
# =============================================================================


async def build_aio(
    tasks: list[BaseTask],
    task_executor: TaskExecutorABC | None = None,
    fail_mode: FailMode = FailMode.FAIL_FAST,
    registry: RegistryABC | None = None,
) -> BuildSummary:
    """Build tasks concurrently using hybrid async/thread/process execution.

    This is the main build function for production use. It:
    - Discovers all tasks in the DAG(s)
    - Schedules tasks for execution when dependencies are met
    - Handles dynamic dependencies via generator suspension
    - Supports multiple root tasks (built concurrently)
    - Routes tasks to async/thread/process based on ExecutionModeSelector
    - Manages all registry interactions (start/complete/fail task)

    Args:
        tasks: List of root tasks to build (and their dependencies)
        task_executor: TaskExecutor for executing tasks (default: HybridConcurrentTaskExecutor).
            Use RoutedTaskExecutor to route tasks to different executors (e.g., Modal).
        fail_mode: How to handle task failures
        registry: Registry for tracking builds (default: from init_registry())

    Returns:
        BuildSummary with status and task counts
    """
    # Determine registry: explicit > init_registry()
    if registry is None:
        registry = init_registry()
    logger.info(f"Using registry: {type(registry).__name__}")

    if task_executor is None:
        task_executor = HybridConcurrentTaskExecutor()

    task_count = TaskCount()
    completion_cache: set[UUID] = set()
    error: BaseException | None = None

    # Task execution states
    task_states: dict[UUID, TaskExecutionState] = {}
    # Events for completion signaling
    completion_events: dict[UUID, asyncio.Event] = {}
    # Currently executing tasks
    executing: set[UUID] = set()

    async def discover(task: BaseTask) -> None:
        """Recursively discover tasks, stopping at already-complete tasks.

        This optimization avoids traversing into dependency subgraphs that
        are already complete, which can significantly reduce discovery time
        for large DAGs with cached results.
        """
        if task.id in task_states:
            return

        static_deps = flatten_task_struct(task.requires())
        task_states[task.id] = TaskExecutionState(task=task, static_deps=static_deps)
        completion_events[task.id] = asyncio.Event()
        task_count.discovered += 1

        # Check if this task is already complete
        is_complete = await task.complete_aio()
        if is_complete:
            completion_cache.add(task.id)
            task_states[task.id].completed = True
            completion_events[task.id].set()
            task_count.previously_completed += 1
            # Don't recurse into deps - they're already built
            return

        # Task not complete - recurse into dependencies
        for dep in static_deps:
            await discover(dep)

    # Discover all tasks from roots
    for root in tasks:
        await discover(root)

    await registry.start_build_aio(root_tasks=tasks)
    await task_executor.setup()

    # Map task_id -> asyncio.Task for in-flight executions
    pending_futures: dict[UUID, asyncio.Task] = {}

    async def process_result(task: BaseTask, result: BaseException | TaskStruct | None):
        """Process a single task result."""
        nonlocal error
        state = task_states[task.id]

        if isinstance(result, BaseException):
            # Task failed - notify registry
            await registry.fail_task_aio(task, str(result))
            state.exception = result
            task_count.failed += 1
            error = result
            if fail_mode == FailMode.FAIL_FAST:
                raise result

        elif result is None:
            # Task completed - notify registry and upload assets
            await registry.complete_task_aio(task)
            assets = task.registry_assets_aio()
            if assets:
                await registry.upload_task_assets_aio(task, assets)
            state.completed = True
            completion_cache.add(task.id)
            completion_events[task.id].set()
            task_count.succeeded += 1

        else:
            # Dynamic deps returned (TaskStruct) - task is suspended
            dynamic_deps = flatten_task_struct(result)

            # Notify registry of dynamic deps discovery and suspension
            await registry.discover_dynamic_deps_aio(task, dynamic_deps)
            await registry.suspend_task_aio(task)

            # Discover any new dynamic deps (discover handles counting)
            for dep in dynamic_deps:
                if dep.id not in task_states:
                    await discover(dep)

            # Accumulate dynamic deps (don't overwrite)
            existing_dyn_ids = {d.id for d in state.dynamic_deps}
            for dep in dynamic_deps:
                if dep.id not in existing_dyn_ids:
                    state.dynamic_deps.append(dep)

    def find_ready_tasks() -> list[BaseTask]:
        """Find tasks that are ready to execute."""
        ready: list[BaseTask] = []
        for state in task_states.values():
            if state.completed or state.task.id in executing:
                continue
            if state.exception is not None:
                continue

            # Check all deps (static + dynamic) complete
            all_deps_complete = all(
                task_states[dep.id].completed for dep in state.all_deps
            )
            if all_deps_complete:
                ready.append(state.task)
                executing.add(state.task.id)
        return ready

    try:
        # Main build loop using as_completed pattern
        while True:
            # Check if all roots complete
            all_roots_complete = all(task_states[root.id].completed for root in tasks)
            if all_roots_complete:
                break

            # Find and submit ready tasks
            ready = find_ready_tasks()

            # Start any ready tasks
            for task in ready:
                state = task_states[task.id]
                if not state.started:
                    await registry.start_task_aio(task)
                    state.started = True
                elif state.dynamic_deps:
                    # Task was suspended waiting for dynamic deps, now resuming
                    await registry.resume_task_aio(task)
                # Create async task and track it
                async_task = asyncio.create_task(task_executor.submit(task))
                pending_futures[task.id] = async_task

            # If nothing is pending, check for deadlock or completion
            if not pending_futures:
                incomplete = [
                    s
                    for s in task_states.values()
                    if not s.completed and s.exception is None
                ]
                if incomplete:
                    # Check if all incomplete tasks are blocked by failed dependencies
                    def has_failed_dep(state: TaskExecutionState) -> bool:
                        for dep in state.all_deps:
                            dep_state = task_states[dep.id]
                            if dep_state.exception is not None:
                                return True
                        return False

                    truly_blocked = [s for s in incomplete if not has_failed_dep(s)]
                    if truly_blocked:
                        # Real deadlock - tasks blocked without failed deps
                        raise RuntimeError(
                            f"Deadlock: {len(truly_blocked)} tasks cannot proceed. "
                            f"Tasks: {[s.task.id for s in truly_blocked[:5]]}"
                        )
                    # All remaining tasks are blocked by failed deps - exit gracefully
                break

            # Wait for at least one task to complete
            done, _ = await asyncio.wait(
                pending_futures.values(), return_when=asyncio.FIRST_COMPLETED
            )

            # Process completed tasks
            for async_task in done:
                # Find which task this was
                task_id = None
                for tid, fut in pending_futures.items():
                    if fut is async_task:
                        task_id = tid
                        break
                assert task_id is not None

                # Remove from pending and executing
                del pending_futures[task_id]
                executing.discard(task_id)

                # Get result and process
                task = task_states[task_id].task
                try:
                    result = async_task.result()
                except Exception as e:
                    result = e
                await process_result(task, result)

        await registry.complete_build_aio()
        return BuildSummary(
            status=BuildExitStatus.SUCCESS
            if error is None
            else BuildExitStatus.FAILURE,
            task_count=task_count,
            error=error,
        )

    except Exception as e:
        await registry.fail_build_aio(str(e))
        return BuildSummary(
            status=BuildExitStatus.FAILURE,
            task_count=task_count,
            error=e,
        )

    finally:
        await task_executor.teardown()


# =============================================================================
# Convenience wrapper for sync callers
# =============================================================================


def build(
    tasks: list[BaseTask],
    task_executor: TaskExecutorABC | None = None,
    fail_mode: FailMode = FailMode.FAIL_FAST,
    registry: RegistryABC | None = None,
) -> BuildSummary:
    """Build tasks concurrently (sync wrapper for build_aio).

    This is the recommended entry point for building tasks from synchronous code.
    Wraps the async build_aio() function.

    Note:
        This function cannot be called from within an already running event loop.
        If you're in an async context (e.g., inside an async function, or using
        frameworks like Playwright, FastAPI, etc.), use `await build_aio()` instead.
    """
    try:
        return asyncio.run(build_aio(tasks, task_executor, fail_mode, registry))
    except RuntimeError as e:
        if "cannot be called from a running event loop" in str(e):
            raise RuntimeError(
                "build() cannot be used from within an already running event loop. "
                "Use 'await build_aio()' instead, or 'build_sequential()' if you "
                "need synchronous execution without an event loop."
            ) from e
        raise


__all__ = [
    "HybridConcurrentTaskExecutor",
    "build",
    "build_aio",
]
