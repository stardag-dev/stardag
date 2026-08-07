"""Concurrent build implementation.

This module contains:
- HybridConcurrentTaskExecutor: Routes tasks to async/thread/process based on policy
- build_aio(): Async concurrent build function
- build(): Sync wrapper for build_aio() (the default for production)
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import traceback as tb_module
import warnings
from datetime import datetime, timezone
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from enum import StrEnum
from typing import AsyncGenerator, Generator, Literal, Protocol, Sequence, Union, cast
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
    BuildExitStatus,
    BuildSummary,
    ClaimConfig,
    DetachedExecutionStatus,
    DefaultGlobalLockSelector,
    DetachedHandle,
    current_build_id_var,
    FailMode,
    GlobalConcurrencyLockManager,
    GlobalLockConfig,
    GlobalLockSelector,
    LockAcquisitionResult,
    LockAcquisitionStatus,
    OnRegistryFailure,
    TaskCount,
    TaskExecutionError,
    TaskExecutionState,
    TaskExecutorABC,
    handle_registry_error,
)
from stardag.build._concurrency import (
    ConcurrencyConfig,
    ConcurrencyLimiter,
    build_concurrency_limiter,
)
from stardag.registry import RegistryABC, registry_provider

logger = logging.getLogger(__name__)


class _SkippedSentinel(Exception):
    """Marker placed on TaskExecutionState.exception for skipped tasks.

    Skipped tasks never executed (a dep failed or was cancelled). Setting
    a sentinel exception lets ``find_ready_tasks`` and the skip-emission
    fixed-point loop treat them like other terminal-state tasks without
    confusing them with real failures (which contribute to BuildSummary.error).
    Never raised — purely a state marker.
    """


# Number of tasks the build engine sends per ``task_register_bulk_aio``
# HTTP call. Deliberately well under the API's hard cap (1000) so that
# (a) DB transactions on the server stay short and don't contend with
# concurrent builds, (b) compressed request bodies stay easily within
# any reverse-proxy limit even with fat task specs, and (c) a chunk
# failure in ``warn`` mode loses few tasks before the per-task fallback
# kicks in. For a 5000-task DAG it's still 100 requests instead of 5000
# — most of the bulk-register win remains.
_BULK_REGISTER_CHUNK_SIZE = 50

# Interval between background renewals of held (deprecated) global locks —
# half the lock service's 60s TTL. Module-level for testability.
_LOCK_RENEWAL_INTERVAL_SECONDS = 30.0


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
# Concurrent Build Function
# =============================================================================


async def build_aio(
    tasks: Sequence[BaseTask] | BaseTask,
    task_executor: TaskExecutorABC | None = None,
    fail_mode: FailMode = FailMode.FAIL_FAST,
    registry: RegistryABC | None = None,
    max_concurrent_discover: int = 50,
    global_lock_manager: GlobalConcurrencyLockManager | None = None,
    global_lock_config: GlobalLockConfig | None = None,
    resume_build_id: UUID | None = None,
    register_all: bool = False,
    on_registry_failure: OnRegistryFailure = "raise",
    concurrency_config: ConcurrencyConfig | None = None,
    concurrency_limiter: ConcurrencyLimiter | None = None,
    claim: bool | None = None,
    claim_config: ClaimConfig | None = None,
) -> BuildSummary:
    """Build tasks concurrently using hybrid async/thread/process execution.

    This is the main build function for production use. It:
    - Discovers all tasks in the DAG(s) and registers each one with the
      registry as soon as it's discovered (so the full DAG is visible in the
      UI immediately, not progressively as tasks become runnable)
    - Schedules tasks for execution when dependencies are met
    - Handles dynamic dependencies via generator suspension
    - Supports multiple root tasks (built concurrently)
    - Routes tasks to async/thread/process based on ExecutionModeSelector
    - Manages all registry interactions (register/start/complete/fail task)
    - Optionally uses global concurrency locks for distributed execution

    Args:
        tasks: List of root tasks to build (and their dependencies) or a single root
            task.
        task_executor: TaskExecutor for executing tasks (default: HybridConcurrentTaskExecutor).
            Use RoutedTaskExecutor to route tasks to different executors (e.g., Modal).
        fail_mode: How to handle task failures
        registry: Registry for tracking builds (default: from registry_provider)
        max_concurrent_discover: Maximum concurrent completion checks during DAG discovery.
            Higher values speed up discovery for large DAGs with remote targets.
        global_lock_manager: Global concurrency lock manager for distributed builds.
            If provided with global_lock_config.enabled=True, tasks will acquire locks
            before execution to ensure exactly-once execution across processes.
        global_lock_config: Configuration for global locking behavior.
        resume_build_id: Optional build ID to resume. If provided, continues tracking
            events under this existing build instead of starting a new one.
        register_all: If True, discovery continues recursing into dependencies of
            already-complete tasks. This ensures all tasks in the DAG get registered
            in the registry (useful for complete DAG visualization). Default False
            for performance — skipping complete subgraphs avoids unnecessary I/O.
        on_registry_failure: How to handle registry call failures. "raise" (default)
            propagates the exception; "warn" logs a warning and continues.
        concurrency_config: Build-local concurrency limiting — an overall cap
            (``max_concurrent_tasks``) and/or named limits mapped to tasks via
            a callback. Applied to every executor by gating the executor
            ``submit`` call. Ignored if ``concurrency_limiter`` is given.
        concurrency_limiter: An explicit ConcurrencyLimiter (e.g. a custom or
            future registry-backed/global implementation). Takes precedence
            over ``concurrency_config``. When neither is set, no throttle is
            applied (zero overhead).

    Returns:
        BuildSummary with status, task counts, and build_id
    """
    if isinstance(tasks, BaseTask):
        tasks = [tasks]
    else:
        tasks = list(tasks)
        for idx, task in enumerate(tasks):
            if not isinstance(task, BaseTask):
                raise ValueError(
                    f"Invalid task at index {idx}: {task} (must be BaseTask)"
                )

    # Determine registry: explicit > registry_provider
    if registry is None:
        registry = registry_provider.get()
    logger.info(f"Using registry: {type(registry).__name__}")

    if task_executor is None:
        task_executor = HybridConcurrentTaskExecutor()

    # Setup global lock selector
    if global_lock_config is None:
        global_lock_config = GlobalLockConfig()
    lock_selector: GlobalLockSelector = DefaultGlobalLockSelector(global_lock_config)

    # Resolve the concurrency limiter (explicit limiter > config > no-op). The
    # LocalConcurrencyLimiter builds its semaphores here, inside the running
    # build loop, so they bind to the correct event loop.
    limiter: ConcurrencyLimiter = build_concurrency_limiter(
        concurrency_config, concurrency_limiter
    )

    if global_lock_config is not None and global_lock_config.enabled:
        warnings.warn(
            "The global concurrency lock is deprecated in favor of per-task "
            "execution claims, which are enabled by default for probeable "
            "(detached) executions when a registry is configured — see the "
            "`claim` parameter and stardag#185. The lock remains available "
            "for executions without probeable liveness (e.g. local "
            "executors).",
            DeprecationWarning,
            stacklevel=3,
        )

    # Track locks held by this build for manual release
    held_locks: set[str] = set()
    # Background TTL-renewal tasks per held lock (the low-level acquire's
    # 60s lease would otherwise expire under long-running tasks).
    lock_renewals: dict[str, asyncio.Task] = {}

    # --- Per-task execution claims (default exactly-once) ---
    claim_cfg = claim_config or ClaimConfig()

    def claim_enabled_for(task: BaseTask) -> bool:
        """Whether to claim-start this task (see build_aio's ``claim``).

        auto (None): only when the execution is probeable — a denied
        competitor can re-attach to or probe the winner. Ref-less
        executions (local executors) would need TTL liveness the claim
        doesn't have; the (deprecated) global lock still covers those.
        """
        if claim is False:
            return False
        if claim is True:
            return True
        return task_executor.supports_detached(task)

    task_count = TaskCount()
    completion_cache: set[UUID] = set()
    error: BaseException | None = None
    # Set when any task fails (or its lock acquisition fails). The main
    # loop processes the whole batch first, then breaks out if FAIL_FAST.
    # Lifting the raise out of process_result keeps sibling completions in
    # the same wait-batch from being silently dropped.
    fail_fast_triggered: bool = False

    # Task execution states
    task_states: dict[UUID, TaskExecutionState] = {}
    # Re-attachable detached executions reported by the registry at
    # registration time: task UUID -> (executor name, executor ref). A task
    # lands here when its global status is RUNNING with a recorded ref —
    # e.g. spawned by a previous orchestrator run that died, or by a
    # concurrently running build. Entries are popped on first use.
    detached_refs: dict[UUID, tuple[str, str]] = {}
    # Events for completion signaling
    completion_events: dict[UUID, asyncio.Event] = {}
    # Currently executing tasks
    executing: set[UUID] = set()

    # Tasks found to be already complete during discovery (mark complete in registry
    # after discovery finishes — registration itself happens via the bulk call).
    previously_completed_tasks: list[BaseTask] = []
    # Tasks accumulated during the current discover() walk, in post-order,
    # awaiting the bulk-registration call. Within each subtree this is
    # strict post-order; sibling subtrees may interleave (TaskGroup runs
    # them concurrently), but each task's static deps are always appended
    # before it — which is all the API needs to avoid phantom-creation.
    # Cleared by ``flush_pending_registrations`` after each bulk call.
    pending_registrations: list[BaseTask] = []
    # Per-task event signalling that this task's discover() has finished
    # appending it to pending_registrations. The fast-path
    # ``if task.id in task_states: return`` would otherwise let a sibling
    # discoverer race past — appending its own parent ahead of the still-
    # in-flight dep — re-introducing exactly the phantom window the
    # post-order walk is designed to eliminate (diamond DAGs, shared deps).
    discover_done: dict[UUID, asyncio.Event] = {}

    # Synchronization for concurrent discovery
    discover_lock = asyncio.Lock()
    discover_semaphore = asyncio.Semaphore(max_concurrent_discover)

    # Start or resume build *before* discovery so we have a build_id to
    # register tasks against. Registering during discovery makes the full
    # DAG visible in the UI immediately, not leaves-first as tasks run.
    if resume_build_id is not None:
        build_id = resume_build_id
        logger.info(f"Resuming build: {build_id}")
        # Emit a BUILD_RESUMED event so the registry flips a previously
        # terminal build back to RUNNING and the UI surfaces "running
        # (resumed)". On older registry servers this is a no-op (the
        # endpoint 404s and APIRegistry swallows it with a warning).
        try:
            await registry.build_resume_aio(build_id)
        except Exception as reg_err:
            handle_registry_error(
                reg_err,
                f"Failed to mark build {build_id} as resumed",
                on_registry_failure,
            )
    else:
        build_id = await registry.build_start_aio(root_tasks=tasks)
        logger.info(f"Started build: {build_id}")

    # Expose the build id ambiently for the duration of the build (e.g.
    # executors forward it to self-reporting workers). Reset in the outer
    # finally below.
    _build_id_token = current_build_id_var.set(build_id)

    async def discover(task: BaseTask) -> None:
        """Recursively discover tasks, stopping at already-complete tasks.

        Discovery only populates local state and ``pending_registrations``;
        the actual ``task_register_bulk_aio`` call fires once via
        ``flush_pending_registrations()`` after the whole walk completes.
        Walks in **post-order** so deps appear in ``pending_registrations``
        before their parents — the bulk endpoint processes the array in
        order, so by the time a parent's ``dependency_task_ids`` are
        reconciled the dep rows already exist (no phantom creation in
        ``_reconcile_dependency_edges``).

        Uses concurrent recursion with TaskGroup for parallel discovery,
        with a lock protecting shared data structures and a semaphore
        limiting concurrent completion checks.

        Concurrency invariant: when a sibling coroutine encounters this
        task already-discovered (fast-path), it ``await``s the
        ``discover_done`` event before returning. Without that, a
        diamond-DAG sibling could append its own parent to
        ``pending_registrations`` before this coroutine appends the
        shared dep — re-introducing the phantom window we're trying to
        eliminate.
        """
        # Check if already discovered and reserve our spot (with lock)
        async with discover_lock:
            if task.id in task_states:
                done_event = discover_done[task.id]
                already_seen = True
            else:
                static_deps = flatten_task_struct(task.requires())
                task_states[task.id] = TaskExecutionState(
                    task=task, static_deps=static_deps
                )
                completion_events[task.id] = asyncio.Event()
                discover_done[task.id] = asyncio.Event()
                done_event = discover_done[task.id]
                task_count.discovered += 1
                already_seen = False

        if already_seen:
            # Wait for the original discoverer to finish appending this
            # task to pending_registrations before returning. Otherwise a
            # parent further up our chain would append ahead of the dep.
            await done_event.wait()
            return

        # ``static_deps`` is only assigned in the else-branch above, but
        # pyright can't infer the control flow; pull it back from the
        # state we just stored.
        static_deps = task_states[task.id].static_deps

        try:
            # Check completion outside lock (I/O bound, use semaphore to limit concurrency)
            async with discover_semaphore:
                is_complete = await task.complete_aio()

            if is_complete:
                async with discover_lock:
                    completion_cache.add(task.id)
                    task_states[task.id].completed = True
                    completion_events[task.id].set()
                    task_count.previously_completed += 1
                    previously_completed_tasks.append(task)
                if not register_all:
                    # Don't recurse into deps — they're already built.
                    # Append to pending_registrations (leaf in post-order).
                    pending_registrations.append(task)
                    return

            # Task not complete (or register_all) — recurse into deps
            # first (post-order). TaskGroup waits for all children to
            # finish before this body continues, so all child appends to
            # pending_registrations land before our own append below.
            async with asyncio.TaskGroup() as tg:
                for dep in static_deps:
                    tg.create_task(discover(dep))

            # Append self after children — preserves post-order within
            # subtree.
            pending_registrations.append(task)
        finally:
            # Always set so any sibling fast-path waiters can proceed,
            # even when this discover() raised. (TaskGroup will propagate
            # the failure to siblings via cancellation, but we don't want
            # waiters to deadlock on a never-set event before that
            # cancellation reaches them.)
            done_event.set()

    async def flush_pending_registrations() -> None:
        """Bulk-register every task accumulated since the last flush.

        Called after each discover-walk (initial walk and each dynamic-deps
        walk). Chunks ``batch`` into ``_BULK_REGISTER_CHUNK_SIZE``-sized
        slices to stay within the API's per-call cap and to keep each
        transaction bounded. On chunk failure: ``warn`` mode logs and
        stops processing further chunks — the per-task retry inside
        ``submit_with_lock`` picks up the slack as tasks become runnable.
        ``raise`` mode propagates.
        """
        if not pending_registrations:
            return
        # Snapshot then clear so a recursive discover call inside the same
        # event loop tick can't accidentally re-register these tasks.
        batch = list(pending_registrations)
        pending_registrations.clear()

        for chunk_start in range(0, len(batch), _BULK_REGISTER_CHUNK_SIZE):
            chunk = batch[chunk_start : chunk_start + _BULK_REGISTER_CHUNK_SIZE]
            try:
                registered_infos = await registry.task_register_bulk_aio(
                    build_id, chunk
                )
            except Exception as reg_err:
                # Include up to 5 task IDs in the warning so debugging is
                # possible without dumping a 1000-id list into logs.
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
                task_states[t.id].registered = True
            # Collect re-attachable detached executions (task already RUNNING
            # elsewhere with a recorded executor ref). Backends that don't
            # report execution info return None.
            for info in registered_infos or []:
                if (
                    info.latest_status == "running"
                    and info.latest_executor is not None
                    and info.latest_executor_ref is not None
                ):
                    try:
                        task_uuid = UUID(info.task_id)
                    except ValueError:
                        continue
                    if task_uuid in task_states:
                        detached_refs[task_uuid] = (
                            info.latest_executor,
                            info.latest_executor_ref,
                        )

    # Mark previously completed tasks as complete in the registry. Registration
    # already happened inline in discover() above; we still need to fire
    # task_complete_aio so they appear COMPLETED rather than PENDING — and to
    # self-heal tasks left in "Started" state from a previous build that
    # crashed (their target exists, so they are complete, but the registry
    # still shows them as running).
    async def mark_previously_completed(task: BaseTask) -> None:
        if not task_states[task.id].registered:
            # Registration failed in `warn` mode; skip task_complete since
            # the API row doesn't exist.
            return
        try:
            await registry.task_complete_aio(build_id, task)
        except Exception as reg_err:
            handle_registry_error(
                reg_err,
                f"Failed to mark previously completed task {task.id} as complete",
                on_registry_failure,
            )

    # Map task_id -> asyncio.Task for in-flight executions
    pending_futures: dict[UUID, asyncio.Task] = {}

    async def process_result(
        task: BaseTask,
        result: LockAcquisitionResult
        | TaskExecutionError
        | BaseException
        | TaskStruct
        | None,
    ):
        """Process a single task result (including lock acquisition results).

        Never raises in FAIL_FAST mode — sets ``fail_fast_triggered`` so the
        main loop can finish processing the current ``done`` batch first
        (otherwise sibling completions in the same batch would be lost) and
        then escalate.
        """
        nonlocal error, fail_fast_triggered
        state = task_states[task.id]

        # Handle lock acquisition results (lock was not acquired)
        if isinstance(result, LockAcquisitionResult):
            if result.status == LockAcquisitionStatus.ALREADY_COMPLETED:
                # Task completed externally - wait for visibility then mark complete
                await wait_for_completion_with_retry(task)
                state.completed = True
                completion_cache.add(task.id)
                completion_events[task.id].set()
                task_count.previously_completed += 1
            else:
                # ERROR, HELD_BY_OTHER, or CONCURRENCY_LIMIT_REACHED after timeout
                msg = f"Lock {result.status.value}"
                if result.error_message:
                    msg += f": {result.error_message}"
                state.exception = Exception(msg)
                task_count.failed += 1
                error = state.exception
                if fail_mode == FailMode.FAIL_FAST:
                    fail_fast_triggered = True
            return

        # Handle normal task execution results
        if isinstance(result, TaskExecutionError):
            # Task failed - release lock (not completed) and notify registry
            await release_lock_for_task(task, completed=False)
            try:
                await registry.task_fail_aio(build_id, task, str(result))
            except Exception as reg_err:
                handle_registry_error(
                    reg_err,
                    f"Failed to notify registry of task {task.id} failure",
                    on_registry_failure,
                )
            state.exception = result.exception
            task_count.failed += 1
            error = result.exception
            if fail_mode == FailMode.FAIL_FAST:
                fail_fast_triggered = True

        elif isinstance(result, BaseException):
            # Backward compat: custom executor returned a bare exception
            await release_lock_for_task(task, completed=False)
            try:
                await registry.task_fail_aio(build_id, task, str(result))
            except Exception as reg_err:
                handle_registry_error(
                    reg_err,
                    f"Failed to notify registry of task {task.id} failure",
                    on_registry_failure,
                )
            state.exception = result
            task_count.failed += 1
            error = result
            if fail_mode == FailMode.FAIL_FAST:
                fail_fast_triggered = True

        elif result is None:
            # Task completed - release lock (completed) and notify registry.
            # When the worker self-reports lifecycle events, it has already
            # emitted TASK_COMPLETED and uploaded artifacts from inside the
            # worker (which also survives an orchestrator crash mid-await).
            await release_lock_for_task(task, completed=True)
            if not task_executor.reports_lifecycle(task):
                try:
                    await registry.task_complete_aio(build_id, task)
                except Exception as reg_err:
                    handle_registry_error(
                        reg_err,
                        f"Failed to notify registry of task {task.id} completion",
                        on_registry_failure,
                    )
                # Upload artifacts if any
                try:
                    artifacts = await task.artifacts_aio()
                    if artifacts:
                        await registry.task_upload_artifacts_aio(
                            build_id, task, artifacts
                        )
                except Exception as artifact_err:
                    handle_registry_error(
                        artifact_err,
                        f"Failed to collect/upload artifacts for task {task.id}",
                        on_registry_failure,
                    )
            state.completed = True
            completion_cache.add(task.id)
            completion_events[task.id].set()
            task_count.succeeded += 1

        else:
            # Dynamic deps returned (TaskStruct) - task is suspended
            # Note: Lock is still held - release on final completion/failure
            dynamic_deps = flatten_task_struct(result)

            # Notify registry that task is suspended waiting for dynamic deps
            # (the worker already emitted TASK_SUSPENDED when it self-reports;
            # dep registration/edges below stay engine-side either way — the
            # engine must discover the full requires() subtrees anyway).
            if not task_executor.reports_lifecycle(task):
                try:
                    await registry.task_suspend_aio(build_id, task)
                except Exception as reg_err:
                    handle_registry_error(
                        reg_err,
                        f"Failed to notify registry of task {task.id} suspension",
                        on_registry_failure,
                    )

            # Discover any new dynamic deps FIRST (which post-order-collects
            # them and their requires() subtree into pending_registrations).
            # Then bulk-register the new batch BEFORE recording the edge —
            # this way the upstream row exists when the edge insert runs,
            # and _reconcile_dependency_edges doesn't have to phantom-
            # create it.
            for dep in dynamic_deps:
                if dep.id not in task_states:
                    await discover(dep)
            await flush_pending_registrations()

            # Now record yielded deps as edges so the DAG view shows them.
            # This is the ONLY place dynamic edges enter the registry —
            # static deps are recorded by task_register via task.requires().
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

    async def wait_for_completion_with_retry(task: BaseTask) -> bool:
        """Wait for task.complete_aio() to return True (handles eventual consistency).

        When the lock reports ALREADY_COMPLETED, the task output may not be
        immediately visible due to eventual consistency (e.g., S3). This function
        retries until the output exists or timeout is reached.
        """
        assert global_lock_config is not None
        timeout = global_lock_config.completion_retry_timeout_seconds
        interval = global_lock_config.completion_retry_interval_seconds
        start_time = asyncio.get_event_loop().time()

        while True:
            if await task.complete_aio():
                return True
            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed >= timeout:
                logger.warning(
                    f"Task {task.id} reported as completed by lock service, "
                    f"but complete_aio() returned False after {timeout}s. "
                    "Treating as complete (eventual consistency)."
                )
                return True
            await asyncio.sleep(interval)

    async def release_lock_for_task(task: BaseTask, completed: bool) -> None:
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
            _stop_lock_renewal(task_id)

    async def acquire_lock_with_completion_check(
        task: BaseTask,
        task_id: str,
        lock_manager: GlobalConcurrencyLockManager,
        config: GlobalLockConfig,
    ) -> LockAcquisitionResult:
        """Acquire lock with retry/backoff and external completion checking.

        During the retry loop, we also check if the task was completed externally
        (e.g., by another process). This handles the race condition where:
        1. Lock is held by another process
        2. That process completes the task and releases the lock
        3. Before we can re-acquire, we should notice the task is complete
        """
        timeout = config.lock_wait_timeout_seconds
        current_interval = config.lock_wait_initial_interval_seconds
        max_interval = config.lock_wait_max_interval_seconds
        backoff_factor = config.lock_wait_backoff_factor
        state = task_states[task.id]
        notified_waiting = False  # Track if we've already notified registry

        loop = asyncio.get_event_loop()
        start_time = loop.time()

        while True:
            # Try to acquire the lock
            result = await lock_manager.acquire(task_id)

            if result.status == LockAcquisitionStatus.ACQUIRED:
                # Clear waiting flag if we were waiting
                state.waiting_for_lock = False
                return result

            if result.status == LockAcquisitionStatus.ALREADY_COMPLETED:
                state.waiting_for_lock = False
                return result

            if result.status == LockAcquisitionStatus.ERROR:
                state.waiting_for_lock = False
                return result

            # HELD_BY_OTHER or CONCURRENCY_LIMIT_REACHED - retry with backoff
            # Mark task as waiting for lock and notify registry (once)
            if not notified_waiting:
                state.waiting_for_lock = True
                notified_waiting = True
                lock_owner = result.error_message  # May contain owner info
                try:
                    await registry.task_waiting_for_lock_aio(build_id, task, lock_owner)
                except Exception as e:
                    handle_registry_error(
                        e,
                        f"Failed to notify registry of lock wait for task {task.id}",
                        on_registry_failure,
                    )

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
                state.waiting_for_lock = False
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

    def _start_lock_renewal(task_id_str: str) -> None:
        """Keep the (deprecated) global lock alive under long tasks.

        The engine uses the lock manager's low-level ``acquire`` (60s TTL,
        no renewal); without this loop a task outliving the TTL silently
        loses its lock. Managers without a public ``renew`` are left as-is.
        """
        renew = getattr(global_lock_manager, "renew", None)
        if renew is None or task_id_str in lock_renewals:
            return

        async def _renewal_loop() -> None:
            while True:
                await asyncio.sleep(_LOCK_RENEWAL_INTERVAL_SECONDS)
                try:
                    renewed = await renew(task_id_str)
                    if not renewed:
                        logger.warning(
                            f"Failed to renew global lock for task "
                            f"{task_id_str}; it may have been lost."
                        )
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning(
                        f"Error renewing global lock for task {task_id_str}: {e}"
                    )

        lock_renewals[task_id_str] = asyncio.create_task(
            _renewal_loop(), name=f"lock-renewal-{task_id_str}"
        )

    def _stop_lock_renewal(task_id_str: str) -> None:
        renewal = lock_renewals.pop(task_id_str, None)
        if renewal is not None:
            renewal.cancel()

    _ALREADY_COMPLETED_RESULT = LockAcquisitionResult(
        status=LockAcquisitionStatus.ALREADY_COMPLETED, acquired=False
    )

    async def _probe_claimed_execution(
        task: BaseTask, executor_name: str, ref: str
    ) -> DetachedExecutionStatus:
        """Probe a claimed execution's liveness, never letting the probe
        itself decide "dead": any probe error degrades to UNKNOWN (wait)
        rather than FAILED — a transient backend blip must not let a loser
        declare a live winner dead and spawn the duplicate the claim exists
        to prevent."""
        try:
            return await task_executor.detached_status(task, executor_name, ref)
        except Exception as e:
            logger.warning(
                f"Liveness probe for claimed execution {ref!r} of task "
                f"{task.id} failed (treating as unknown): {e}"
            )
            return DetachedExecutionStatus.UNKNOWN

    def _claim_holder_has_lapsed(latest_status_expires_at: str | None) -> bool:
        """Whether the winning claim is past its own expiry.

        Evidence, not inference: the server stamped this claim with the
        moment it stops being honoured, and past it the claim is
        re-claimable anyway. None ("never lapses" — an older server, or a
        start recorded before the column) is not evidence of death, so the
        loser keeps waiting exactly as it always has.

        Compares a server-issued timestamp against the local clock, so
        client-server skew shifts the effective moment by that skew; TTLs
        are minutes-to-hours, so seconds of skew are immaterial.
        """
        if not latest_status_expires_at:
            return False
        try:
            expires_at = datetime.fromisoformat(latest_status_expires_at)
        except ValueError:
            return False
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) > expires_at

    async def acquire_claim(
        task: BaseTask,
    ) -> tuple[str, DetachedHandle | LockAcquisitionResult | TaskExecutionError | None]:
        """Claim the task's execution, resolving denials.

        Returns one of:
          - ``("granted", None)``: we won — a claiming TASK_STARTED (without
            ref) is recorded; caller spawns and then records the ref.
          - ``("unclaimed", None)``: the claim call itself failed against
            the registry (warn mode) — the caller proceeds through the
            normal, unclaimed start path (true graceful degradation; no
            state is assumed recorded).
          - ``("attach", handle)``: another live execution won — await it.
          - ``("already_completed", LockAcquisitionResult)``: task is done
            elsewhere; caller returns it so process_result runs the
            eventual-consistency completion retry (same as the lock path).
          - ``("error", LockAcquisitionResult)``: gave up (wait timeout).
            Deliberately NOT a task_fail: the winner may be a legitimately
            long-running ref-less execution — this build records a local
            failure only, without clobbering the task's env-global status
            (which would release the winner's claim to a third build).

        Denial resolution: a live winner is re-attached; a winner whose ref
        probes FAILED on **two probes** (corroborated after a short delay,
        so a single transient error can't kill a live winner) is recorded
        failed and the claim re-tried; a ref-less winner is waited on with
        backoff, unless its claim has lapsed — which covers the winner's
        claim→ref-record crash window without any locally configured guess
        at how long "too long" is. Every branch — including dead-winner
        retries — is bounded by ``wait_timeout_seconds``.
        """
        state = task_states[task.id]
        loop = asyncio.get_event_loop()
        start_time = loop.time()
        interval = claim_cfg.wait_initial_interval_seconds
        notified_waiting = False
        # Ref that probed FAILED once — a second FAILED probe (next
        # iteration, after a delay) corroborates before we record the death.
        suspected_dead_ref: str | None = None
        attempted = False  # always try the claim at least once, even with
        # wait_timeout_seconds=0 ("claim, but don't wait if held")
        try:
            claim_metadata: dict | None = None
            try:
                claim_metadata = await task_executor.get_executor_metadata(task)
            except Exception:
                logger.debug(
                    f"Executor metadata resolution failed for task {task.id}; "
                    "claiming without it.",
                    exc_info=True,
                )
            while True:
                timeout = claim_cfg.wait_timeout_seconds
                if (
                    attempted
                    and timeout is not None
                    and loop.time() - start_time >= timeout
                ):
                    return (
                        "error",
                        LockAcquisitionResult(
                            status=LockAcquisitionStatus.HELD_BY_OTHER,
                            acquired=False,
                            error_message=(
                                f"Claim wait timed out after {timeout}s — the "
                                f"task is (still) claimed by another execution"
                            ),
                        ),
                    )
                attempted = True
                try:
                    result = await registry.task_start_claim_aio(
                        build_id, task, executor_metadata=claim_metadata
                    )
                except Exception as claim_err:
                    # Registry hiccup on the claim itself: in `warn` mode
                    # fall back to the normal unclaimed start path (nothing
                    # is assumed recorded); `raise` mode propagates.
                    handle_registry_error(
                        claim_err,
                        f"Claim start failed for task {task.id}",
                        on_registry_failure,
                    )
                    return ("unclaimed", None)
                if result.started:
                    return ("granted", None)
                if result.denied_reason == "already_completed":
                    return ("already_completed", _ALREADY_COMPLETED_RESULT)

                # already_running: try to resolve via the winner's ref.
                if result.executor and result.executor_ref:
                    try:
                        attach_handle = await task_executor.reattach(
                            task, result.executor, result.executor_ref
                        )
                    except Exception as e:
                        logger.warning(
                            f"Re-attach to claimed execution "
                            f"{result.executor_ref!r} for task {task.id} "
                            f"failed: {e}"
                        )
                        attach_handle = None
                    if attach_handle is not None:
                        logger.info(
                            f"Claim for task {task.id} lost — re-attached to "
                            f"the winning execution {attach_handle.ref!r}."
                        )
                        return ("attach", attach_handle)
                    probe = await _probe_claimed_execution(
                        task, result.executor, result.executor_ref
                    )
                    if probe == DetachedExecutionStatus.FAILED:
                        if suspected_dead_ref != result.executor_ref:
                            # First FAILED probe: corroborate after a short
                            # delay before declaring the winner dead.
                            suspected_dead_ref = result.executor_ref
                            await asyncio.sleep(
                                min(claim_cfg.wait_initial_interval_seconds, 1.0)
                            )
                            continue
                        suspected_dead_ref = None
                        if await task.complete_aio():
                            return ("already_completed", _ALREADY_COMPLETED_RESULT)
                        try:
                            await registry.task_fail_aio(
                                build_id,
                                task,
                                "Claimed execution is gone (observed by a "
                                "competing claimant)",
                            )
                        except Exception as reg_err:
                            handle_registry_error(
                                reg_err,
                                f"Failed to record dead claimed execution for "
                                f"task {task.id}",
                                on_registry_failure,
                            )
                        # Small pause before re-claiming so a persistently
                        # failing /fail endpoint can't spin this loop hot
                        # (the deadline above still bounds it).
                        await asyncio.sleep(claim_cfg.wait_initial_interval_seconds)
                        continue
                    suspected_dead_ref = None
                elif _claim_holder_has_lapsed(result.latest_status_expires_at):
                    # Ref-less holder whose claim has lapsed — e.g. a winner
                    # that crashed between its claim and its ref-recording
                    # start. The expiry is the server's own statement that
                    # the claim is no longer honoured, so recover like a
                    # dead ref rather than waiting out the timeout.
                    if await task.complete_aio():
                        return ("already_completed", _ALREADY_COMPLETED_RESULT)
                    try:
                        await registry.task_fail_aio(
                            build_id,
                            task,
                            "Ref-less claimed execution's claim has lapsed "
                            "(observed by a competing claimant)",
                        )
                    except Exception as reg_err:
                        handle_registry_error(
                            reg_err,
                            f"Failed to record stale claimed execution for "
                            f"task {task.id}",
                            on_registry_failure,
                        )
                    await asyncio.sleep(claim_cfg.wait_initial_interval_seconds)
                    continue

                # Wait for external completion, re-trying the claim with
                # backoff.
                if not notified_waiting:
                    notified_waiting = True
                    state.waiting_for_lock = True
                    try:
                        await registry.task_waiting_for_lock_aio(
                            build_id, task, result.executor_ref
                        )
                    except Exception:
                        logger.debug(
                            f"Failed to record waiting state for task {task.id}",
                            exc_info=True,
                        )
                if await task.complete_aio():
                    return ("already_completed", _ALREADY_COMPLETED_RESULT)
                await asyncio.sleep(interval)
                interval = min(
                    interval * claim_cfg.wait_backoff_factor,
                    claim_cfg.wait_max_interval_seconds,
                )
        finally:
            state.waiting_for_lock = False

    async def registry_task_start(
        task: BaseTask, handle: DetachedHandle | None
    ) -> None:
        """Emit TASK_STARTED, with the detached-execution ref when present."""
        if handle is None:
            await registry.task_start_aio(build_id, task)
            return
        await registry.task_start_aio(
            build_id,
            task,
            executor=handle.executor,
            executor_ref=handle.ref,
            executor_metadata=handle.executor_metadata,
        )

    async def submit_with_lock(
        task: BaseTask,
    ) -> LockAcquisitionResult | TaskExecutionError | TaskStruct | None:
        """Submit task for execution, acquiring lock first if enabled.

        This wraps lock acquisition + task execution as a single async unit,
        allowing the main loop to remain non-blocking while waiting for locks.

        Returns:
            - LockAcquisitionResult: If lock was not acquired (ALREADY_COMPLETED,
                ERROR, or timeout). The task was NOT executed.
            - TaskExecutionError | TaskStruct | None: Normal task result if lock was
                acquired (or locking wasn't needed) and task was executed.
        """
        state = task_states[task.id]
        use_lock = global_lock_manager is not None and lock_selector(task)

        if use_lock:
            assert global_lock_manager is not None  # For type checker
            task_id_str = str(task.id)

            # Always acquire lock (even if we think we hold it from a previous
            # dynamic deps yield). This handles:
            # 1. Fresh task execution - normal acquire
            # 2. Task resuming after dynamic deps - re-acquire is safe since
            #    we're the same owner, and handles the case where lock expired
            #    during the wait for deps
            lock_result = await acquire_lock_with_completion_check(
                task, task_id_str, global_lock_manager, global_lock_config
            )

            if lock_result.status != LockAcquisitionStatus.ACQUIRED:
                # Lock not acquired - return the lock result for handling
                return lock_result

            # Lock acquired - track it for release later (with background
            # TTL renewal so long tasks don't outlive the lease).
            held_locks.add(task_id_str)
            _start_lock_renewal(task_id_str)

        # Atomic execution claim (on by default for probeable executions;
        # see build_aio's ``claim``): the claim-start happens BEFORE the
        # concurrency slot and the spawn, so a losing racer never occupies
        # a worker or slot — it re-attaches to the winner instead. Note the
        # placement mirrors the global lock above and precedes the limiter
        # deliberately: a registry-backed limiter records its own enforced
        # start inside ``slot()``, which would otherwise deny our claim as
        # "already running".
        claimed = False
        claim_handle: DetachedHandle | None = None
        if claim_enabled_for(task) and not state.completed:
            if not state.registered:
                # The claim start 404s on unregistered tasks — retry the
                # registration first (same warn-mode protection as below).
                try:
                    await registry.task_register_aio(build_id, task)
                    state.registered = True
                except Exception as reg_err:
                    handle_registry_error(
                        reg_err,
                        f"Failed to register task {task.id} before claim",
                        on_registry_failure,
                    )
            if state.registered:
                claim_kind, claim_value = await acquire_claim(task)
                if claim_kind == "attach":
                    claim_handle = cast(DetachedHandle, claim_value)
                elif claim_kind == "already_completed":
                    return cast(LockAcquisitionResult, claim_value)
                elif claim_kind == "error":
                    return cast(LockAcquisitionResult, claim_value)
                elif claim_kind == "granted":
                    # A claiming TASK_STARTED (no ref) is durably recorded.
                    claimed = True
                    state.started = True
                else:
                    # "unclaimed": the claim call failed against the registry
                    # (warn mode). Nothing was recorded — fall through to the
                    # normal, unclaimed start path.
                    assert claim_kind == "unclaimed", claim_kind

        # Now we have the lock/claim (or neither was needed). Acquire a
        # concurrency slot before spawning; the slot sits *after* the global
        # lock and the claim so tasks don't burn execution slots while
        # blocked on either, and it is released automatically on every exit
        # path (completion, failure, cancellation, dynamic-deps suspension).
        # Note: with claims on, a CLAIMED task queued behind a local limiter
        # slot already shows RUNNING (without a ref) in the registry — the
        # claim must precede the slot so a losing racer never occupies one,
        # and a registry-backed limiter's own enforced start would otherwise
        # deny our claim as already-running. Unclaimed tasks keep the old
        # behavior (not shown started while queued).
        async with contextlib.AsyncExitStack() as slot_stack:
            try:
                await slot_stack.enter_async_context(limiter.slot(task))
            except Exception as e:
                # A limiter enforcing external state can fail acquisition
                # (e.g. the registry-backed limiter's max_wait timeout or a
                # non-transient registry error) — that fails THIS task,
                # never the whole build.
                return TaskExecutionError(
                    exception=e,
                    traceback="".join(tb_module.format_exception(e)),
                )
            # Detached execution: first try re-attaching to a live execution
            # recorded in the registry (from a previous orchestrator run, or
            # a concurrently running build); otherwise spawn detached when
            # the executor supports it for this task. Detached executions
            # survive this process, so their (executor, ref) is recorded with
            # the TASK_STARTED event below — before we block on the result.
            handle: DetachedHandle | None = claim_handle
            detached_ref = detached_refs.pop(task.id, None)
            if handle is None and detached_ref is not None:
                ref_executor, ref = detached_ref
                try:
                    handle = await task_executor.reattach(task, ref_executor, ref)
                except Exception as e:
                    logger.warning(
                        f"Re-attach to detached execution {ref!r} for task "
                        f"{task.id} failed; falling back to execution: {e}"
                    )
                    handle = None
                if handle is not None:
                    logger.info(
                        f"Re-attached to running detached execution "
                        f"{handle.ref!r} for task {task.id}"
                    )
            if handle is None and task_executor.supports_detached(task):
                try:
                    handle = await task_executor.submit_detached(task)
                except Exception as e:
                    return TaskExecutionError(
                        exception=e,
                        traceback="".join(tb_module.format_exception(e)),
                    )

            # Start the task in registry. The task was already registered during
            # discover; if registration failed in `warn` mode we retry once here
            # so the /start endpoint doesn't 404.
            if not state.started:
                if not state.registered:
                    try:
                        await registry.task_register_aio(build_id, task)
                        state.registered = True
                    except Exception as reg_err:
                        handle_registry_error(
                            reg_err,
                            f"Failed to register task {task.id} before start",
                            on_registry_failure,
                        )
                # Skip /start if registration never succeeded — the endpoint
                # would 404 and that hard-fails the build even in `warn` mode.
                #
                # Even when the worker self-reports lifecycle events
                # (reports_lifecycle), the engine still emits TASK_STARTED
                # for detached spawns: it lands immediately (the worker's own
                # start event only fires once its container is up, which can
                # be minutes later under cold start) so a crash in between
                # still leaves a re-attachable ref. Duplicate started events
                # are tolerated by the registry.
                if state.registered:
                    try:
                        await registry_task_start(task, handle)
                        state.started = True
                    except Exception as reg_err:
                        handle_registry_error(
                            reg_err,
                            f"Failed to start task {task.id}",
                            on_registry_failure,
                        )
            elif claimed and handle is not None:
                # We won the claim (whose start carried no ref — arbitration
                # happens before the spawn): record the executor ref with a
                # plain, tolerated-duplicate start.
                try:
                    await registry_task_start(task, handle)
                except Exception as reg_err:
                    handle_registry_error(
                        reg_err,
                        f"Failed to record executor ref for claimed task {task.id}",
                        on_registry_failure,
                    )
            elif state.dynamic_deps:
                # Task was suspended waiting for dynamic deps, now resuming. Same
                # warn-mode protection: no point firing /resume if registration
                # never landed. When the worker self-reports, skip the resume
                # event — the worker's own TASK_STARTED re-asserts RUNNING and
                # carries the re-invocation's fresh executor ref (making the
                # re-spawn re-attachable, which TASK_RESUMED wouldn't).
                if (
                    state.registered
                    and not claimed
                    and not task_executor.reports_lifecycle(task)
                ):
                    try:
                        await registry.task_resume_aio(build_id, task)
                    except Exception as reg_err:
                        handle_registry_error(
                            reg_err,
                            f"Failed to resume task {task.id}",
                            on_registry_failure,
                        )

            # Await the detached execution, or execute via the executor
            if handle is not None:
                return await handle.wait()
            return await task_executor.submit(task)

    async def cancel_pending_in_flight() -> None:
        """Cancel any still-running ``pending_futures`` and emit ``task_cancel``.

        Called when escalating FAIL_FAST after the first failure, so
        sibling tasks running concurrently (e.g. on Modal) terminate
        promptly instead of being abandoned. Idempotent — safe to call
        multiple times.

        Race-aware: the awaits inside ``process_result`` (registry calls,
        artifact upload) can let other futures finish in the same loop
        iteration. Those already-done futures are processed normally so
        their actual outcome is recorded — only the futures still
        running get cancel-marked.

        The asyncio cancel is what propagates into the executor (e.g.
        Modal's remote-call cancel); the explicit ``task_executor.cancel``
        hook is for executors that need extra teardown (default no-op).
        """
        if not pending_futures:
            return
        snapshot = dict(pending_futures)
        pending_futures.clear()

        # Split: futures that already completed while process_result was
        # awaiting (treat as their actual outcome) vs still-running ones
        # (cancel and mark TASK_CANCELLED).
        completed_tids: list[UUID] = []
        running_tids: list[UUID] = []
        for tid, fut in snapshot.items():
            if fut.done():
                completed_tids.append(tid)
            else:
                running_tids.append(tid)

        for tid in completed_tids:
            executing.discard(tid)
            done_task = task_states[tid].task
            try:
                done_result = snapshot[tid].result()
            except Exception as e:
                done_result = TaskExecutionError(
                    exception=e,
                    traceback="".join(tb_module.format_exception(e)),
                )
            await process_result(done_task, done_result)

        if not running_tids:
            return

        cancelled_tasks: list[BaseTask] = [
            task_states[tid].task for tid in running_tids
        ]
        for tid in running_tids:
            snapshot[tid].cancel()
        for cancel_task in cancelled_tasks:
            try:
                await task_executor.cancel(cancel_task)
            except Exception as e:
                logger.warning(f"Executor cancel failed for {cancel_task.id}: {e}")
        # Drain so cancelled futures don't dangle.
        await asyncio.gather(
            *(snapshot[tid] for tid in running_tids), return_exceptions=True
        )
        for tid in running_tids:
            executing.discard(tid)
        for cancel_task in cancelled_tasks:
            await release_lock_for_task(cancel_task, completed=False)
            state = task_states[cancel_task.id]
            # Only call /cancel when the task was successfully registered;
            # otherwise the endpoint 404s (warn-mode tolerates registration
            # failures). Mirrors the guard pattern around /start, /resume.
            if state.registered:
                try:
                    await registry.task_cancel_aio(build_id, cancel_task)
                except Exception as reg_err:
                    handle_registry_error(
                        reg_err,
                        f"Failed to mark task {cancel_task.id} as cancelled",
                        on_registry_failure,
                    )
            if state.exception is None:
                state.exception = asyncio.CancelledError(
                    "Cancelled by build engine in FAIL_FAST mode"
                )
            task_count.cancelled += 1

    async def emit_skips_for_blocked_tasks() -> None:
        """Emit ``task_skip_aio`` for tasks blocked by failed/cancelled deps.

        Iterates to a fixed point so transitive descendants are also
        skipped. Idempotent: tasks already marked with ``state.exception``
        are filtered out, so re-calling is a no-op.
        """
        while True:
            skipped_this_pass = 0
            for state in task_states.values():
                if state.completed or state.exception is not None:
                    continue
                blocked = any(
                    task_states[dep.id].exception is not None for dep in state.all_deps
                )
                if not blocked:
                    continue
                if state.registered:
                    try:
                        await registry.task_skip_aio(build_id, state.task)
                    except Exception as reg_err:
                        handle_registry_error(
                            reg_err,
                            f"Failed to mark task {state.task.id} as skipped",
                            on_registry_failure,
                        )
                state.exception = _SkippedSentinel()
                task_count.skipped += 1
                skipped_this_pass += 1
            if skipped_this_pass == 0:
                return

    try:
        # Discover all tasks from roots concurrently. Inline-registration
        # makes the full DAG appear in the registry/UI immediately. If any
        # discover() raises (e.g. a task's requires() throws), the outer
        # except below emits build_fail_aio so the build doesn't get stuck
        # in RUNNING state.
        async with asyncio.TaskGroup() as tg:
            for root in tasks:
                tg.create_task(discover(root))

        # Bulk-register every discovered task in one HTTP call. Order is
        # post-order so the API resolves all dependency_task_ids to existing
        # rows without phantom-creating any.
        await flush_pending_registrations()

        # Mark previously-completed tasks as complete (concurrently). Has to
        # happen after registration so the API rows exist; previously_completed
        # is populated during discover() above.
        if previously_completed_tasks:
            async with asyncio.TaskGroup() as tg:
                for task in previously_completed_tasks:
                    tg.create_task(mark_previously_completed(task))

        await task_executor.setup()

        # Main build loop using as_completed pattern
        while True:
            # Check if all roots complete
            all_roots_complete = all(task_states[root.id].completed for root in tasks)
            if all_roots_complete:
                break

            # Find and submit ready tasks
            ready = find_ready_tasks()

            # Submit ready tasks (lock acquisition + execution as single async unit)
            for task in ready:
                async_task = asyncio.create_task(submit_with_lock(task))
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
                    result = TaskExecutionError(
                        exception=e,
                        traceback="".join(tb_module.format_exception(e)),
                    )
                await process_result(task, result)

            # Stop scheduling once any task has flagged FAIL_FAST escalation.
            # Sibling completions in this same `done` batch were already
            # processed above so their task_complete_aio events landed.
            if fail_fast_triggered and fail_mode == FailMode.FAIL_FAST:
                break

            # Check for exit-early condition: all remaining tasks waiting for locks
            if global_lock_config.exit_early_when_all_locked:
                remaining_tasks = [
                    s
                    for s in task_states.values()
                    if not s.completed and s.exception is None
                ]
                if remaining_tasks:
                    all_waiting = all(s.waiting_for_lock for s in remaining_tasks)
                    if all_waiting:
                        reason = (
                            f"All {len(remaining_tasks)} remaining tasks "
                            "are running in other builds"
                        )
                        logger.info(f"Exiting early: {reason}")
                        try:
                            await registry.build_exit_early_aio(build_id, reason)
                        except Exception as reg_err:
                            handle_registry_error(
                                reg_err,
                                "Failed to notify registry of exit early",
                                on_registry_failure,
                            )
                        return BuildSummary(
                            status=BuildExitStatus.EXIT_EARLY,
                            task_count=task_count,
                            build_id=build_id,
                            error=None,
                        )

        # If FAIL_FAST escalated, terminate in-flight siblings before
        # finalising. asyncio.cancel propagates CancelledError into
        # cooperative awaitables (e.g. Modal's remote.aio), so remote
        # containers stop billing rather than being abandoned. Skips
        # are emitted for any task whose deps include a failed/cancelled
        # task — covers both modes (no-op when nothing is blocked).
        if fail_fast_triggered and fail_mode == FailMode.FAIL_FAST:
            await cancel_pending_in_flight()
        await emit_skips_for_blocked_tasks()

        # FAIL_FAST: re-enter the outer except so build_fail_aio fires.
        if error is not None and fail_mode == FailMode.FAIL_FAST:
            raise error

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
        # Cancel any in-flight (covers exceptions raised mid-loop) and
        # emit skips before recording the build failure. Both helpers
        # are idempotent so it's safe even when reached via the
        # FAIL_FAST escalation path that already called them.
        try:
            await cancel_pending_in_flight()
            await emit_skips_for_blocked_tasks()
        except Exception as cleanup_err:
            logger.warning(f"Error during build cleanup: {cleanup_err}")
        await registry.build_fail_aio(build_id, str(e))
        if fail_mode == FailMode.FAIL_FAST:
            raise
        return BuildSummary(
            status=BuildExitStatus.FAILURE,
            task_count=task_count,
            build_id=build_id,
            error=e,
        )

    finally:
        current_build_id_var.reset(_build_id_token)
        if lock_renewals:
            for renewal in lock_renewals.values():
                renewal.cancel()
            await asyncio.gather(*lock_renewals.values(), return_exceptions=True)
            lock_renewals.clear()
        await task_executor.teardown()


# =============================================================================
# Convenience wrapper for sync callers
# =============================================================================


def build(
    tasks: Sequence[BaseTask] | BaseTask,
    task_executor: TaskExecutorABC | None = None,
    fail_mode: FailMode = FailMode.FAIL_FAST,
    registry: RegistryABC | None = None,
    max_concurrent_discover: int = 50,
    global_lock_manager: GlobalConcurrencyLockManager | None = None,
    global_lock_config: GlobalLockConfig | None = None,
    resume_build_id: UUID | None = None,
    register_all: bool = False,
    on_registry_failure: OnRegistryFailure = "raise",
    concurrency_config: ConcurrencyConfig | None = None,
    concurrency_limiter: ConcurrencyLimiter | None = None,
    claim: bool | None = None,
    claim_config: ClaimConfig | None = None,
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
        return asyncio.run(
            build_aio(
                tasks,
                task_executor=task_executor,
                fail_mode=fail_mode,
                registry=registry,
                max_concurrent_discover=max_concurrent_discover,
                global_lock_manager=global_lock_manager,
                global_lock_config=global_lock_config,
                resume_build_id=resume_build_id,
                register_all=register_all,
                on_registry_failure=on_registry_failure,
                concurrency_config=concurrency_config,
                concurrency_limiter=concurrency_limiter,
                claim=claim,
                claim_config=claim_config,
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
