"""Concurrent build implementation.

This module contains:
- HybridConcurrentTaskRunner: Routes tasks to async/thread/process based on policy
- build_aio(): Async concurrent build function
- build(): Sync wrapper for build_aio() (the default for production)
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from typing import TYPE_CHECKING, Generator
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
    DefaultGlobalLockSelector,
    ExecutionMode,
    ExecutionModeSelector,
    FailMode,
    GlobalConcurrencyLockManager,
    GlobalLockConfig,
    GlobalLockSelector,
    LockAcquisitionStatus,
    RunWrapper,
    TaskCount,
    TaskExecutionState,
    TaskRunnerABC,
)
from stardag.registry import NoOpRegistry, RegistryABC

if TYPE_CHECKING:
    pass

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
# Task Runner Implementation
# =============================================================================


class HybridConcurrentTaskRunner(TaskRunnerABC):
    """Task runner with async, thread, and process pools.

    Routes tasks to appropriate execution context based on ExecutionModeSelector.
    Handles generator suspension for dynamic dependencies.
    Optionally uses global concurrency locks for distributed execution.

    Args:
        registry: Registry for tracking task execution.
        run_wrapper: Optional RunWrapper for delegating task execution (e.g., Modal).
            When provided, all tasks are executed via the wrapper (ignoring
            execution_mode_selector). When None, tasks run locally using
            DefaultRunWrapper with execution mode selection.
        execution_mode_selector: Callable to select execution mode per task.
            Only used when run_wrapper is None.
        max_async_workers: Maximum concurrent async tasks (semaphore-based).
        max_thread_workers: Maximum concurrent thread pool workers.
        max_process_workers: Maximum concurrent process pool workers.
        global_lock_manager: Optional lock manager for distributed locking.
        global_lock_selector: Selector for which tasks should use global lock.
        global_lock_config: Configuration for global locking behavior.
    """

    def __init__(
        self,
        registry: RegistryABC | None = None,
        run_wrapper: RunWrapper | None = None,
        execution_mode_selector: ExecutionModeSelector | None = None,
        max_async_workers: int = 10,
        max_thread_workers: int = 10,
        max_process_workers: int | None = None,
        global_lock_manager: GlobalConcurrencyLockManager | None = None,
        global_lock_selector: GlobalLockSelector | None = None,
        global_lock_config: GlobalLockConfig | None = None,
    ) -> None:
        self.registry = registry or NoOpRegistry()
        self.run_wrapper = run_wrapper
        self.execution_mode_selector = (
            execution_mode_selector or DefaultExecutionModeSelector()
        )
        self.max_async_workers = max_async_workers
        self.max_thread_workers = max_thread_workers
        self.max_process_workers = max_process_workers

        # Global lock configuration
        self._global_lock_manager = global_lock_manager
        self._global_lock_config = global_lock_config or GlobalLockConfig()
        self._global_lock_selector = global_lock_selector or DefaultGlobalLockSelector(
            self._global_lock_config
        )

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
        """Execute a task and return result."""
        # Check if we're resuming a suspended generator (in-process dynamic deps)
        if task.id in self._suspended_generators:
            return await self._resume_generator(task)

        # Check if task is pending re-execution (cross-process dynamic deps)
        # Task yielded incomplete deps, deps are now built, re-execute task
        is_reexecution = task.id in self._pending_reexecution
        if is_reexecution:
            self._pending_reexecution.discard(task.id)

        # Check if global lock should be used for this task
        use_lock = self._global_lock_manager is not None and self._global_lock_selector(
            task
        )

        if use_lock:
            return await self._submit_with_lock(task, is_reexecution)
        else:
            return await self._submit_without_lock(task, is_reexecution)

    async def _submit_without_lock(
        self, task: BaseTask, is_reexecution: bool
    ) -> None | TaskStruct | Exception:
        """Execute task without global lock."""
        if not is_reexecution:
            await self.registry.start_task_aio(task)

        mode = self.execution_mode_selector(task)

        try:
            result = await self._execute_task(task, mode)
            return await self._handle_result(task, result)
        except Exception as e:
            await self.registry.fail_task_aio(task, str(e))
            return e

    async def _submit_with_lock(
        self, task: BaseTask, is_reexecution: bool
    ) -> None | TaskStruct | Exception:
        """Execute task with global concurrency lock.

        Flow:
        1. Acquire lock (includes check for task completion in registry)
        2. If ALREADY_COMPLETED: retry task.complete_aio() until True or timeout
        3. If ACQUIRED: execute task, mark completed on success
        4. If HELD_BY_OTHER or CONCURRENCY_LIMIT_REACHED: wait and retry
        5. If ERROR: return exception
        """
        assert self._global_lock_manager is not None
        task_id = str(task.id)
        config = self._global_lock_config

        # Track start time for timeout
        start_time = asyncio.get_event_loop().time()
        timeout = config.lock_wait_timeout_seconds
        poll_interval = config.lock_wait_poll_interval_seconds

        # Track last acquisition result for logging
        last_status: LockAcquisitionStatus | None = None

        while True:
            async with self._global_lock_manager.lock(task_id) as handle:
                result = handle.result
                last_status = result.status

                if result.status == LockAcquisitionStatus.ALREADY_COMPLETED:
                    # Task completed elsewhere - wait for eventual consistency
                    completed = await self._wait_for_completion(task)
                    if completed:
                        logger.debug(
                            f"Task {task_id} already completed (confirmed via retry)"
                        )
                        return None
                    else:
                        # Timeout waiting - log warning but don't fail
                        # The task may still become visible eventually
                        logger.warning(
                            f"Task {task_id} marked complete in registry but "
                            f"target not visible after retry timeout"
                        )
                        return None

                if result.status == LockAcquisitionStatus.ACQUIRED:
                    # Lock acquired - execute task
                    if not is_reexecution:
                        await self.registry.start_task_aio(task)

                    mode = self.execution_mode_selector(task)

                    try:
                        exec_result = await self._execute_task(task, mode)
                        handled = await self._handle_result(task, exec_result)

                        # Mark completed if task finished successfully
                        if handled is None:
                            handle.mark_completed()

                        return handled

                    except Exception as e:
                        await self.registry.fail_task_aio(task, str(e))
                        return e

                if result.status == LockAcquisitionStatus.ERROR:
                    msg = f"Failed to acquire lock for task {task_id}: {result.error_message}"
                    logger.error(msg)
                    return Exception(msg)

                # HELD_BY_OTHER or CONCURRENCY_LIMIT_REACHED
                # Check if we should wait and retry
                if timeout is None:
                    # No waiting configured - fail immediately
                    msg = f"Lock for task {task_id} unavailable: {result.status}"
                    logger.warning(msg)
                    return Exception(msg)

            # Outside the context manager (lock released if we had it)
            # We only get here for HELD_BY_OTHER or CONCURRENCY_LIMIT_REACHED
            # (other statuses return from within the context manager)

            # Check timeout before waiting (timeout is guaranteed not None here
            # because we check `if timeout is None` above and return)
            assert timeout is not None
            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed >= timeout:
                msg = (
                    f"Timeout waiting for lock on task {task_id} "
                    f"(status: {last_status}, waited {elapsed:.1f}s)"
                )
                logger.warning(msg)
                return Exception(msg)

            # Check if task completed while we were waiting
            # (another process may have finished it)
            if await task.complete_aio():
                logger.debug(
                    f"Task {task_id} completed by another process while waiting for lock"
                )
                return None

            # Wait before retrying
            logger.debug(
                f"Lock for task {task_id} unavailable ({last_status}), "
                f"retrying in {poll_interval}s..."
            )
            await asyncio.sleep(poll_interval)

    async def _wait_for_completion(self, task: BaseTask) -> bool:
        """Wait for task completion with retry for eventual consistency.

        When the lock manager reports a task is already completed (in the registry),
        but local target.exists() returns False, retry until visible or timeout.

        This handles S3 eventual consistency where a newly written object may not
        be immediately visible to all readers.
        """
        config = self._global_lock_config
        timeout = config.completion_retry_timeout_seconds
        interval = config.completion_retry_interval_seconds

        start = asyncio.get_event_loop().time()
        while True:
            if await task.complete_aio():
                return True

            elapsed = asyncio.get_event_loop().time() - start
            if elapsed >= timeout:
                return False

            await asyncio.sleep(interval)

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
        # If run_wrapper is provided, use it for all execution
        if self.run_wrapper is not None:
            assert self._async_semaphore is not None
            async with self._async_semaphore:
                return await self.run_wrapper.run(task)

        # Otherwise, use execution mode selector with local pools
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

    async def _handle_result(
        self,
        task: BaseTask,
        result: Generator[TaskStruct, None, None] | TaskStruct | None,
    ) -> None | TaskStruct | Exception:
        """Handle task execution result.

        Handles three cases:
        1. None: Task completed normally.
        2. Generator: Task has dynamic deps and is suspended (in-process execution).
           Store generator and return first yielded deps.
        3. TaskStruct: Task has dynamic deps but cannot be suspended (cross-process
           or remote execution). Return deps directly; task will be re-executed
           when deps complete (idempotent re-execution).
        """
        if result is None:
            # Task completed normally
            await self._complete_task(task)
            return None

        # Check if result is a generator (dynamic deps, in-process)
        # Use hasattr to check for generator protocol
        if hasattr(result, "__next__"):
            # Cast to Generator for type checker - we've verified it has __next__
            gen: Generator[TaskStruct, None, None] = result  # type: ignore[assignment]
            return await self._handle_generator(task, gen)

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

    async def _handle_generator(
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
            await self._complete_task(task)
            return None

    async def _resume_generator(self, task: BaseTask) -> None | TaskStruct | Exception:
        """Resume a suspended generator."""
        gen = self._suspended_generators[task.id]

        try:
            yielded = next(gen)
            # Still more dynamic deps
            return yielded
        except StopIteration:
            # Generator completed
            del self._suspended_generators[task.id]
            await self._complete_task(task)
            return None
        except Exception as e:
            del self._suspended_generators[task.id]
            await self.registry.fail_task_aio(task, str(e))
            return e

    async def _complete_task(self, task: BaseTask) -> None:
        """Mark task as completed in registry."""
        await self.registry.complete_task_aio(task)

        # Upload registry assets if any
        assets = task.registry_assets_aio()
        if assets:
            await self.registry.upload_task_assets_aio(task, assets)


# =============================================================================
# Concurrent Build Function
# =============================================================================


async def build_aio(
    tasks: list[BaseTask],
    task_runner: TaskRunnerABC | None = None,
    fail_mode: FailMode = FailMode.FAIL_FAST,
    registry: RegistryABC | None = None,
    run_wrapper: RunWrapper | None = None,
    global_lock_manager: GlobalConcurrencyLockManager | None = None,
    global_lock_config: GlobalLockConfig | None = None,
) -> BuildSummary:
    """Build tasks concurrently using hybrid async/thread/process execution.

    This is the main build function for production use. It:
    - Discovers all tasks in the DAG(s)
    - Schedules tasks for execution when dependencies are met
    - Handles dynamic dependencies via generator suspension
    - Supports multiple root tasks (built concurrently)
    - Routes tasks to async/thread/process based on ExecutionModeSelector
    - Optionally uses global concurrency locks for distributed execution

    Args:
        tasks: List of root tasks to build (and their dependencies)
        task_runner: TaskRunner for executing tasks (default: HybridConcurrentTaskRunner)
        fail_mode: How to handle task failures
        registry: Registry for tracking builds (passed to HybridConcurrentTaskRunner if
            task_runner not provided)
        run_wrapper: Optional RunWrapper for delegating task execution (e.g., Modal).
            Only used when task_runner is None.
        global_lock_manager: Optional lock manager for distributed locking.
            Only used when task_runner is None.
        global_lock_config: Configuration for global locking (enabled, retry settings).
            Only used when task_runner is None.

    Returns:
        BuildSummary with status and task counts
    """
    registry = registry or NoOpRegistry()

    if task_runner is None:
        task_runner = HybridConcurrentTaskRunner(
            registry=registry,
            run_wrapper=run_wrapper,
            global_lock_manager=global_lock_manager,
            global_lock_config=global_lock_config,
        )

    task_count = TaskCount()
    completion_cache: set[UUID] = set()
    error: BaseException | None = None

    # Task execution states
    task_states: dict[UUID, TaskExecutionState] = {}
    # Events for completion signaling
    completion_events: dict[UUID, asyncio.Event] = {}
    # Currently executing tasks
    executing: set[UUID] = set()
    # Lock for shared state
    lock = asyncio.Lock()

    def discover(task: BaseTask) -> None:
        """Recursively discover all tasks."""
        if task.id in task_states:
            return
        static_deps = flatten_task_struct(task.requires())
        task_states[task.id] = TaskExecutionState(task=task, static_deps=static_deps)
        completion_events[task.id] = asyncio.Event()
        for dep in static_deps:
            discover(dep)

    # Discover all tasks from roots
    for root in tasks:
        discover(root)

    task_count.discovered = len(task_states)

    # Check initial completion (parallel for efficiency with remote targets)
    states_list = list(task_states.values())
    completion_results = await asyncio.gather(
        *[state.task.complete_aio() for state in states_list]
    )
    for state, is_complete in zip(states_list, completion_results):
        if is_complete:
            completion_cache.add(state.task.id)
            state.completed = True
            completion_events[state.task.id].set()
            task_count.previously_completed += 1

    await registry.start_build_aio(root_tasks=tasks)
    await task_runner.setup()

    try:
        # Main build loop
        while True:
            async with lock:
                # Check if all roots complete
                all_roots_complete = all(
                    task_states[root.id].completed for root in tasks
                )
                if all_roots_complete:
                    break

                # Find tasks ready to execute
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

            if not ready and not executing:
                # Check if there are incomplete tasks
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

            # Submit ready tasks concurrently
            if ready:
                results = await asyncio.gather(
                    *[task_runner.submit(task) for task in ready],
                    return_exceptions=True,
                )

                async with lock:
                    for task, result in zip(ready, results):
                        executing.discard(task.id)
                        state = task_states[task.id]

                        if isinstance(result, BaseException):
                            # Task failed
                            state.exception = result
                            task_count.failed += 1
                            error = result
                            if fail_mode == FailMode.FAIL_FAST:
                                raise result

                        elif result is None:
                            # Task completed
                            state.completed = True
                            completion_cache.add(task.id)
                            completion_events[task.id].set()
                            task_count.succeeded += 1

                        else:
                            # Dynamic deps returned (TaskStruct)
                            dynamic_deps = flatten_task_struct(result)

                            # Discover any new dynamic deps
                            for dep in dynamic_deps:
                                if dep.id not in task_states:
                                    discover(dep)
                                    task_count.discovered += 1

                            # Accumulate dynamic deps (don't overwrite)
                            existing_dyn_ids = {d.id for d in state.dynamic_deps}
                            for dep in dynamic_deps:
                                if dep.id not in existing_dyn_ids:
                                    state.dynamic_deps.append(dep)

                            # Note: Don't add to ready/executing here - let the
                            # next iteration find it via the normal ready check.
                            # This avoids a bug where tasks get marked as executing
                            # but never actually submitted.

            # Small yield to allow other coroutines to run
            await asyncio.sleep(0)

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
        await task_runner.teardown()


# =============================================================================
# Convenience wrapper for sync callers
# =============================================================================


def build(
    tasks: list[BaseTask],
    task_runner: TaskRunnerABC | None = None,
    fail_mode: FailMode = FailMode.FAIL_FAST,
    registry: RegistryABC | None = None,
    run_wrapper: RunWrapper | None = None,
    global_lock_manager: GlobalConcurrencyLockManager | None = None,
    global_lock_config: GlobalLockConfig | None = None,
) -> BuildSummary:
    """Build tasks concurrently (sync wrapper for build_aio).

    This is the main entry point for building tasks in production.
    Wraps the async build_aio() function for use from synchronous code.

    See build_aio() for full documentation of parameters.
    """
    return asyncio.run(
        build_aio(
            tasks,
            task_runner,
            fail_mode,
            registry,
            run_wrapper,
            global_lock_manager,
            global_lock_config,
        )
    )


__all__ = [
    "HybridConcurrentTaskRunner",
    "build",
    "build_aio",
]
