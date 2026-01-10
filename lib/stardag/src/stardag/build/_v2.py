"""Task build v2.

Unified build system with three entrypoints:
- build_sequential(): Sync API for debugging when no event loop running
- build_sequential_aio(): Async API for debugging with existing event loop
- build(): Async concurrent hybrid execution (the default)

Each task can be one of three run-types:
A. Sync-only, i.e., implements `def run(self)`
B. Async-only, i.e., implements `async def run_aio(self)`
C. Dual, i.e., implements both `def run(self)` and `async def run_aio(self)`

"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Generator, Literal, Protocol
from uuid import UUID

from stardag._task import (
    BaseTask,
    TaskStruct,
    _has_custom_run,
    _has_custom_run_aio,
    flatten_task_struct,
)
from stardag.build.registry import NoOpRegistry
from stardag.build.registry._base import RegistryABC

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


# =============================================================================
# Data Structures
# =============================================================================


class BuildExitStatus(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"


@dataclass
class TaskCount:
    discovered: int = 0
    previously_completed: int = 0
    succeeded: int = 0
    failed: int = 0

    @property
    def pending(self) -> int:
        return (
            self.discovered - self.previously_completed - self.succeeded - self.failed
        )


@dataclass
class BuildSummary:
    status: BuildExitStatus
    task_count: TaskCount
    error: BaseException | None = None


class FailMode(StrEnum):
    # Exit at first failure
    FAIL_FAST = "fail_fast"
    # Continue executing all tasks whose dependencies are met
    CONTINUE = "continue"


# =============================================================================
# Execution Mode Selection
# =============================================================================


class ExecutionMode(StrEnum):
    SYNC_BLOCKING = "sync_blocking"
    SYNC_THREAD = "sync_thread"
    SYNC_PROCESS = "sync_process"
    ASYNC_MAIN_LOOP = "async_main_loop"
    # Future: ASYNC_AIOMULTIPROCESS for async-in-subprocess
    # (to be implemented as separate TaskRunner)


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
    - Sync-only tasks: configurable (thread/blocking/process)
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
# Task Execution State
# =============================================================================


@dataclass
class TaskExecutionState:
    """Tracks the execution state of a task during build."""

    task: BaseTask
    # Static dependencies from requires()
    static_deps: list[BaseTask] = field(default_factory=list)
    # Dynamic dependencies discovered during execution
    dynamic_deps: list[BaseTask] = field(default_factory=list)
    # Generator if task has dynamic deps and is suspended
    generator: Generator[TaskStruct, None, None] | None = None
    # True when task execution has fully completed
    completed: bool = False
    # Exception if task failed
    exception: BaseException | None = None

    @property
    def all_deps(self) -> list[BaseTask]:
        return self.static_deps + self.dynamic_deps


# =============================================================================
# Task Runner Protocol and Implementation
# =============================================================================


class TaskRunnerABC(ABC):
    """Abstract base for task runners.

    Receives tasks and executes them according to some policy. The runner is
    responsible for:
    - Executing tasks in the appropriate context (async/thread/process)
    - Calling registry methods at appropriate times
    - Handling generator suspension for dynamic dependencies

    The runner is NOT responsible for dependency resolution - that's handled
    by the build() function.
    """

    @abstractmethod
    async def submit(self, task: BaseTask) -> None | TaskStruct | Exception:
        """Submit a task for execution.

        Args:
            task: The task to execute.

        Returns:
            - None: Task completed successfully with no dynamic dependencies.
            - TaskStruct: Task "suspended" because it yielded dynamic dependencies.
                The returned TaskStruct contains the discovered dependencies.
            - Exception: Task failed with the given exception.
        """
        ...

    @abstractmethod
    async def setup(self) -> None:
        """Setup any resources needed for the task runner (pools, etc.)."""
        ...

    @abstractmethod
    async def teardown(self) -> None:
        """Teardown any resources used by the task runner."""
        ...


class DefaultTaskRunner(TaskRunnerABC):
    """Default task runner with async, thread, and process pools.

    Routes tasks to appropriate execution context based on ExecutionModeSelector.
    Handles generator suspension for dynamic dependencies.

    Args:
        registry: Registry for tracking task execution.
        execution_mode_selector: Callable to select execution mode per task.
        max_async_workers: Maximum concurrent async tasks (semaphore-based).
        max_thread_workers: Maximum concurrent thread pool workers.
        max_process_workers: Maximum concurrent process pool workers.
    """

    def __init__(
        self,
        registry: RegistryABC | None = None,
        execution_mode_selector: ExecutionModeSelector | None = None,
        max_async_workers: int = 10,
        max_thread_workers: int = 10,
        max_process_workers: int | None = None,
    ) -> None:
        self.registry = registry or NoOpRegistry()
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
        self._suspended_generators: dict[UUID, Generator[TaskStruct, None, None]] = {}

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

    async def submit(self, task: BaseTask) -> None | TaskStruct | Exception:
        """Execute a task and return result."""
        # Check if we're resuming a suspended generator
        if task.id in self._suspended_generators:
            return await self._resume_generator(task)

        mode = self.execution_mode_selector(task)

        # Notify registry
        await self.registry.start_task_aio(task)

        try:
            result = await self._execute_task(task, mode)
            return await self._handle_result(task, result)
        except Exception as e:
            await self.registry.fail_task_aio(task, str(e))
            return e

    async def _execute_task(
        self, task: BaseTask, mode: ExecutionMode
    ) -> Generator[TaskStruct, None, None] | None:
        """Execute task in appropriate context."""
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
            # Note: For process execution, we need to ensure the task is picklable
            return await loop.run_in_executor(self._process_pool, task.run)

        elif mode == ExecutionMode.SYNC_BLOCKING:
            # Block the event loop (debugging only)
            return task.run()

        else:
            raise ValueError(f"Unsupported execution mode: {mode}")

    async def _handle_result(
        self, task: BaseTask, result: Generator[TaskStruct, None, None] | None
    ) -> None | TaskStruct | Exception:
        """Handle task execution result."""
        if result is None:
            # Task completed normally
            await self._complete_task(task)
            return None

        # Check if result is a generator (dynamic deps)
        if hasattr(result, "__next__"):
            return await self._handle_generator(task, result)

        # Shouldn't reach here
        await self._complete_task(task)
        return None

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
# Sequential Build Functions (for debugging)
# =============================================================================


def build_sequential(
    tasks: list[BaseTask],
    registry: RegistryABC | None = None,
    fail_mode: FailMode = FailMode.FAIL_FAST,
    dual_run_default: Literal["sync", "async"] = "sync",
) -> BuildSummary:
    """Sync API for building tasks sequentially.

    This is intended primarily for debugging and testing.

    Task execution policy:
    - Sync-only tasks: run via `run()`
    - Async-only tasks: run via `asyncio.run(run_aio())`. (Does not work if called
        from within an existing event loop.)
    - Dual tasks: run via `run()` if `dual_run_default=="sync"` (default), else
        (`dual_run_default=="async"`) via `asyncio.run(run_aio())`.

    Args:
        tasks: List of root tasks to build (and their dependencies)
        registry: Registry for tracking builds
        fail_mode: How to handle task failures
        dual_run_default: For dual tasks, prefer sync or async execution

    Returns:
        BuildSummary with status and task counts
    """
    registry = registry or NoOpRegistry()
    task_count = TaskCount()
    completion_cache: set[UUID] = set()
    failed_cache: set[UUID] = set()
    error: Exception | None = None

    # Discover all tasks
    all_tasks: dict[UUID, BaseTask] = {}

    def discover(task: BaseTask) -> None:
        if task.id in all_tasks:
            return
        all_tasks[task.id] = task
        for dep in flatten_task_struct(task.requires()):
            discover(dep)

    for root in tasks:
        discover(root)

    task_count.discovered = len(all_tasks)

    # Check initial completion
    for task in all_tasks.values():
        if task.complete():
            completion_cache.add(task.id)
            task_count.previously_completed += 1

    registry.start_build(root_tasks=tasks)

    def has_failed_dep(task: BaseTask) -> bool:
        """Check if any dependency has failed."""
        deps = flatten_task_struct(task.requires())
        return any(d.id in failed_cache for d in deps)

    try:
        # Build in topological order
        while True:
            # Find a task ready to run
            ready_task: BaseTask | None = None
            for task in all_tasks.values():
                if task.id in completion_cache or task.id in failed_cache:
                    continue
                # Skip tasks with failed dependencies
                if has_failed_dep(task):
                    continue
                deps = flatten_task_struct(task.requires())
                if all(d.id in completion_cache for d in deps):
                    ready_task = task
                    break

            if ready_task is None:
                # No more tasks can run - either all done or blocked by failures
                break

            # Execute the task
            try:
                _run_task_sequential(
                    ready_task,
                    completion_cache,
                    all_tasks,
                    registry,
                    dual_run_default,
                )
                task_count.succeeded += 1
            except Exception as e:
                task_count.failed += 1
                failed_cache.add(ready_task.id)
                error = e
                registry.fail_task(ready_task, str(e))
                if fail_mode == FailMode.FAIL_FAST:
                    raise

        registry.complete_build()
        return BuildSummary(
            status=BuildExitStatus.SUCCESS
            if error is None
            else BuildExitStatus.FAILURE,
            task_count=task_count,
            error=error,
        )

    except Exception as e:
        registry.fail_build(str(e))
        return BuildSummary(
            status=BuildExitStatus.FAILURE,
            task_count=task_count,
            error=e,
        )


def _run_task_sequential(
    task: BaseTask,
    completion_cache: set[UUID],
    all_tasks: dict[UUID, BaseTask],
    registry: RegistryABC,
    dual_run_default: Literal["sync", "async"],
) -> None:
    """Run a single task in sequential mode, handling dynamic deps."""
    registry.start_task(task)

    has_run = _has_custom_run(task)
    has_run_aio = _has_custom_run_aio(task)

    # Determine how to run
    use_async = False
    if has_run_aio and not has_run:
        # Async-only
        use_async = True
    elif has_run and has_run_aio:
        # Dual
        use_async = dual_run_default == "async"
    # else: sync-only, use_async = False

    # Execute
    if use_async:
        result = asyncio.run(task.run_aio())
    else:
        result = task.run()

    # Handle generator (dynamic deps)
    if result is not None and hasattr(result, "__next__"):
        gen = result
        while True:
            try:
                yielded = next(gen)
                dynamic_deps = flatten_task_struct(yielded)

                # Discover and build dynamic deps
                for dep in dynamic_deps:
                    if dep.id not in all_tasks:
                        all_tasks[dep.id] = dep
                        # Recursively discover
                        for sub_dep in flatten_task_struct(dep.requires()):
                            if sub_dep.id not in all_tasks:
                                all_tasks[sub_dep.id] = sub_dep

                    if dep.id not in completion_cache:
                        _run_task_sequential(
                            dep, completion_cache, all_tasks, registry, dual_run_default
                        )

            except StopIteration:
                break

    completion_cache.add(task.id)
    registry.complete_task(task)

    # Upload registry assets if any
    assets = task.registry_assets()
    if assets:
        registry.upload_task_assets(task, assets)


async def build_sequential_aio(
    tasks: list[BaseTask],
    registry: RegistryABC | None = None,
    fail_mode: FailMode = FailMode.FAIL_FAST,
    sync_run_default: Literal["thread", "blocking"] = "blocking",
) -> BuildSummary:
    """Async API for building tasks sequentially.

    This is intended primarily for debugging and testing.

    Task execution policy:
    - Sync-only tasks: runs *blocking* via `run()` in main event loop if
        `sync_run_default=="blocking"` (default), else (`sync_run_default=="thread"`)
        in thread pool.
    - Async-only tasks: run via `await run_aio()`.
    - Dual tasks: run via `await run_aio()`.

    Args:
        tasks: List of root tasks to build (and their dependencies)
        registry: Registry for tracking builds
        fail_mode: How to handle task failures
        sync_run_default: For sync-only tasks, block or use thread pool

    Returns:
        BuildSummary with status and task counts
    """
    registry = registry or NoOpRegistry()
    task_count = TaskCount()
    completion_cache: set[UUID] = set()
    failed_cache: set[UUID] = set()
    error: Exception | None = None

    # Discover all tasks
    all_tasks: dict[UUID, BaseTask] = {}

    def discover(task: BaseTask) -> None:
        if task.id in all_tasks:
            return
        all_tasks[task.id] = task
        for dep in flatten_task_struct(task.requires()):
            discover(dep)

    for root in tasks:
        discover(root)

    task_count.discovered = len(all_tasks)

    # Check initial completion
    for task in all_tasks.values():
        if await task.complete_aio():
            completion_cache.add(task.id)
            task_count.previously_completed += 1

    await registry.start_build_aio(root_tasks=tasks)

    def has_failed_dep(task: BaseTask) -> bool:
        """Check if any dependency has failed."""
        deps = flatten_task_struct(task.requires())
        return any(d.id in failed_cache for d in deps)

    try:
        # Build in topological order
        while True:
            # Find a task ready to run
            ready_task: BaseTask | None = None
            for task in all_tasks.values():
                if task.id in completion_cache or task.id in failed_cache:
                    continue
                # Skip tasks with failed dependencies
                if has_failed_dep(task):
                    continue
                deps = flatten_task_struct(task.requires())
                if all(d.id in completion_cache for d in deps):
                    ready_task = task
                    break

            if ready_task is None:
                # No more tasks can run - either all done or blocked by failures
                break

            # Execute the task
            try:
                await _run_task_sequential_aio(
                    ready_task,
                    completion_cache,
                    all_tasks,
                    registry,
                    sync_run_default,
                )
                task_count.succeeded += 1
            except Exception as e:
                task_count.failed += 1
                failed_cache.add(ready_task.id)
                error = e
                await registry.fail_task_aio(ready_task, str(e))
                if fail_mode == FailMode.FAIL_FAST:
                    raise

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


async def _run_task_sequential_aio(
    task: BaseTask,
    completion_cache: set[UUID],
    all_tasks: dict[UUID, BaseTask],
    registry: RegistryABC,
    sync_run_default: Literal["thread", "blocking"],
) -> None:
    """Run a single task in async sequential mode, handling dynamic deps."""
    await registry.start_task_aio(task)

    has_run = _has_custom_run(task)
    has_run_aio = _has_custom_run_aio(task)

    # Determine how to run
    if has_run_aio:
        # Async-only or Dual - use async
        result = await task.run_aio()
    elif has_run:
        # Sync-only
        if sync_run_default == "thread":
            result = await asyncio.to_thread(task.run)
        else:
            # Blocking - not recommended but useful for debugging
            result = task.run()
    else:
        raise ValueError(f"Task {task} has no run method")

    # Handle generator (dynamic deps)
    if result is not None and hasattr(result, "__next__"):
        gen = result
        while True:
            try:
                yielded = next(gen)
                dynamic_deps = flatten_task_struct(yielded)

                # Discover and build dynamic deps
                for dep in dynamic_deps:
                    if dep.id not in all_tasks:
                        all_tasks[dep.id] = dep
                        # Recursively discover
                        for sub_dep in flatten_task_struct(dep.requires()):
                            if sub_dep.id not in all_tasks:
                                all_tasks[sub_dep.id] = sub_dep

                    if dep.id not in completion_cache:
                        await _run_task_sequential_aio(
                            dep, completion_cache, all_tasks, registry, sync_run_default
                        )

            except StopIteration:
                break

    completion_cache.add(task.id)
    await registry.complete_task_aio(task)

    # Upload registry assets if any
    assets = task.registry_assets_aio()
    if assets:
        await registry.upload_task_assets_aio(task, assets)


# =============================================================================
# Concurrent Build Function (the default)
# =============================================================================


async def build(
    tasks: list[BaseTask],
    task_runner: TaskRunnerABC | None = None,
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

    Args:
        tasks: List of root tasks to build (and their dependencies)
        task_runner: TaskRunner for executing tasks (default: DefaultTaskRunner)
        fail_mode: How to handle task failures
        registry: Registry for tracking builds (passed to DefaultTaskRunner if
            task_runner not provided)

    Returns:
        BuildSummary with status and task counts
    """
    registry = registry or NoOpRegistry()

    if task_runner is None:
        task_runner = DefaultTaskRunner(registry=registry)

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


def build_sync(
    tasks: list[BaseTask],
    task_runner: TaskRunnerABC | None = None,
    fail_mode: FailMode = FailMode.FAIL_FAST,
    registry: RegistryABC | None = None,
) -> BuildSummary:
    """Sync wrapper for the async build() function.

    Convenience function for calling build() from synchronous code.
    """
    return asyncio.run(build(tasks, task_runner, fail_mode, registry))
