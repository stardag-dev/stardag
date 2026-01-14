"""Base interfaces and data structures for the build system.

This module contains:
- Data structures: BuildExitStatus, TaskCount, BuildSummary, FailMode
- Task state tracking: TaskExecutionState
- Task executor protocol: TaskExecutorABC
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Callable, Generator, Generic, TypeVar

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
