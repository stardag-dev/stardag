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
import inspect
import logging
import time
from collections.abc import Awaitable, Sequence
from typing import Any, AsyncIterator, Callable, Literal
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
from stardag.registry import RegistryABC, registry_provider

logger = logging.getLogger(__name__)


# Number of tasks the build engine sends per ``task_register_bulk[_aio]``
# HTTP call. Deliberately well under the API's hard cap (1000) — see
# the matching constant in ``_concurrent.py`` for rationale (smaller DB
# transactions, fewer warn-mode losses, friendlier compressed body
# sizes for fat task specs).
_BULK_REGISTER_CHUNK_SIZE = 50


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
    on_registry_failure: OnRegistryFailure = "raise",
) -> BuildSummary:
    """Sync API for building tasks sequentially.

    This is intended primarily for debugging and testing.

    Tasks are registered with the registry as they are discovered (in
    deterministic DFS order from the roots), so the full DAG appears in the
    UI immediately rather than progressively as tasks become runnable.

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
        on_registry_failure: How to handle registry call failures. "raise" (default)
            propagates the exception; "warn" logs a warning and continues.

    Returns:
        BuildSummary with status, task counts, and build_id
    """
    tasks_list = _validate_tasks(tasks)

    if registry is None:
        registry = registry_provider.get()
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
    # Tasks already registered with the registry. Tracked so the per-task
    # retry path inside _run_task_sequential / lock-failure handling
    # doesn't double-register.
    registered_tasks: set[UUID] = set()
    # Tasks accumulated during the current discover() walk (post-order)
    # awaiting the bulk-register call. Cleared by
    # ``flush_pending_registrations()``.
    pending_registrations: list[BaseTask] = []

    # Start or resume build *before* discovery so we have a build_id to
    # register tasks against.
    if resume_build_id is not None:
        build_id = resume_build_id
        # Emit a BUILD_RESUMED event so the registry flips a previously
        # terminal build back to RUNNING. On older registry servers this
        # is a no-op (the endpoint 404s and APIRegistry swallows it).
        try:
            registry.build_resume(build_id)
        except Exception as reg_err:
            handle_registry_error(
                reg_err,
                f"Failed to mark build {build_id} as resumed",
                on_registry_failure,
            )
    else:
        build_id = registry.build_start(root_tasks=tasks_list)

    def register_task_once(task: BaseTask) -> None:
        """Register a single task (used as a fallback retry path).

        The happy path uses ``flush_pending_registrations`` to bulk-
        register every task collected during a discover walk. This per-
        task fallback exists for the lock-failure branch and for
        ``_run_task_sequential`` when discover-time registration was
        skipped or failed in ``warn`` mode.
        """
        if task.id in registered_tasks:
            return
        try:
            registry.task_register(build_id, task)
            registered_tasks.add(task.id)
        except Exception as reg_err:
            handle_registry_error(
                reg_err,
                f"Failed to register task {task.id}",
                on_registry_failure,
            )

    def flush_pending_registrations() -> None:
        """Bulk-register every task accumulated since the last flush.

        Called after each discover walk (initial walk and any runtime
        walk triggered by dynamic deps). Chunks the batch into
        ``_BULK_REGISTER_CHUNK_SIZE``-sized slices to stay within the
        API's per-call cap. On chunk failure: ``warn`` mode logs and
        stops processing further chunks; the per-task retry inside
        ``_run_task_sequential`` falls back to per-task register as
        tasks become runnable. ``raise`` mode propagates.
        """
        if not pending_registrations:
            return
        batch = list(pending_registrations)
        pending_registrations.clear()

        for chunk_start in range(0, len(batch), _BULK_REGISTER_CHUNK_SIZE):
            chunk = batch[chunk_start : chunk_start + _BULK_REGISTER_CHUNK_SIZE]
            try:
                registry.task_register_bulk(build_id, chunk)
            except Exception as reg_err:
                ids_preview = ", ".join(str(t.id) for t in chunk[:5])
                if len(chunk) > 5:
                    ids_preview += f", +{len(chunk) - 5} more"
                total_chunks = (
                    len(batch) + _BULK_REGISTER_CHUNK_SIZE - 1
                ) // _BULK_REGISTER_CHUNK_SIZE
                this_chunk = (chunk_start // _BULK_REGISTER_CHUNK_SIZE) + 1
                handle_registry_error(
                    reg_err,
                    f"Failed to bulk-register chunk {this_chunk}/{total_chunks} "
                    f"({len(chunk)} tasks; ids: {ids_preview})",
                    on_registry_failure,
                )
                return
            for t in chunk:
                registered_tasks.add(t.id)

    def discover(task: BaseTask) -> None:
        """Recursively discover tasks, stopping at already-complete tasks.

        Discovery only collects into ``pending_registrations`` (post-order
        — deps first, parents last). The actual bulk-register call fires
        via ``flush_pending_registrations()`` after the walk.
        """
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
                # Don't recurse into deps - they're already built. Append
                # to pending_registrations (leaf in post-order).
                pending_registrations.append(task)
                return

        # Task not complete (or register_all) — recurse into deps first
        # (post-order), so when we register this task its deps already
        # exist in the API.
        for dep in flatten_task_struct(task.requires()):
            discover(dep)

        # All deps are registered. Append self after children — preserves
        # post-order within the subtree.
        pending_registrations.append(task)

    # Mark previously-completed tasks as complete in the registry. Registration
    # already happened inline in discover(); we still need to fire
    # task_complete so they appear COMPLETED rather than PENDING — and to
    # self-heal tasks left in "Started" state from a previous build that
    # crashed (their target exists, so they are complete, but the registry
    # still shows them as running).
    completed_previously_completed_count = 0

    def mark_pending_previously_completed() -> None:
        """Send task_complete for any previously-completed tasks not yet marked.

        Drains ``previously_completed_tasks`` starting from the last marked
        index. Called both for the initial bulk pass and after any runtime
        ``discover()`` call that might newly surface a complete task (e.g. the
        static dep of a dynamically yielded task).
        """
        nonlocal completed_previously_completed_count
        while completed_previously_completed_count < len(previously_completed_tasks):
            pc_task = previously_completed_tasks[completed_previously_completed_count]
            completed_previously_completed_count += 1
            if pc_task.id not in registered_tasks:
                # Registration failed in `warn` mode; can't mark complete
                # against a row that was never written.
                continue
            try:
                registry.task_complete(build_id, pc_task)
            except Exception as reg_err:
                handle_registry_error(
                    reg_err,
                    f"Failed to mark previously completed task {pc_task.id} as complete",
                    on_registry_failure,
                )

    def runtime_discover(task: BaseTask) -> None:
        """``discover()`` wrapper used after the initial registration pass.

        Walks ``discover()`` (collecting newly-found tasks into
        ``pending_registrations``), bulk-registers them, then sends
        task_complete for any previously-completed tasks the walk
        surfaced.
        """
        discover(task)
        flush_pending_registrations()
        mark_pending_previously_completed()

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
        # Discover all tasks. If discover() raises (e.g. requires() /
        # complete() throws), the outer except below emits build_fail so
        # the build doesn't get stuck in RUNNING state.
        for root in tasks_list:
            discover(root)

        # Bulk-register every discovered task in one HTTP call. Order is
        # post-order so the API resolves dependency_task_ids without
        # phantom-creating any rows.
        flush_pending_registrations()

        # Mark previously-completed tasks as complete now that registration
        # has landed.
        mark_pending_previously_completed()

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
                    # Ensure the task row exists before failing it (no-op if
                    # registration succeeded during discovery; retry if it
                    # failed in `warn` mode).
                    register_task_once(ready_task)
                    try:
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
                    runtime_discover,
                    register_task_once,
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
    register_task_once: Callable[[BaseTask], None],
    task_count: TaskCount | None = None,
    on_registry_failure: OnRegistryFailure = "raise",
) -> None:
    """Run a single task in sequential mode, handling dynamic deps."""
    # Ensure static requires() are complete before running this task. When the
    # outer build loop schedules a ready task this is a no-op (its deps are
    # already in completion_cache), but it's required when this function is
    # called recursively for a dynamically-yielded dep whose requires() chain
    # has not been built yet (see issue #118).
    #
    # Go through discover() (the runtime wrapper in build_sequential) so that:
    # - task_count.discovered is updated when we first walk into a subgraph,
    # - any newly-surfaced already-complete deps get a registry
    #   task_register + task_complete pair (they missed the initial bulk
    #   registration because they were only found at runtime).
    for static_dep in flatten_task_struct(task.requires()):
        discover(static_dep)
        if static_dep.id not in completion_cache:
            _run_task_sequential(
                static_dep,
                completion_cache,
                all_tasks,
                build_id,
                registry,
                dual_run_default,
                discover,
                register_task_once,
                task_count,
                on_registry_failure,
            )
            if task_count is not None:
                task_count.succeeded += 1

    # The task should already be registered (during discover), but retry once
    # if discover-time registration failed in `warn` mode. We still wrap
    # /start in handle_registry_error so a 404 (registration didn't land) or
    # a transient blip doesn't hard-fail the build in `warn` mode.
    register_task_once(task)
    try:
        registry.task_start(build_id, task)
    except Exception as reg_err:
        handle_registry_error(
            reg_err,
            f"Failed to start task {task.id}",
            on_registry_failure,
        )

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

                # Discover and build dynamic deps FIRST. discover() runtime
                # wrapper recurses into requires() and post-order-registers
                # everything in the subtree, so by the time we record the
                # edge below, every upstream row already exists in the API
                # and _reconcile_dependency_edges doesn't have to
                # phantom-create anything.
                for dep in dynamic_deps:
                    discover(dep)

                    if dep.id not in completion_cache:
                        _run_task_sequential(
                            dep,
                            completion_cache,
                            all_tasks,
                            build_id,
                            registry,
                            dual_run_default,
                            discover,
                            register_task_once,
                            task_count,
                            on_registry_failure,
                        )
                        if task_count is not None:
                            task_count.succeeded += 1

                # Now record yielded deps as edges so the DAG view shows
                # them (static deps are recorded via task_register).
                if dynamic_deps:
                    try:
                        registry.task_add_dependencies(
                            build_id, task, dynamic_deps, is_dynamic=True
                        )
                    except Exception as reg_err:
                        handle_registry_error(
                            reg_err,
                            f"Failed to record dynamic deps for task {task.id}",
                            on_registry_failure,
                        )

            except StopIteration:
                break

    completion_cache.add(task.id)
    try:
        registry.task_complete(build_id, task)
    except Exception as reg_err:
        handle_registry_error(
            reg_err,
            f"Failed to complete task {task.id}",
            on_registry_failure,
        )

    # Upload artifacts if any
    try:
        artifacts = task.artifacts()
        if artifacts:
            registry.task_upload_artifacts(build_id, task, artifacts)
    except Exception as artifact_err:
        handle_registry_error(
            artifact_err,
            f"Failed to collect/upload artifacts for task {task.id}",
            on_registry_failure,
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
    on_registry_failure: OnRegistryFailure = "raise",
) -> BuildSummary:
    """Async API for building tasks sequentially.

    This is intended primarily for debugging and testing.

    Tasks are registered with the registry as they are discovered (in
    deterministic DFS order from the roots), so the full DAG appears in the
    UI immediately rather than progressively as tasks become runnable.

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
        on_registry_failure: How to handle registry call failures. "raise" (default)
            propagates the exception; "warn" logs a warning and continues.

    Returns:
        BuildSummary with status, task counts, and build_id
    """
    tasks_list = _validate_tasks(tasks)

    if registry is None:
        registry = registry_provider.get()
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
    # Tasks already registered with the registry. Tracked so per-task
    # fallback retry paths don't double-register.
    registered_tasks: set[UUID] = set()
    # Tasks accumulated during the current discover() walk (post-order),
    # awaiting bulk registration. Cleared by flush_pending_registrations_aio.
    pending_registrations: list[BaseTask] = []

    # Start or resume build *before* discovery so we have a build_id to
    # register tasks against.
    if resume_build_id is not None:
        build_id = resume_build_id
        # Emit a BUILD_RESUMED event so the registry flips a previously
        # terminal build back to RUNNING. On older registry servers this
        # is a no-op (the endpoint 404s and APIRegistry swallows it).
        try:
            await registry.build_resume_aio(build_id)
        except Exception as reg_err:
            handle_registry_error(
                reg_err,
                f"Failed to mark build {build_id} as resumed",
                on_registry_failure,
            )
    else:
        build_id = await registry.build_start_aio(root_tasks=tasks_list)

    async def register_task_once_aio(task: BaseTask) -> None:
        """Register a single task (per-task fallback retry path).

        Used by ``_run_task_sequential_aio`` when discover-time bulk
        registration was skipped or failed in ``warn`` mode.
        """
        if task.id in registered_tasks:
            return
        try:
            await registry.task_register_aio(build_id, task)
            registered_tasks.add(task.id)
        except Exception as reg_err:
            handle_registry_error(
                reg_err,
                f"Failed to register task {task.id}",
                on_registry_failure,
            )

    async def flush_pending_registrations_aio() -> None:
        """Bulk-register every task collected since the last flush.

        Chunks the batch into ``_BULK_REGISTER_CHUNK_SIZE``-sized slices
        to stay within the API's per-call cap. Stops on first chunk
        failure (per-task retry inside ``_run_task_sequential_aio``
        handles the rest in ``warn`` mode).
        """
        if not pending_registrations:
            return
        batch = list(pending_registrations)
        pending_registrations.clear()

        for chunk_start in range(0, len(batch), _BULK_REGISTER_CHUNK_SIZE):
            chunk = batch[chunk_start : chunk_start + _BULK_REGISTER_CHUNK_SIZE]
            try:
                await registry.task_register_bulk_aio(build_id, chunk)
            except Exception as reg_err:
                ids_preview = ", ".join(str(t.id) for t in chunk[:5])
                if len(chunk) > 5:
                    ids_preview += f", +{len(chunk) - 5} more"
                total_chunks = (
                    len(batch) + _BULK_REGISTER_CHUNK_SIZE - 1
                ) // _BULK_REGISTER_CHUNK_SIZE
                this_chunk = (chunk_start // _BULK_REGISTER_CHUNK_SIZE) + 1
                handle_registry_error(
                    reg_err,
                    f"Failed to bulk-register chunk {this_chunk}/{total_chunks} "
                    f"({len(chunk)} tasks; ids: {ids_preview})",
                    on_registry_failure,
                )
                return
            for t in chunk:
                registered_tasks.add(t.id)

    async def discover(task: BaseTask) -> None:
        """Recursively discover tasks, stopping at already-complete tasks.

        Discovery only collects into ``pending_registrations`` in
        post-order (deps first, parents last). The bulk-register call
        fires via ``flush_pending_registrations_aio()`` after the walk.
        """
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
                # Don't recurse into deps - they're already built. Append
                # to pending_registrations (leaf in post-order).
                pending_registrations.append(task)
                return

        # Task not complete (or register_all) — recurse into deps first
        # (post-order), so by the time the bulk register call processes
        # this task its deps are already in the array (and thus in the DB).
        for dep in flatten_task_struct(task.requires()):
            await discover(dep)

        # Append self after children — preserves post-order within subtree.
        pending_registrations.append(task)

    # Mark previously-completed tasks as complete in the registry. Registration
    # already happened inline in discover(); we still need to fire
    # task_complete_aio so they appear COMPLETED rather than PENDING.
    completed_previously_completed_count = 0

    async def mark_pending_previously_completed_aio() -> None:
        """Send task_complete for any previously-completed tasks not yet marked.

        Drains ``previously_completed_tasks`` starting from the last marked
        index. Called for the initial bulk pass and after any runtime
        ``discover()`` call that might surface a new complete task (e.g. the
        static dep of a dynamically yielded task).
        """
        nonlocal completed_previously_completed_count
        while completed_previously_completed_count < len(previously_completed_tasks):
            pc_task = previously_completed_tasks[completed_previously_completed_count]
            completed_previously_completed_count += 1
            if pc_task.id not in registered_tasks:
                # Registration failed in `warn` mode; skip task_complete since
                # the API row doesn't exist.
                continue
            try:
                await registry.task_complete_aio(build_id, pc_task)
            except Exception as reg_err:
                handle_registry_error(
                    reg_err,
                    f"Failed to mark previously completed task {pc_task.id} as complete",
                    on_registry_failure,
                )

    async def runtime_discover_aio(task: BaseTask) -> None:
        """``discover()`` wrapper used after the initial registration pass.

        Walks ``discover()`` (collecting newly-found tasks into
        ``pending_registrations``), bulk-registers them, then sends
        task_complete for any previously-completed tasks the walk
        surfaced.
        """
        await discover(task)
        await flush_pending_registrations_aio()
        await mark_pending_previously_completed_aio()

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
        # Discover all tasks. If discover() raises (e.g. requires() /
        # complete_aio() throws), the outer except below emits
        # build_fail_aio so the build doesn't get stuck in RUNNING state.
        for root in tasks_list:
            await discover(root)

        # Bulk-register every discovered task in one HTTP call.
        await flush_pending_registrations_aio()

        # Mark previously-completed tasks as complete now that registration
        # has landed.
        await mark_pending_previously_completed_aio()

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
                    # Ensure the task row exists before failing it (no-op if
                    # registration succeeded during discovery; retry if it
                    # failed in `warn` mode).
                    await register_task_once_aio(ready_task)
                    try:
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
                    runtime_discover_aio,
                    register_task_once_aio,
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


async def _iter_dynamic_deps(result: Any) -> AsyncIterator[Any]:
    """Iterate yielded dynamic deps from a sync or async generator run result.

    Normalizes the three cases into a single async iteration:
    - None / non-generator: yields nothing
    - Sync generator (``__next__``): wraps with ``next()`` calls
    - Async generator (``__anext__``): iterates with ``async for``
    """
    if result is None:
        return
    if hasattr(result, "__anext__"):
        async for value in result:
            yield value
        return
    if hasattr(result, "__next__"):
        while True:
            try:
                yield next(result)
            except StopIteration:
                return


async def _run_task_sequential_aio(
    task: BaseTask,
    completion_cache: set[UUID],
    all_tasks: dict[UUID, BaseTask],
    build_id: UUID,
    registry: RegistryABC,
    sync_run_default: Literal["thread", "blocking"],
    discover: Callable[[BaseTask], Awaitable[None]],
    register_task_once_aio: Callable[[BaseTask], Awaitable[None]],
    task_count: TaskCount | None = None,
    on_registry_failure: OnRegistryFailure = "raise",
) -> None:
    """Run a single task in async sequential mode, handling dynamic deps."""
    # Ensure static requires() are complete before running this task — see the
    # matching comment and issue #118 reference in _run_task_sequential.
    for static_dep in flatten_task_struct(task.requires()):
        await discover(static_dep)
        if static_dep.id not in completion_cache:
            await _run_task_sequential_aio(
                static_dep,
                completion_cache,
                all_tasks,
                build_id,
                registry,
                sync_run_default,
                discover,
                register_task_once_aio,
                task_count,
                on_registry_failure,
            )
            if task_count is not None:
                task_count.succeeded += 1

    # The task should already be registered (during discover), but retry once
    # if discover-time registration failed in `warn` mode. We still wrap
    # /start in handle_registry_error so a 404 (registration didn't land) or
    # a transient blip doesn't hard-fail the build in `warn` mode.
    await register_task_once_aio(task)
    try:
        await registry.task_start_aio(build_id, task)
    except Exception as reg_err:
        handle_registry_error(
            reg_err,
            f"Failed to start task {task.id}",
            on_registry_failure,
        )

    has_run = _has_custom_run(task)
    has_run_aio = _has_custom_run_aio(task)

    # Determine how to run
    if has_run_aio:
        # Async generators (dynamic deps via `async def run_aio: yield ...`)
        # cannot be awaited — calling the bound method returns the generator.
        if inspect.isasyncgenfunction(type(task).run_aio):
            result: Any = task.run_aio()
        else:
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

    # Handle dynamic deps — unified across sync and async generators
    async for yielded in _iter_dynamic_deps(result):
        dynamic_deps = flatten_task_struct(yielded)

        # Discover and build dynamic deps FIRST. discover() is the runtime
        # wrapper that recurses into requires() and post-order-registers
        # everything in the subtree, so the upstream rows already exist in
        # the API by the time we record the edge below — no phantom
        # creation in _reconcile_dependency_edges.
        for dep in dynamic_deps:
            await discover(dep)

            if dep.id not in completion_cache:
                await _run_task_sequential_aio(
                    dep,
                    completion_cache,
                    all_tasks,
                    build_id,
                    registry,
                    sync_run_default,
                    discover,
                    register_task_once_aio,
                    task_count,
                    on_registry_failure,
                )
                if task_count is not None:
                    task_count.succeeded += 1

        # Now record yielded deps as edges so the DAG view shows them
        # (static deps are recorded via task_register).
        if dynamic_deps:
            try:
                await registry.task_add_dependencies_aio(
                    build_id, task, dynamic_deps, is_dynamic=True
                )
            except Exception as reg_err:
                handle_registry_error(
                    reg_err,
                    f"Failed to record dynamic deps for task {task.id}",
                    on_registry_failure,
                )

    completion_cache.add(task.id)
    try:
        await registry.task_complete_aio(build_id, task)
    except Exception as reg_err:
        handle_registry_error(
            reg_err,
            f"Failed to complete task {task.id}",
            on_registry_failure,
        )

    # Upload artifacts if any
    try:
        artifacts = await task.artifacts_aio()
        if artifacts:
            await registry.task_upload_artifacts_aio(build_id, task, artifacts)
    except Exception as artifact_err:
        handle_registry_error(
            artifact_err,
            f"Failed to collect/upload artifacts for task {task.id}",
            on_registry_failure,
        )


__all__ = [
    "build_sequential",
    "build_sequential_aio",
]
