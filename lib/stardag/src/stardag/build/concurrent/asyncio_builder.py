"""AsyncIO-based concurrent build implementation.

This implementation uses Python's asyncio for concurrent task execution.
It's well-suited for async tasks and provides clean handling of dynamic deps.

Key features:
- Uses asyncio.create_task for concurrent execution
- Handles dynamic dependencies naturally with async/await
- Cleaner control flow than thread-based approach
- Full async support: tasks can implement run_aio() for native async execution
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Generator
from uuid import UUID

from stardag._task import BaseTask, TaskStruct, flatten_task_struct
from stardag.build.registry import RegistryABC, registry_provider
from stardag.build.task_runner import AsyncRunCallback, AsyncTaskRunner

logger = logging.getLogger(__name__)


@dataclass
class TaskExecState:
    """Tracks execution state of a task with dynamic deps."""

    task: BaseTask
    generator: Generator[TaskStruct, None, None] | None = None
    pending_deps: list[BaseTask] = field(default_factory=list)
    event: asyncio.Event = field(default_factory=asyncio.Event)


async def build(
    task: BaseTask,
    *,
    max_concurrent: int | None = None,
    completion_cache: set[UUID] | None = None,
    task_runner: AsyncTaskRunner | None = None,
    before_run_callback: AsyncRunCallback | None = None,
    on_complete_callback: AsyncRunCallback | None = None,
    registry: RegistryABC | None = None,
) -> None:
    """Build a task DAG using asyncio for concurrency.

    Tasks are executed using their async run_aio() method, enabling native async I/O.
    This provides true concurrent execution without thread pool overhead.

    Args:
        task: Root task to build
        max_concurrent: Maximum number of concurrent tasks (None = unlimited)
        completion_cache: Set of already-completed task IDs
        task_runner: Custom async task runner
        before_run_callback: Async callback called before each task runs
        on_complete_callback: Async callback called after each task completes
        registry: Registry for tracking builds
    """
    registry = registry or registry_provider.get()
    task_runner = task_runner or AsyncTaskRunner(
        before_run_callback=before_run_callback,
        on_complete_callback=on_complete_callback,
        registry=registry,
    )

    await registry.start_build_aio(root_tasks=[task])

    try:
        await build_with_asyncio(
            task,
            max_concurrent=max_concurrent,
            completion_cache=completion_cache or set(),
            task_runner=task_runner,
        )
        await registry.complete_build_aio()
    except Exception as e:
        await registry.fail_build_aio(str(e))
        raise


async def build_with_asyncio(
    root_task: BaseTask,
    *,
    max_concurrent: int | None = None,
    completion_cache: set[UUID],
    task_runner: AsyncTaskRunner,
) -> None:
    """Core async build logic.

    Algorithm:
    1. Discover all tasks from requires()
    2. Create async task for each stardag task
    3. Each async task waits for its dependencies before running
    4. Dynamic deps are handled by pausing and waiting for new deps

    Tasks are executed via task.run_aio() for native async support.
    """
    # All discovered tasks
    all_tasks: dict[UUID, BaseTask] = {}
    # Completion events - signaled when task completes
    completion_events: dict[UUID, asyncio.Event] = {}
    # Semaphore for limiting concurrency
    semaphore = asyncio.Semaphore(max_concurrent) if max_concurrent else None
    # Lock for shared state
    lock = asyncio.Lock()
    # Active async tasks
    active_tasks: dict[UUID, asyncio.Task] = {}

    def discover_tasks(task: BaseTask) -> None:
        """Recursively discover all tasks."""
        if task.id in all_tasks:
            return
        all_tasks[task.id] = task
        completion_events[task.id] = asyncio.Event()
        for dep in flatten_task_struct(task.requires()):
            discover_tasks(dep)

    async def is_complete(task: BaseTask) -> bool:
        """Check if task is complete (async)."""
        if task.id in completion_cache:
            return True
        if await task.complete_aio():
            completion_cache.add(task.id)
            completion_events[task.id].set()
            return True
        return False

    async def wait_for_deps(deps: list[BaseTask]) -> None:
        """Wait for all dependencies to complete."""
        for dep in deps:
            if not await is_complete(dep):
                await completion_events[dep.id].wait()

    async def run_task_with_deps(task: BaseTask) -> None:
        """Run a single task after waiting for its dependencies."""
        # Check if already complete
        if await is_complete(task):
            return

        # Get static dependencies
        static_deps = flatten_task_struct(task.requires())

        # Ensure all deps are discovered and being processed
        async with lock:
            for dep in static_deps:
                if dep.id not in all_tasks:
                    discover_tasks(dep)
                if dep.id not in active_tasks and not await is_complete(dep):
                    active_tasks[dep.id] = asyncio.create_task(run_task_with_deps(dep))

        # Wait for static deps
        await wait_for_deps(static_deps)

        # Check again if complete (might have been completed by another path)
        if await is_complete(task):
            return

        # Acquire semaphore if limiting concurrency
        if semaphore:
            await semaphore.acquire()

        try:
            # Execute the task using async runner - no thread needed!
            result = await task_runner.run(task)

            if result is not None:
                # Task has dynamic deps - result is a generator
                gen = result
                while True:
                    try:
                        yielded = next(gen)
                        dynamic_deps = flatten_task_struct(yielded)

                        # Check if all dynamic deps are already complete
                        all_complete = True
                        for dep in dynamic_deps:
                            if not await is_complete(dep):
                                all_complete = False
                                break
                        if all_complete:
                            continue

                        # Discover and schedule dynamic deps
                        async with lock:
                            for dep in dynamic_deps:
                                if dep.id not in all_tasks:
                                    discover_tasks(dep)
                                if dep.id not in active_tasks and not await is_complete(
                                    dep
                                ):
                                    active_tasks[dep.id] = asyncio.create_task(
                                        run_task_with_deps(dep)
                                    )

                        # Wait for dynamic deps
                        await wait_for_deps(dynamic_deps)

                    except StopIteration:
                        # Generator completed - task is done
                        break

            # Mark complete
            completion_cache.add(task.id)
            completion_events[task.id].set()

        finally:
            if semaphore:
                semaphore.release()

    # Discover initial tasks
    discover_tasks(root_task)

    # Start building from root (dependencies will be built recursively)
    await run_task_with_deps(root_task)

    # Wait for any remaining active tasks (shouldn't be any)
    if active_tasks:
        await asyncio.gather(*active_tasks.values(), return_exceptions=True)


# Alternative: More explicit approach using a task queue
async def build_queue_based(
    root_task: BaseTask,
    *,
    max_concurrent: int = 4,
    completion_cache: set[UUID] | None = None,
    task_runner: AsyncTaskRunner | None = None,
    before_run_callback: AsyncRunCallback | None = None,
    on_complete_callback: AsyncRunCallback | None = None,
    registry: RegistryABC | None = None,
) -> None:
    """Alternative build using explicit task queue.

    Tasks are executed via async run_aio() method for native async support.

    This approach is closer to how Prefect works:
    1. Queue tasks that are ready to run
    2. Workers pull from queue and execute asynchronously
    3. When task completes, check if new tasks are ready
    """
    registry = registry or registry_provider.get()
    task_runner = task_runner or AsyncTaskRunner(
        before_run_callback=before_run_callback,
        on_complete_callback=on_complete_callback,
        registry=registry,
    )
    completion_cache = completion_cache or set()

    await registry.start_build_aio(root_tasks=[root_task])

    # Task tracking
    all_tasks: dict[UUID, BaseTask] = {}
    completion_events: dict[UUID, asyncio.Event] = {}
    # Generators for tasks with dynamic deps
    generators: dict[UUID, Generator[TaskStruct, None, None]] = {}
    # Pending dynamic deps for suspended tasks
    pending_dynamic_deps: dict[UUID, list[BaseTask]] = {}

    # Work queue
    ready_queue: asyncio.Queue[BaseTask] = asyncio.Queue()
    # Track what's currently executing
    executing: set[UUID] = set()

    def discover_tasks(task: BaseTask) -> None:
        """Recursively discover all tasks."""
        if task.id in all_tasks:
            return
        all_tasks[task.id] = task
        completion_events[task.id] = asyncio.Event()
        for dep in flatten_task_struct(task.requires()):
            discover_tasks(dep)

    def is_complete(task_id: UUID) -> bool:
        return task_id in completion_cache

    def is_ready(task: BaseTask) -> bool:
        """Check if task is ready to execute."""
        if is_complete(task.id):
            return False
        if task.id in executing:
            return False

        # Check static deps
        for dep in flatten_task_struct(task.requires()):
            if not is_complete(dep.id):
                return False

        # Check dynamic deps if suspended
        if task.id in pending_dynamic_deps:
            for dep in pending_dynamic_deps[task.id]:
                if not is_complete(dep.id):
                    return False

        return True

    def enqueue_ready_tasks() -> None:
        """Add all ready tasks to the queue."""
        for task in all_tasks.values():
            if is_ready(task):
                ready_queue.put_nowait(task)
                executing.add(task.id)

    async def worker() -> None:
        """Worker coroutine that processes tasks from the queue."""
        while True:
            try:
                task = await asyncio.wait_for(ready_queue.get(), timeout=0.1)
            except asyncio.TimeoutError:
                # Check if we're done
                if is_complete(root_task.id):
                    return
                continue

            try:
                # Check if task is resuming from suspension
                if task.id in generators:
                    gen = generators[task.id]
                    try:
                        yielded = next(gen)
                        dynamic_deps = flatten_task_struct(yielded)

                        # Check if all deps complete
                        if all(is_complete(dep.id) for dep in dynamic_deps):
                            # Continue in the loop
                            ready_queue.put_nowait(task)
                        else:
                            # Discover and wait for deps
                            for dep in dynamic_deps:
                                discover_tasks(dep)
                            pending_dynamic_deps[task.id] = dynamic_deps
                            executing.discard(task.id)
                            enqueue_ready_tasks()
                        continue

                    except StopIteration:
                        # Generator done - task complete
                        del generators[task.id]
                        if task.id in pending_dynamic_deps:
                            del pending_dynamic_deps[task.id]
                        completion_cache.add(task.id)
                        completion_events[task.id].set()
                        executing.discard(task.id)
                        enqueue_ready_tasks()
                        continue

                # Execute the task using async runner
                result = await task_runner.run(task)

                if result is not None:
                    # Dynamic deps - store generator
                    gen = result
                    try:
                        yielded = next(gen)
                        dynamic_deps = flatten_task_struct(yielded)

                        if all(is_complete(dep.id) for dep in dynamic_deps):
                            # All deps ready, continue
                            generators[task.id] = gen
                            ready_queue.put_nowait(task)
                        else:
                            # Wait for deps
                            generators[task.id] = gen
                            for dep in dynamic_deps:
                                discover_tasks(dep)
                            pending_dynamic_deps[task.id] = dynamic_deps
                            executing.discard(task.id)
                            enqueue_ready_tasks()

                    except StopIteration:
                        # No dynamic deps actually yielded
                        completion_cache.add(task.id)
                        completion_events[task.id].set()
                        executing.discard(task.id)
                        enqueue_ready_tasks()
                else:
                    # Normal completion
                    completion_cache.add(task.id)
                    completion_events[task.id].set()
                    executing.discard(task.id)
                    enqueue_ready_tasks()

            except Exception as e:
                logger.exception(f"Error executing task {task}: {e}")
                executing.discard(task.id)
                raise

    try:
        # Discover all tasks
        discover_tasks(root_task)

        # Seed the queue
        enqueue_ready_tasks()

        # Start workers
        workers = [asyncio.create_task(worker()) for _ in range(max_concurrent)]

        # Wait for either root to complete or any worker to fail
        completion_task = asyncio.create_task(completion_events[root_task.id].wait())

        # Wait for completion or first worker failure
        done, pending = await asyncio.wait(
            [completion_task] + workers,
            return_when=asyncio.FIRST_COMPLETED,
        )

        # Check for worker failures
        for task in done:
            if task is not completion_task:
                # A worker finished - check if it raised an exception
                exc = task.exception()
                if exc:
                    # Cancel other tasks and re-raise
                    for p in pending:
                        p.cancel()
                    raise exc

        # Cancel remaining workers
        for p in pending:
            p.cancel()

        await registry.complete_build_aio()

    except Exception as e:
        await registry.fail_build_aio(str(e))
        raise
