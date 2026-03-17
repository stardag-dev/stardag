"""Sequential build functions for debugging.

These functions execute tasks one at a time in dependency order.
Intended for debugging and testing, not production use.

Design note: build_sequential exposes a synchronous interface and does not
manage a long-lived event loop itself, to maximize debuggability and
compatibility. Internally it may still call asyncio.run() for async-only
tasks (implementing only run_aio) or async global-lock operations, which
means calling build_sequential from within an already-running event loop
(e.g. Jupyter, asyncio-based applications) can raise RuntimeError. In such
environments, run it in a separate thread/process or use the async build API
instead. Do NOT refactor build_sequential to wrap an async core — this
breaks the sync contract.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Sequence
from typing import Callable, Literal
from uuid import UUID

from stardag import (
    BaseTask,
    flatten_task_struct,
)
from stardag._core.base_task import (
    _has_custom_run,
    _has_custom_run_aio,
)
from stardag.build._base import (
    BuildExitStatus,
    BuildSummary,
    DefaultGlobalLockSelector,
    FailMode,
    GlobalConcurrencyLockManager,
    GlobalLockConfig,
    GlobalLockSelector,
    LockAcquisitionResult,
    LockAcquisitionStatus,
    OnRegistryFailure,
    TaskCount,
    handle_registry_error,
)
from stardag.registry import RegistryABC, init_registry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pure helper functions shared by both sync and async build variants.
# These contain no I/O and are safe to call from any context.
# ---------------------------------------------------------------------------


def _validate_tasks(tasks: Sequence[BaseTask] | BaseTask) -> list[BaseTask]:
    """Validate and normalize the tasks argument into a list of BaseTask."""
    if isinstance(tasks, BaseTask):
        return [tasks]
    task_list = list(tasks)
    for idx, task in enumerate(task_list):
        if not isinstance(task, BaseTask):
            raise ValueError(f"Invalid task at index {idx}: {task} (must be BaseTask)")
    return task_list


def _has_failed_dep(task: BaseTask, failed_cache: set[UUID]) -> bool:
    """Check if any dependency of the given task has failed."""
    deps = flatten_task_struct(task.requires())
    return any(d.id in failed_cache for d in deps)


def _find_ready_task(
    all_tasks: dict[UUID, BaseTask],
    completion_cache: set[UUID],
    failed_cache: set[UUID],
) -> BaseTask | None:
    """Find the next task whose dependencies are all complete.

    Returns None if no task is ready to run.
    """
    for task in all_tasks.values():
        if task.id in completion_cache or task.id in failed_cache:
            continue
        if _has_failed_dep(task, failed_cache):
            continue
        deps = flatten_task_struct(task.requires())
        if all(d.id in completion_cache for d in deps):
            return task
    return None


def _check_for_deadlock(
    all_tasks: dict[UUID, BaseTask],
    completion_cache: set[UUID],
    failed_cache: set[UUID],
) -> None:
    """Raise RuntimeError if incomplete tasks exist that are not blocked by failures.

    Should be called when no ready task is found. If all remaining incomplete tasks
    are blocked by failed dependencies, this is not a deadlock and the function
    returns silently.
    """
    incomplete = [
        t
        for t in all_tasks.values()
        if t.id not in completion_cache and t.id not in failed_cache
    ]
    if incomplete:
        truly_blocked = [t for t in incomplete if not _has_failed_dep(t, failed_cache)]
        if truly_blocked:
            raise RuntimeError(
                f"Deadlock: {len(truly_blocked)} tasks cannot proceed. "
                f"Tasks: {[str(t.id) for t in truly_blocked[:5]]}"
            )


def build_sequential(
    tasks: Sequence[BaseTask] | BaseTask,
    registry: RegistryABC | None = None,
    fail_mode: FailMode = FailMode.FAIL_FAST,
    dual_run_default: Literal["sync", "async"] = "sync",
    resume_build_id: UUID | None = None,
    global_lock_manager: GlobalConcurrencyLockManager | None = None,
    global_lock_config: GlobalLockConfig | None = None,
    register_all: bool = False,
    on_registry_failure: OnRegistryFailure = "warn",
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
        tasks: List of root tasks to build (and their dependencies) or a single root
            task.
        registry: Registry for tracking builds
        fail_mode: How to handle task failures
        dual_run_default: For dual tasks, prefer sync or async execution
        resume_build_id: Optional build ID to resume. If provided, continues tracking
            events under this existing build instead of starting a new one.
        global_lock_manager: Global concurrency lock manager for distributed builds.
            If provided with global_lock_config.enabled=True, tasks will acquire locks
            before execution for "exactly once" semantics across processes.
        global_lock_config: Configuration for global locking behavior.
        register_all: If True, discovery continues recursing into dependencies of
            already-complete tasks. This ensures all tasks in the DAG get registered
            in the registry (useful for complete DAG visualization). Default False
            for performance — skipping complete subgraphs avoids unnecessary I/O.
        on_registry_failure: How to handle registry call failures. "warn" (default)
            logs a warning and continues; "raise" propagates the exception.

    Returns:
        BuildSummary with status, task counts, and build_id
    """
    tasks_list = _validate_tasks(tasks)

    if registry is None:
        registry = init_registry()
    if global_lock_config is None:
        global_lock_config = GlobalLockConfig()
    lock_selector: GlobalLockSelector = DefaultGlobalLockSelector(global_lock_config)
    held_locks: set[str] = set()

    task_count = TaskCount()
    completion_cache: set[UUID] = set()
    failed_cache: set[UUID] = set()
    error: Exception | None = None

    # Discover all tasks, stopping at already-complete tasks
    all_tasks: dict[UUID, BaseTask] = {}
    previously_completed_tasks: list[BaseTask] = []

    def discover(task: BaseTask) -> None:
        """Recursively discover tasks, stopping at already-complete tasks."""
        if task.id in all_tasks:
            return
        all_tasks[task.id] = task
        task_count.discovered += 1

        # Check if this task is already complete
        if task.complete():
            completion_cache.add(task.id)
            task_count.previously_completed += 1
            previously_completed_tasks.append(task)
            if not register_all:
                # Don't recurse into deps - they're already built
                return

        # Task not complete (or register_all) - recurse into dependencies
        for dep in flatten_task_struct(task.requires()):
            discover(dep)

    for root in tasks_list:
        discover(root)

    # Start or resume build
    if resume_build_id is not None:
        build_id = resume_build_id
    else:
        build_id = registry.build_start(root_tasks=tasks_list)

    # Register previously completed tasks so they appear in the build's task list.
    # We also call task_complete to mark them as done — otherwise they remain in
    # PENDING state in the registry (e.g. WrapperTasks that are complete because
    # their deps are complete, but were never explicitly run).
    for task in previously_completed_tasks:
        try:
            registry.task_register(build_id, task)
        except Exception as reg_err:
            handle_registry_error(
                reg_err,
                f"Failed to register previously completed task {task.id}",
                on_registry_failure,
            )
            continue
        try:
            registry.task_complete(build_id, task)
        except Exception as reg_err:
            handle_registry_error(
                reg_err,
                f"Failed to mark previously completed task {task.id} as complete",
                on_registry_failure,
            )

    def acquire_lock_sync(task: BaseTask) -> LockAcquisitionResult:
        """Acquire lock synchronously with retry/backoff."""
        assert global_lock_manager is not None
        assert global_lock_config is not None
        task_id = str(task.id)
        timeout = global_lock_config.lock_wait_timeout_seconds
        current_interval = global_lock_config.lock_wait_initial_interval_seconds
        max_interval = global_lock_config.lock_wait_max_interval_seconds
        backoff_factor = global_lock_config.lock_wait_backoff_factor
        start_time = time.time()

        while True:
            result = asyncio.run(global_lock_manager.acquire(task_id))

            if result.status == LockAcquisitionStatus.ACQUIRED:
                return result

            if result.status == LockAcquisitionStatus.ALREADY_COMPLETED:
                return result

            if result.status == LockAcquisitionStatus.ERROR:
                return result

            # HELD_BY_OTHER or CONCURRENCY_LIMIT_REACHED - retry with backoff
            if timeout is None:
                return result

            elapsed = time.time() - start_time
            if elapsed >= timeout:
                return LockAcquisitionResult(
                    status=result.status,
                    acquired=False,
                    error_message=f"Timeout after {timeout}s: {result.status.value}",
                )

            # Check if task was completed externally during the wait
            if task.complete():
                return LockAcquisitionResult(
                    status=LockAcquisitionStatus.ALREADY_COMPLETED,
                    acquired=False,
                )

            logger.debug(
                f"Lock for {task_id} unavailable ({result.status}), "
                f"retrying in {current_interval:.1f}s..."
            )
            time.sleep(current_interval)
            current_interval = min(current_interval * backoff_factor, max_interval)

    def release_lock_sync(task: BaseTask, completed: bool) -> None:
        """Release lock for task if held."""
        if global_lock_manager is None:
            return
        task_id = str(task.id)
        if task_id not in held_locks:
            return
        try:
            asyncio.run(global_lock_manager.release(task_id, task_completed=completed))
        except Exception as e:
            logger.warning(f"Failed to release lock for task {task_id}: {e}")
        finally:
            held_locks.discard(task_id)

    try:
        # Build in topological order
        while True:
            ready_task = _find_ready_task(all_tasks, completion_cache, failed_cache)

            if ready_task is None:
                _check_for_deadlock(all_tasks, completion_cache, failed_cache)
                # All remaining tasks are blocked by failed deps - exit gracefully
                break

            # Acquire lock if needed
            use_lock = global_lock_manager is not None and lock_selector(ready_task)
            if use_lock:
                lock_result = acquire_lock_sync(ready_task)

                if lock_result.status == LockAcquisitionStatus.ALREADY_COMPLETED:
                    # Task completed elsewhere - skip execution
                    completion_cache.add(ready_task.id)
                    task_count.previously_completed += 1
                    continue

                if lock_result.status != LockAcquisitionStatus.ACQUIRED:
                    # Lock not acquired - treat as failure
                    task_count.failed += 1
                    failed_cache.add(ready_task.id)
                    error = RuntimeError(
                        f"Failed to acquire lock: {lock_result.error_message}"
                    )
                    try:
                        # Register first so the registry has a task row to fail
                        registry.task_register(build_id, ready_task)
                        registry.task_fail(build_id, ready_task, str(error))
                    except Exception as reg_err:
                        handle_registry_error(
                            reg_err,
                            "Failed to notify registry of lock failure",
                            on_registry_failure,
                        )
                    if fail_mode == FailMode.FAIL_FAST:
                        raise error
                    continue

                # Lock acquired - track it
                held_locks.add(str(ready_task.id))

            # Execute the task
            task_completed = False
            try:
                _run_task_sequential(
                    ready_task,
                    completion_cache,
                    all_tasks,
                    build_id,
                    registry,
                    dual_run_default,
                    discover,
                    task_count,
                    on_registry_failure,
                )
                task_count.succeeded += 1
                task_completed = True
            except Exception as e:
                task_count.failed += 1
                failed_cache.add(ready_task.id)
                error = e
                try:
                    registry.task_fail(build_id, ready_task, str(e))
                except Exception as reg_err:
                    handle_registry_error(
                        reg_err,
                        f"Failed to notify registry of task {ready_task.id} failure",
                        on_registry_failure,
                    )
                if fail_mode == FailMode.FAIL_FAST:
                    raise
            finally:
                if use_lock:
                    release_lock_sync(ready_task, completed=task_completed)

        registry.build_complete(build_id)
        return BuildSummary(
            status=BuildExitStatus.SUCCESS
            if error is None
            else BuildExitStatus.FAILURE,
            task_count=task_count,
            build_id=build_id,
            error=error,
        )

    except Exception as e:
        registry.build_fail(build_id, str(e))
        if fail_mode == FailMode.FAIL_FAST:
            raise
        return BuildSummary(
            status=BuildExitStatus.FAILURE,
            task_count=task_count,
            build_id=build_id,
            error=e,
        )


def _run_task_sequential(
    task: BaseTask,
    completion_cache: set[UUID],
    all_tasks: dict[UUID, BaseTask],
    build_id: UUID,
    registry: RegistryABC,
    dual_run_default: Literal["sync", "async"],
    discover: Callable[[BaseTask], None],
    task_count: TaskCount | None = None,
    on_registry_failure: OnRegistryFailure = "warn",
) -> None:
    """Run a single task in sequential mode, handling dynamic deps."""
    registry.task_start(build_id, task)

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

                # Discover and build dynamic deps using the same discover()
                # function used for static deps, so task_count.discovered
                # is properly incremented and sub-deps are fully recursed.
                for dep in dynamic_deps:
                    was_known = dep.id in all_tasks
                    discover(dep)

                    # A dynamic dep that was newly discovered and already complete
                    # won't be caught by the initial "register previously completed"
                    # loop (which runs before the build loop). Register it now so it
                    # appears correctly in the registry for this build.
                    if not was_known and dep.id in completion_cache:
                        try:
                            registry.task_register(build_id, dep)
                        except Exception as reg_err:
                            handle_registry_error(
                                reg_err,
                                f"Failed to register dynamic previously-completed task {dep.id}",
                                on_registry_failure,
                            )
                        try:
                            registry.task_complete(build_id, dep)
                        except Exception as reg_err:
                            handle_registry_error(
                                reg_err,
                                f"Failed to mark dynamic previously-completed task {dep.id} as complete",
                                on_registry_failure,
                            )

                    if dep.id not in completion_cache:
                        _run_task_sequential(
                            dep,
                            completion_cache,
                            all_tasks,
                            build_id,
                            registry,
                            dual_run_default,
                            discover,
                            task_count,
                            on_registry_failure,
                        )
                        if task_count is not None:
                            task_count.succeeded += 1

            except StopIteration:
                break

    completion_cache.add(task.id)
    registry.task_complete(build_id, task)

    # Upload artifacts if any. Artifact collection is best-effort — errors are
    # logged but do not fail the task (it already ran and completed successfully).
    try:
        artifacts = task.artifacts()
        if artifacts:
            registry.task_upload_artifacts(build_id, task, artifacts)
    except Exception as artifact_err:
        logger.warning(
            f"Failed to collect/upload artifacts for task {task.id}: {artifact_err}"
        )


async def build_sequential_aio(
    tasks: Sequence[BaseTask] | BaseTask,
    registry: RegistryABC | None = None,
    fail_mode: FailMode = FailMode.FAIL_FAST,
    sync_run_default: Literal["thread", "blocking"] = "blocking",
    resume_build_id: UUID | None = None,
    global_lock_manager: GlobalConcurrencyLockManager | None = None,
    global_lock_config: GlobalLockConfig | None = None,
    register_all: bool = False,
    on_registry_failure: OnRegistryFailure = "warn",
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
        tasks: List of root tasks to build (and their dependencies) or a single root
            task.
        registry: Registry for tracking builds
        fail_mode: How to handle task failures
        sync_run_default: For sync-only tasks, block or use thread pool
        resume_build_id: Optional build ID to resume. If provided, continues tracking
            events under this existing build instead of starting a new one.
        global_lock_manager: Global concurrency lock manager for distributed builds.
            If provided with global_lock_config.enabled=True, tasks will acquire locks
            before execution for "exactly once" semantics across processes.
        global_lock_config: Configuration for global locking behavior.
        register_all: If True, discovery continues recursing into dependencies of
            already-complete tasks. This ensures all tasks in the DAG get registered
            in the registry (useful for complete DAG visualization). Default False
            for performance — skipping complete subgraphs avoids unnecessary I/O.
        on_registry_failure: How to handle registry call failures. "warn" (default)
            logs a warning and continues; "raise" propagates the exception.

    Returns:
        BuildSummary with status, task counts, and build_id
    """
    tasks_list = _validate_tasks(tasks)

    if registry is None:
        registry = init_registry()
    if global_lock_config is None:
        global_lock_config = GlobalLockConfig()
    lock_selector: GlobalLockSelector = DefaultGlobalLockSelector(global_lock_config)
    held_locks: set[str] = set()

    task_count = TaskCount()
    completion_cache: set[UUID] = set()
    failed_cache: set[UUID] = set()
    error: Exception | None = None

    # Discover all tasks, stopping at already-complete tasks
    all_tasks: dict[UUID, BaseTask] = {}
    previously_completed_tasks: list[BaseTask] = []

    async def discover(task: BaseTask) -> None:
        """Recursively discover tasks, stopping at already-complete tasks."""
        if task.id in all_tasks:
            return
        all_tasks[task.id] = task
        task_count.discovered += 1

        # Check if this task is already complete
        if await task.complete_aio():
            completion_cache.add(task.id)
            task_count.previously_completed += 1
            previously_completed_tasks.append(task)
            if not register_all:
                # Don't recurse into deps - they're already built
                return

        # Task not complete (or register_all) - recurse into dependencies
        for dep in flatten_task_struct(task.requires()):
            await discover(dep)

    for root in tasks_list:
        await discover(root)

    # Start or resume build
    if resume_build_id is not None:
        build_id = resume_build_id
    else:
        build_id = await registry.build_start_aio(root_tasks=tasks_list)

    # Register previously completed tasks so they appear in the build's task list.
    # We also call task_complete_aio to mark them as done — otherwise they remain in
    # PENDING state in the registry (e.g. WrapperTasks that are complete because
    # their deps are complete, but were never explicitly run).
    for task in previously_completed_tasks:
        try:
            await registry.task_register_aio(build_id, task)
        except Exception as reg_err:
            handle_registry_error(
                reg_err,
                f"Failed to register previously completed task {task.id}",
                on_registry_failure,
            )
            continue
        try:
            await registry.task_complete_aio(build_id, task)
        except Exception as reg_err:
            handle_registry_error(
                reg_err,
                f"Failed to mark previously completed task {task.id} as complete",
                on_registry_failure,
            )

    async def acquire_lock_aio(task: BaseTask) -> LockAcquisitionResult:
        """Acquire lock asynchronously with retry/backoff."""
        assert global_lock_manager is not None
        assert global_lock_config is not None
        task_id = str(task.id)
        timeout = global_lock_config.lock_wait_timeout_seconds
        current_interval = global_lock_config.lock_wait_initial_interval_seconds
        max_interval = global_lock_config.lock_wait_max_interval_seconds
        backoff_factor = global_lock_config.lock_wait_backoff_factor

        loop = asyncio.get_event_loop()
        start_time = loop.time()

        while True:
            result = await global_lock_manager.acquire(task_id)

            if result.status == LockAcquisitionStatus.ACQUIRED:
                return result

            if result.status == LockAcquisitionStatus.ALREADY_COMPLETED:
                return result

            if result.status == LockAcquisitionStatus.ERROR:
                return result

            # HELD_BY_OTHER or CONCURRENCY_LIMIT_REACHED - retry with backoff
            if timeout is None:
                return result

            elapsed = loop.time() - start_time
            if elapsed >= timeout:
                return LockAcquisitionResult(
                    status=result.status,
                    acquired=False,
                    error_message=f"Timeout after {timeout}s: {result.status.value}",
                )

            # Check if task was completed externally during the wait
            if await task.complete_aio():
                return LockAcquisitionResult(
                    status=LockAcquisitionStatus.ALREADY_COMPLETED,
                    acquired=False,
                )

            logger.debug(
                f"Lock for {task_id} unavailable ({result.status}), "
                f"retrying in {current_interval:.1f}s..."
            )
            await asyncio.sleep(current_interval)
            current_interval = min(current_interval * backoff_factor, max_interval)

    async def release_lock_aio(task: BaseTask, completed: bool) -> None:
        """Release lock for task if held."""
        if global_lock_manager is None:
            return
        task_id = str(task.id)
        if task_id not in held_locks:
            return
        try:
            await global_lock_manager.release(task_id, task_completed=completed)
        except Exception as e:
            logger.warning(f"Failed to release lock for task {task_id}: {e}")
        finally:
            held_locks.discard(task_id)

    try:
        # Build in topological order
        while True:
            ready_task = _find_ready_task(all_tasks, completion_cache, failed_cache)

            if ready_task is None:
                _check_for_deadlock(all_tasks, completion_cache, failed_cache)
                # All remaining tasks are blocked by failed deps - exit gracefully
                break

            # Acquire lock if needed
            use_lock = global_lock_manager is not None and lock_selector(ready_task)
            if use_lock:
                lock_result = await acquire_lock_aio(ready_task)

                if lock_result.status == LockAcquisitionStatus.ALREADY_COMPLETED:
                    # Task completed elsewhere - skip execution
                    completion_cache.add(ready_task.id)
                    task_count.previously_completed += 1
                    continue

                if lock_result.status != LockAcquisitionStatus.ACQUIRED:
                    # Lock not acquired - treat as failure
                    task_count.failed += 1
                    failed_cache.add(ready_task.id)
                    error = RuntimeError(
                        f"Failed to acquire lock: {lock_result.error_message}"
                    )
                    try:
                        # Register first so the registry has a task row to fail
                        await registry.task_register_aio(build_id, ready_task)
                        await registry.task_fail_aio(build_id, ready_task, str(error))
                    except Exception as reg_err:
                        handle_registry_error(
                            reg_err,
                            "Failed to notify registry of lock failure",
                            on_registry_failure,
                        )
                    if fail_mode == FailMode.FAIL_FAST:
                        raise error
                    continue

                # Lock acquired - track it
                held_locks.add(str(ready_task.id))

            # Execute the task
            task_completed = False
            try:
                await _run_task_sequential_aio(
                    ready_task,
                    completion_cache,
                    all_tasks,
                    build_id,
                    registry,
                    sync_run_default,
                    discover,
                    task_count,
                    on_registry_failure,
                )
                task_count.succeeded += 1
                task_completed = True
            except Exception as e:
                task_count.failed += 1
                failed_cache.add(ready_task.id)
                error = e
                try:
                    await registry.task_fail_aio(build_id, ready_task, str(e))
                except Exception as reg_err:
                    handle_registry_error(
                        reg_err,
                        f"Failed to notify registry of task {ready_task.id} failure",
                        on_registry_failure,
                    )
                if fail_mode == FailMode.FAIL_FAST:
                    raise
            finally:
                if use_lock:
                    await release_lock_aio(ready_task, completed=task_completed)

        await registry.build_complete_aio(build_id)
        return BuildSummary(
            status=BuildExitStatus.SUCCESS
            if error is None
            else BuildExitStatus.FAILURE,
            task_count=task_count,
            build_id=build_id,
            error=error,
        )

    except Exception as e:
        await registry.build_fail_aio(build_id, str(e))
        if fail_mode == FailMode.FAIL_FAST:
            raise
        return BuildSummary(
            status=BuildExitStatus.FAILURE,
            task_count=task_count,
            build_id=build_id,
            error=e,
        )


async def _run_task_sequential_aio(
    task: BaseTask,
    completion_cache: set[UUID],
    all_tasks: dict[UUID, BaseTask],
    build_id: UUID,
    registry: RegistryABC,
    sync_run_default: Literal["thread", "blocking"],
    discover: Callable[[BaseTask], Awaitable[None]],
    task_count: TaskCount | None = None,
    on_registry_failure: OnRegistryFailure = "warn",
) -> None:
    """Run a single task in async sequential mode, handling dynamic deps."""
    await registry.task_start_aio(build_id, task)

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

                # Discover and build dynamic deps using the same discover()
                # function used for static deps, so task_count.discovered
                # is properly incremented and sub-deps are fully recursed.
                for dep in dynamic_deps:
                    was_known = dep.id in all_tasks
                    await discover(dep)

                    # A dynamic dep that was newly discovered and already complete
                    # won't be caught by the initial "register previously completed"
                    # loop (which runs before the build loop). Register it now so it
                    # appears correctly in the registry for this build.
                    if not was_known and dep.id in completion_cache:
                        try:
                            await registry.task_register_aio(build_id, dep)
                        except Exception as reg_err:
                            handle_registry_error(
                                reg_err,
                                f"Failed to register dynamic previously-completed task {dep.id}",
                                on_registry_failure,
                            )
                        try:
                            await registry.task_complete_aio(build_id, dep)
                        except Exception as reg_err:
                            handle_registry_error(
                                reg_err,
                                f"Failed to mark dynamic previously-completed task {dep.id} as complete",
                                on_registry_failure,
                            )

                    if dep.id not in completion_cache:
                        await _run_task_sequential_aio(
                            dep,
                            completion_cache,
                            all_tasks,
                            build_id,
                            registry,
                            sync_run_default,
                            discover,
                            task_count,
                            on_registry_failure,
                        )
                        if task_count is not None:
                            task_count.succeeded += 1

            except StopIteration:
                break

    completion_cache.add(task.id)
    await registry.task_complete_aio(build_id, task)

    # Upload artifacts if any. Artifact collection is best-effort — errors are
    # logged but do not fail the task (it already ran and completed successfully).
    try:
        artifacts = await task.artifacts_aio()
        if artifacts:
            await registry.task_upload_artifacts_aio(build_id, task, artifacts)
    except Exception as artifact_err:
        logger.warning(
            f"Failed to collect/upload artifacts for task {task.id}: {artifact_err}"
        )


__all__ = [
    "build_sequential",
    "build_sequential_aio",
]
