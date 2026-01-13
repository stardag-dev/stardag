"""Base interfaces and data structures for the build system.

This module contains:
- Data structures: BuildExitStatus, TaskCount, BuildSummary, FailMode
- Execution mode types: ExecutionMode, ExecutionModeSelector, DefaultExecutionModeSelector
- Task state tracking: TaskExecutionState
- Task executor protocol: TaskExecutorABC
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Generator, Literal, Protocol

from stardag._task import (
    BaseTask,
    TaskStruct,
    _has_custom_run,
    _has_custom_run_aio,
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
    - Registry calls (start_task, complete_task, etc.) - handled by build()
    - Dependency resolution - handled by build()

    Return types support two patterns for dynamic dependencies:

    1. **Generator (in-process)**: When task executes in same process (thread/async),
       the generator can be suspended and resumed. TaskExecutor stores the generator
       and resumes it after dynamic deps complete.

    2. **TaskStruct (cross-process/remote)**: When task executes in subprocess or
       remote system, generators cannot be pickled. Instead, return the yielded
       TaskStruct directly. TaskExecutor will re-execute the task from scratch after
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
        """Teardown any resources used by the task runner."""
        ...
