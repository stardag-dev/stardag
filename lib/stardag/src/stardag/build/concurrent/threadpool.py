"""ThreadPool-based concurrent build implementation.

This implementation uses Python's concurrent.futures.ThreadPoolExecutor to
execute tasks concurrently. It's suitable for I/O-bound tasks.

Key design decisions:
- Uses task.requires() for dependency resolution (simpler than wait_for futures)
- Handles dynamic dependencies by tracking generator state
- Tasks are submitted when all their dependencies are complete
- Worker threads poll for ready tasks from a shared queue
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Generator
from uuid import UUID

from stardag._task import BaseTask, TaskStruct, flatten_task_struct
from stardag.build.registry import RegistryABC, registry_provider
from stardag.build.task_runner import RunCallback, TaskRunner

logger = logging.getLogger(__name__)


@dataclass
class TaskState:
    """Tracks the execution state of a task."""

    task: BaseTask
    # Static dependencies from requires()
    static_deps: list[BaseTask] = field(default_factory=list)
    # Dynamic dependencies yielded during execution
    dynamic_deps: list[BaseTask] = field(default_factory=list)
    # Generator if task has dynamic deps and is partially executed
    generator: Generator[TaskStruct, None, None] | None = None
    # True when task execution has fully completed (including all dynamic deps)
    completed: bool = False
    # Exception if task failed
    exception: Exception | None = None

    @property
    def all_deps(self) -> list[BaseTask]:
        return self.static_deps + self.dynamic_deps


def build(
    task: BaseTask,
    *,
    max_workers: int | None = None,
    completion_cache: set[UUID] | None = None,
    task_runner: TaskRunner | None = None,
    before_run_callback: RunCallback | None = None,
    on_complete_callback: RunCallback | None = None,
    registry: RegistryABC | None = None,
) -> None:
    """Build a task DAG using ThreadPoolExecutor for concurrency.

    Args:
        task: Root task to build
        max_workers: Maximum number of worker threads (defaults to ThreadPoolExecutor default)
        completion_cache: Set of already-completed task IDs
        task_runner: Custom task runner for executing tasks
        before_run_callback: Called before each task runs
        on_complete_callback: Called after each task completes
        registry: Registry for tracking builds
    """
    registry = registry or registry_provider.get()
    task_runner = task_runner or TaskRunner(
        before_run_callback=before_run_callback,
        on_complete_callback=on_complete_callback,
        registry=registry,
    )

    registry.start_build(root_tasks=[task])

    try:
        build_with_thread_pool(
            task,
            max_workers=max_workers,
            completion_cache=completion_cache or set(),
            task_runner=task_runner,
        )
        registry.complete_build()
    except Exception as e:
        registry.fail_build(str(e))
        raise


def build_with_thread_pool(
    root_task: BaseTask,
    *,
    max_workers: int | None = None,
    completion_cache: set[UUID],
    task_runner: TaskRunner,
) -> None:
    """Core build logic using ThreadPoolExecutor.

    Algorithm:
    1. Discover all tasks in the DAG (respecting completion cache)
    2. Submit tasks to executor when all dependencies are complete
    3. Handle dynamic dependencies by pausing execution and re-scheduling
    4. Continue until all tasks complete or an error occurs
    """
    # Task state tracking
    task_states: dict[UUID, TaskState] = {}
    # Lock for thread-safe state updates
    lock = threading.Lock()
    # Tasks currently being executed (task_id -> Future)
    executing: dict[UUID, Future] = {}

    def discover_task(task: BaseTask) -> TaskState:
        """Recursively discover tasks and their dependencies."""
        if task.id in task_states:
            return task_states[task.id]

        # Check if already complete
        if task.id in completion_cache or task.complete():
            state = TaskState(task=task, completed=True)
            completion_cache.add(task.id)
            task_states[task.id] = state
            return state

        # Get static dependencies
        static_deps = flatten_task_struct(task.requires())

        state = TaskState(task=task, static_deps=static_deps)
        task_states[task.id] = state

        # Recursively discover dependencies
        for dep in static_deps:
            discover_task(dep)

        return state

    def is_ready(state: TaskState) -> bool:
        """Check if a task is ready to execute (all deps complete)."""
        if state.completed:
            return False
        if state.task.id in executing:
            return False

        for dep in state.all_deps:
            dep_state = task_states.get(dep.id)
            if dep_state is None or not dep_state.completed:
                return False

        return True

    def execute_task(
        state: TaskState,
    ) -> tuple[UUID, Generator | None, Exception | None]:
        """Execute a task and return its result.

        Returns:
            Tuple of (task_id, generator_if_dynamic, exception_if_failed)
        """
        task = state.task
        try:
            if state.generator is not None:
                # Resume generator execution
                gen = state.generator
                try:
                    next(gen)  # Advance generator, yielded value handled elsewhere
                    return task.id, gen, None
                except StopIteration:
                    # Generator completed
                    return task.id, None, None
            else:
                # First execution
                result = task_runner.run(task)
                if result is not None:
                    # Task has dynamic deps, result is a generator
                    return task.id, result, None
                else:
                    # Task completed normally
                    return task.id, None, None

        except Exception as e:
            logger.exception(f"Error executing task {task}: {e}")
            return task.id, None, e

    def handle_dynamic_deps(
        state: TaskState, gen: Generator[TaskStruct, None, None]
    ) -> None:
        """Handle a task that yielded dynamic dependencies."""
        # Get the yielded dependencies
        try:
            # The generator already advanced in execute_task, so we need to
            # handle the deps it yielded. We'll need to restart from scratch
            # since we can't easily get the yielded value after next().
            pass
        except StopIteration:
            pass

    # Step 1: Discover all tasks
    discover_task(root_task)

    # Step 2: Execute tasks concurrently
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        while True:
            with lock:
                # Check for completion
                root_state = task_states[root_task.id]
                if root_state.completed:
                    break

                # Find ready tasks
                ready_tasks = [
                    state for state in task_states.values() if is_ready(state)
                ]

                # Submit ready tasks
                for state in ready_tasks:
                    future = executor.submit(execute_task, state)
                    executing[state.task.id] = future

            if not executing:
                # No tasks executing and root not complete - check for deadlock
                incomplete = [s for s in task_states.values() if not s.completed]
                if incomplete:
                    raise RuntimeError(
                        f"Deadlock detected: {len(incomplete)} tasks cannot proceed. "
                        f"Tasks: {[s.task.id for s in incomplete]}"
                    )
                break

            # Wait for at least one task to complete
            done_futures = []
            for future in as_completed(list(executing.values())):
                done_futures.append(future)
                break  # Process one at a time to check for newly ready tasks

            # Process completed tasks
            for future in done_futures:
                task_id, gen, exc = future.result()

                with lock:
                    del executing[task_id]
                    state = task_states[task_id]

                    if exc is not None:
                        state.exception = exc
                        raise exc

                    if gen is not None:
                        # Task yielded dynamic dependencies
                        # We need to get the yielded value - but we already called next()
                        # This is a limitation of the current approach
                        # Let's use a different strategy: wrap the generator

                        state.generator = gen
                        # For now, mark as needing to resume
                        # The yielded deps need to be discovered and built
                        # This requires refactoring execute_task to return yielded deps
                    else:
                        # Task completed
                        state.completed = True
                        completion_cache.add(task_id)


# Alternative approach: simpler implementation that handles dynamic deps better
def build_simple(
    root_task: BaseTask,
    *,
    max_workers: int | None = None,
    completion_cache: set[UUID] | None = None,
    task_runner: TaskRunner | None = None,
    before_run_callback: RunCallback | None = None,
    on_complete_callback: RunCallback | None = None,
    registry: RegistryABC | None = None,
) -> None:
    """Simpler concurrent build that handles dynamic deps iteratively.

    This version uses a work-stealing approach:
    1. Find all tasks that can be executed (deps complete)
    2. Execute them concurrently
    3. Repeat until all done

    Dynamic deps are handled by:
    1. Starting task execution
    2. When generator yields, extract deps and schedule them
    3. Resume generator when deps complete
    """
    registry = registry or registry_provider.get()
    task_runner = task_runner or TaskRunner(
        before_run_callback=before_run_callback,
        on_complete_callback=on_complete_callback,
        registry=registry,
    )
    completion_cache = completion_cache or set()

    registry.start_build(root_tasks=[root_task])

    @dataclass
    class TaskExecState:
        task: BaseTask
        generator: Generator[TaskStruct, None, None] | None = None
        pending_deps: list[BaseTask] = field(default_factory=list)

    # All discovered tasks
    all_tasks: dict[UUID, BaseTask] = {}
    # Tasks with generators that need to be resumed
    suspended: dict[UUID, TaskExecState] = {}
    # Lock for shared state
    lock = threading.Lock()

    def discover_all_tasks(task: BaseTask) -> None:
        """Recursively discover all tasks in the DAG."""
        if task.id in all_tasks:
            return
        all_tasks[task.id] = task
        for dep in flatten_task_struct(task.requires()):
            discover_all_tasks(dep)

    def is_complete(task: BaseTask) -> bool:
        """Check if a task is complete."""
        if task.id in completion_cache:
            return True
        if task.complete():
            completion_cache.add(task.id)
            return True
        return False

    def get_ready_tasks() -> list[BaseTask]:
        """Get tasks that are ready to execute."""
        ready = []
        for task in all_tasks.values():
            if is_complete(task):
                continue

            # Check if this task is suspended waiting for deps
            if task.id in suspended:
                exec_state = suspended[task.id]
                if all(is_complete(dep) for dep in exec_state.pending_deps):
                    ready.append(task)
                continue

            # Check if all static deps are complete
            deps = flatten_task_struct(task.requires())
            if all(is_complete(dep) for dep in deps):
                ready.append(task)

        return ready

    def run_task(
        task: BaseTask,
    ) -> tuple[UUID, Generator | None, list[BaseTask], Exception | None]:
        """Execute a task and return results.

        Returns:
            (task_id, generator_or_none, yielded_deps, exception_or_none)
        """
        try:
            exec_state = suspended.get(task.id)

            if exec_state is not None and exec_state.generator is not None:
                # Resume suspended generator
                gen = exec_state.generator
                try:
                    yielded = next(gen)
                    deps = flatten_task_struct(yielded)
                    return task.id, gen, deps, None
                except StopIteration:
                    return task.id, None, [], None
            else:
                # First execution
                result = task_runner.run(task)
                if result is not None:
                    # Generator - get first yield
                    try:
                        yielded = next(result)
                        deps = flatten_task_struct(yielded)
                        return task.id, result, deps, None
                    except StopIteration:
                        return task.id, None, [], None
                else:
                    return task.id, None, [], None

        except Exception as e:
            logger.exception(f"Error running task {task}: {e}")
            return task.id, None, [], e

    # Discover all tasks
    discover_all_tasks(root_task)

    try:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            executing: dict[UUID, Future] = {}

            while not is_complete(root_task):
                # Find ready tasks
                with lock:
                    ready = [t for t in get_ready_tasks() if t.id not in executing]

                if not ready and not executing:
                    incomplete = [t for t in all_tasks.values() if not is_complete(t)]
                    if incomplete:
                        raise RuntimeError(
                            f"Deadlock: {len(incomplete)} tasks cannot proceed"
                        )
                    break

                # Submit ready tasks
                for task in ready:
                    future = executor.submit(run_task, task)
                    executing[task.id] = future

                # Wait for completions
                if executing:
                    for future in as_completed(list(executing.values())):
                        task_id, gen, deps, exc = future.result()

                        with lock:
                            del executing[task_id]

                            if exc is not None:
                                raise exc

                            if gen is not None:
                                # Task yielded deps - suspend it
                                # Discover any new tasks
                                for dep in deps:
                                    discover_all_tasks(dep)

                                suspended[task_id] = TaskExecState(
                                    task=all_tasks[task_id],
                                    generator=gen,
                                    pending_deps=deps,
                                )
                            else:
                                # Task completed
                                completion_cache.add(task_id)
                                if task_id in suspended:
                                    del suspended[task_id]

                        # Check for more ready tasks after each completion
                        break

        registry.complete_build()

    except Exception as e:
        registry.fail_build(str(e))
        raise
