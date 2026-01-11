"""Base interfaces and data structures for the build system.

This module contains:
- Data structures: BuildExitStatus, TaskCount, BuildSummary, FailMode
- Execution mode types: ExecutionMode, ExecutionModeSelector, DefaultExecutionModeSelector
- Task state tracking: TaskExecutionState
- Task runner protocol: TaskRunnerABC
- Global concurrency lock: GlobalConcurrencyLock, LockConfig, LockAcquisitionResult
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Callable, Generator, Literal, Protocol

from stardag._task import (
    BaseTask,
    TaskStruct,
    _has_custom_run,
    _has_custom_run_aio,
)

if TYPE_CHECKING:
    pass


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
# Run Wrapper Protocol
# =============================================================================


class RunWrapper(Protocol):
    """Protocol for wrapping task execution.

    A RunWrapper is responsible for actually executing the task's run method.
    This can be used to:
    - Transfer execution to a remote system (e.g., Modal)
    - Add logging/metrics around task execution
    - Implement custom retry logic

    The RunWrapper is NOT responsible for:
    - Registry calls (start_task, complete_task, etc.) - handled by TaskRunner
    - Dependency resolution - handled by build()

    Return types support two patterns for dynamic dependencies:

    1. **Generator (in-process)**: When task executes in same process (thread/async),
       the generator can be suspended and resumed. TaskRunner stores the generator
       and resumes it after dynamic deps complete.

    2. **TaskStruct (cross-process/remote)**: When task executes in subprocess or
       remote system, generators cannot be pickled. Instead, return the yielded
       TaskStruct directly. TaskRunner will re-execute the task from scratch after
       dynamic deps complete (idempotent re-execution pattern).
    """

    async def run(
        self, task: BaseTask
    ) -> Generator[TaskStruct, None, None] | TaskStruct | None:
        """Execute the task's run method.

        Args:
            task: The task to execute.

        Returns:
            - None: Task completed successfully with no dynamic dependencies.
            - Generator: Task has dynamic dependencies (yielded TaskStruct) and is
                suspended in the current process. Can be resumed after deps complete.
            - TaskStruct: Task has dynamic dependencies but cannot be suspended
                (e.g., executed in subprocess/remote). Task should be re-executed
                after deps complete (idempotent re-execution).

        Raises:
            Any exception from task execution is propagated.
        """
        ...


class DefaultRunWrapper:
    """Default run wrapper that executes tasks locally.

    Uses task.run_aio() if available, otherwise falls back to
    asyncio.to_thread(task.run) for sync-only tasks.

    Note: This wrapper returns generators for dynamic deps since it executes
    in the same process where generators can be suspended and resumed.
    """

    async def run(
        self, task: BaseTask
    ) -> Generator[TaskStruct, None, None] | TaskStruct | None:
        """Execute task using async if available, else via thread."""
        if _has_custom_run_aio(task):
            return await task.run_aio()
        elif _has_custom_run(task):
            import asyncio

            return await asyncio.to_thread(task.run)
        else:
            raise ValueError(f"Task {task} has no run method")


# =============================================================================
# Global Concurrency Lock
# =============================================================================


class LockAcquisitionStatus(StrEnum):
    """Status of a lock acquisition attempt."""

    ACQUIRED = "acquired"
    ALREADY_COMPLETED = "already_completed"
    HELD_BY_OTHER = "held_by_other"
    CONCURRENCY_LIMIT_REACHED = "concurrency_limit_reached"
    ERROR = "error"


@dataclass
class LockAcquisitionResult:
    """Result of a lock acquisition attempt."""

    status: LockAcquisitionStatus
    acquired: bool
    error_message: str | None = None


class GlobalConcurrencyLock(Protocol):
    """Protocol for global distributed locks.

    Implementations provide distributed locking for task execution across
    multiple build processes/instances. This enables "exactly once" execution
    guarantees globally (not just within a single build).

    The default implementation uses the Stardag Registry API with PostgreSQL
    backend. Users can implement custom solutions using Redis, DynamoDB, etc.
    """

    async def acquire(
        self,
        task_id: str,
        owner_id: str,
        ttl_seconds: int = 60,
        check_task_completion: bool = True,
    ) -> LockAcquisitionResult:
        """Acquire a lock for a task.

        Args:
            task_id: The task identifier (hash).
            owner_id: UUID identifying the lock owner (stable across retries).
            ttl_seconds: Time-to-live in seconds for the lock.
            check_task_completion: If True, check if task is already completed
                in the registry before acquiring.

        Returns:
            LockAcquisitionResult with status:
            - ACQUIRED: Lock acquired successfully
            - ALREADY_COMPLETED: Task has completion record in registry
            - HELD_BY_OTHER: Lock held by another owner
            - CONCURRENCY_LIMIT_REACHED: Workspace limit reached
            - ERROR: Unexpected error
        """
        ...

    async def renew(
        self,
        task_id: str,
        owner_id: str,
        ttl_seconds: int = 60,
    ) -> bool:
        """Renew a lock's TTL.

        Args:
            task_id: The task identifier.
            owner_id: The expected owner.
            ttl_seconds: New TTL in seconds.

        Returns:
            True if successfully renewed, False otherwise.
        """
        ...

    async def release(
        self,
        task_id: str,
        owner_id: str,
        task_completed: bool = False,
        build_id: str | None = None,
    ) -> bool:
        """Release a lock.

        Args:
            task_id: The task identifier.
            owner_id: The expected owner.
            task_completed: If True and build_id provided, record task completion
                in the same transaction.
            build_id: Build ID for completion recording.

        Returns:
            True if successfully released, False otherwise.
        """
        ...

    async def check_task_completed(
        self,
        task_id: str,
    ) -> bool:
        """Check if a task is registered as completed in the registry.

        Args:
            task_id: The task identifier.

        Returns:
            True if task has a completion record.
        """
        ...


@dataclass
class GlobalLockConfig:
    """Configuration for global concurrency locking.

    Attributes:
        enabled: Whether to use global locking. Can be:
            - True: Lock all tasks
            - False: Lock no tasks (default)
            - Callable: Function that returns True/False for each task
        ttl_seconds: Lock time-to-live in seconds. Choose conservatively;
            locks auto-expire and can be taken over.
        renewal_interval_seconds: How often to renew locks during execution.
            Set to None to disable auto-renewal.
        wait_timeout_seconds: Max time to wait for lock acquisition.
            Set to None to fail immediately if lock is held.
        block_on_workspace_limit: If True, wait when workspace concurrency
            limit is reached. If False, fail immediately.
        completion_retry_timeout_seconds: Max time to retry task.complete()
            when registry shows completed but target doesn't exist yet
            (handles S3 eventual consistency).
        completion_retry_interval_seconds: Interval between completion retries.
    """

    enabled: bool | Callable[[BaseTask], bool] = False
    ttl_seconds: int = 60
    renewal_interval_seconds: int | None = 30
    wait_timeout_seconds: float | None = 300
    block_on_workspace_limit: bool = True
    completion_retry_timeout_seconds: float = 30
    completion_retry_interval_seconds: float = 1.0


class GlobalLockSelector(Protocol):
    """Protocol for selecting whether a task should use global locking."""

    def __call__(self, task: BaseTask) -> bool:
        """Return True if task should use global lock."""
        ...


class DefaultGlobalLockSelector:
    """Default selector that uses GlobalLockConfig.enabled to determine locking.

    If enabled is a callable, it's called for each task.
    Otherwise, the boolean value is used for all tasks.
    """

    def __init__(self, config: GlobalLockConfig) -> None:
        self.config = config

    def __call__(self, task: BaseTask) -> bool:
        if callable(self.config.enabled):
            return self.config.enabled(task)
        return self.config.enabled


# =============================================================================
# Task Runner Protocol
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


__all__ = [
    "BuildExitStatus",
    "BuildSummary",
    "DefaultExecutionModeSelector",
    "DefaultGlobalLockSelector",
    "DefaultRunWrapper",
    "ExecutionMode",
    "ExecutionModeSelector",
    "FailMode",
    "GlobalConcurrencyLock",
    "GlobalLockConfig",
    "GlobalLockSelector",
    "LockAcquisitionResult",
    "LockAcquisitionStatus",
    "RunWrapper",
    "TaskCount",
    "TaskExecutionState",
    "TaskRunnerABC",
]
