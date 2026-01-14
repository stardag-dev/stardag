"""Base interfaces and data structures for the build system.

This module contains:
- Data structures: BuildExitStatus, TaskCount, BuildSummary, FailMode
- Task state tracking: TaskExecutionState
- Task executor protocol: TaskExecutorABC
- Global concurrency lock: GlobalConcurrencyLock, GlobalLockConfig, LockAcquisitionResult
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Callable, Generator, Generic, Protocol, TypeVar

from stardag._task import (
    BaseTask,
    TaskStruct,
)


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
    """Summary of a build execution."""

    status: BuildExitStatus
    task_count: TaskCount
    error: BaseException | None = None

    def __repr__(self) -> str:
        """Return a human-readable summary of the build."""
        tc = self.task_count
        status_icon = "✓" if self.status == BuildExitStatus.SUCCESS else "✗"
        lines = [
            f"Build {self.status.value.upper()} {status_icon}",
            f"  Discovered: {tc.discovered}",
            f"  Previously completed: {tc.previously_completed}",
            f"  Succeeded: {tc.succeeded}",
            f"  Failed: {tc.failed}",
        ]
        if tc.pending > 0:
            lines.append(f"  Pending: {tc.pending}")
        if self.error:
            lines.append(f"  Error: {self.error}")
        return "\n".join(lines)


class FailMode(StrEnum):
    """How to handle task failures during build.

    Attributes:
        FAIL_FAST: Stop the build at the first task failure.
        CONTINUE: Continue executing all tasks whose dependencies are met,
            even if some tasks have failed.
    """

    FAIL_FAST = "fail_fast"
    CONTINUE = "continue"


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
    # True when registry.start_task has been called
    started: bool = False
    # True when task execution has fully completed
    completed: bool = False
    # Exception if task failed
    exception: BaseException | None = None

    @property
    def all_deps(self) -> list[BaseTask]:
        return self.static_deps + self.dynamic_deps


# =============================================================================
# Task Executor Protocol
# =============================================================================


class TaskExecutorABC(ABC):
    """Abstract base for task executors.

    Receives tasks and executes them according to some policy. The executor is
    responsible for:
    - Executing tasks in the appropriate context (async/thread/process)
    - Handling generator suspension for dynamic dependencies

    The executor is NOT responsible for:
    - Dependency resolution - handled by build()
    - Registry calls (start_task, complete_task, etc.) - handled by build()
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
        """Teardown any resources used by the task executor."""
        ...


# Type variable for executor routing keys
ExecutorKeyT = TypeVar("ExecutorKeyT")


class RoutedTaskExecutor(TaskExecutorABC, Generic[ExecutorKeyT]):
    """Task executor that routes tasks to different executors based on a router function.

    This enables flexible execution strategies where different tasks can be
    executed by different executors. For example:
    - Route some tasks to Modal for GPU execution
    - Route other tasks to local thread/process pools
    - Route based on task type, resource requirements, etc.

    Example:
        local_executor = HybridConcurrentTaskExecutor()
        modal_executor = ModalTaskExecutor(app_name="my-app")

        routed = RoutedTaskExecutor(
            executors={"local": local_executor, "modal": modal_executor},
            router=lambda task: "modal" if needs_gpu(task) else "local",
        )
        await build_aio([task], task_executor=routed)
    """

    def __init__(
        self,
        executors: dict[ExecutorKeyT, TaskExecutorABC],
        router: Callable[[BaseTask], ExecutorKeyT],
    ) -> None:
        """Initialize the routed executor.

        Args:
            executors: Mapping from routing keys to task executors.
            router: Function that determines which executor to use for each task.
        """
        self.executors = executors
        self.router = router

    async def submit(self, task: BaseTask) -> None | TaskStruct | Exception:
        """Route task to appropriate executor and submit."""
        key = self.router(task)
        executor = self.executors.get(key)
        if executor is None:
            return KeyError(f"No executor found for routing key: {key}")
        return await executor.submit(task)

    async def setup(self) -> None:
        """Setup all child executors."""
        for executor in self.executors.values():
            await executor.setup()

    async def teardown(self) -> None:
        """Teardown all child executors."""
        for executor in self.executors.values():
            await executor.teardown()


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
