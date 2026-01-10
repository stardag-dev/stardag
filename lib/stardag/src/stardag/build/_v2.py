"""Task build v2.

Each task can be one of three run-types:
A. Sync-only, i.e., implements `def run(self)`
B. Async-only, i.e., implements `async def run_aio(self)`
C. Dual, i.e., implements both `def run(self)` and `async def run_aio(self)`

"""

from abc import abstractmethod
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, Protocol

from stardag._task import BaseTask, TaskStruct, _has_custom_run, _has_custom_run_aio
from stardag.build.registry._base import RegistryABC


class BuildExitStatus(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"


@dataclass
class TaskCount:
    discovered: int
    previously_completed: int
    succeeded: int
    failed: int
    pending: int


@dataclass
class BuildSummary:
    status: BuildExitStatus
    task_count: TaskCount


class FailMode(StrEnum):
    # exit at first failure
    FAIL_FAST = "fail_fast"
    # continue executing all tasks which dependencies are met
    CONTINUE = "continue"


def build_sequential(
    tasks: list[BaseTask],
    registry: RegistryABC | None = None,
    dual_run_default: Literal["sync", "async"] = "sync",
) -> BuildSummary:
    """Sync API for building tasks sequentially.

    This is intended primarily for debugging and testing.

    Task execution policy:
    - Sync-only tasks: run via `run()`
    - Async-only tasks: run via `asyncio.run(run_aio())`. (Does not work if called from
        within an existing event loop.)
    - Dual tasks: run via `run()` if `dual_run_default=="sync"` (default), else
        (`dual_run_default=="async"`) via `asyncio.run(run_aio())`.
    """
    ...


async def build_sequential_aio(
    tasks: list[BaseTask],
    registry: RegistryABC | None = None,
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
    """
    ...


class TaskRunnerABC:
    """Recieves tasks and executes them according to some policy. Including the
    potential transfer of execution to other processes or infrastructure.
    """

    @abstractmethod
    async def submit(self, task: BaseTask) -> None | TaskStruct | Exception:
        """Submit a task for execution.

        Args:
            task: The task to execute.

        Returns:
            - None: Task completed successfully with no dynamic dependencies.
            - TaskStruct: Task "suspended" (execution stopped) since it has dynamic
                dependencies. The returned TaskStruct contains the discovered "next"
                dynamic dependencies in the generator from the run() or run_aio() method.
            - Exception: Task failed with the given exception.
        """
        ...

    @abstractmethod
    async def setup(self) -> None:
        """Setup any resources needed for the task runner."""
        ...

    @abstractmethod
    async def teardown(self) -> None:
        """Teardown any resources used by the task runner."""
        ...


class ExecutionMode(StrEnum):
    SYNC_BLOCKING = "sync_blocking"
    SYNC_THREAD = "sync_thread"
    SYNC_PROCESS = "sync_process"
    ASYNC_MAIN_LOOP = "async_main_loop"
    # Let's explore this:
    ASYNC_AIOMULTIPROCESS = "async_aiomultiprocess"
    # NOTE: we will implement a custom logic to transfer execution to modal apps.
    # (But good to consider this, prepare for reuse of components).


class ExecutionModeSelector(Protocol):
    """Generic protocol for selecting execution mode for a given task.

    This can be used to customize how tasks are executed based on arbitrary criteria.

    One can consider allow the tasks to specify their preferred execution mode, e.g.,
    via class attributes or methods.
    """

    def __call__(self, task: BaseTask) -> ExecutionMode: ...


class DefaultExecutionModeSelector:
    """Selects execution mode based on the task's implemented run methods."""

    def __init__(
        self,
        sync_run_default: Literal["thread", "blocking", "process"] = "thread",
    ) -> None:
        self.sync_run_default = sync_run_default

    def __call__(self, task: BaseTask) -> ExecutionMode:
        if _has_custom_run(task) and _has_custom_run_aio(task):
            # Dual task
            return ExecutionMode.ASYNC_MAIN_LOOP
        elif _has_custom_run_aio(task):
            # Async-only task
            return ExecutionMode.ASYNC_MAIN_LOOP
        elif _has_custom_run(task):
            # Sync-only task
            if self.sync_run_default == "thread":
                return ExecutionMode.SYNC_THREAD
            elif self.sync_run_default == "process":
                return ExecutionMode.SYNC_PROCESS
            else:
                return ExecutionMode.SYNC_BLOCKING
        else:
            raise ValueError(f"Task {task} has no run method.")


class DefaultTaskRunner(TaskRunnerABC):
    """Has the ability to run task execution in a pool of async workers, a pool of
    thread workers, and a pool of processes.

    The execution mode for each task is selected via the provided
    `execution_mode_selector`.

    NOTE: the task runner is only responsible for managing the execution of individual
    tasks (+ managing the lifecycle of the worker pools) NOT to resolve dependencies,
    which is handled by the "build" function.

    Args:
        execution_mode_selector: A callable that selects the execution mode for a
            given task.
        max_async_workers: Maximum number of concurrent async tasks.
        max_thread_workers: Maximum number of concurrent thread tasks.
        max_process_workers: Maximum number of concurrent process tasks.
    """

    def __init__(
        self,
        execution_mode_selector: ExecutionModeSelector = DefaultExecutionModeSelector(),
        max_async_workers: int = 10,
        max_thread_workers: int = 10,
        max_process_workers: int | None = None,
    ) -> None:
        self.execution_mode_selector = execution_mode_selector
        # NOTE Below is a draft, consider using libraries like aiomultiprocess for
        # async process pools!
        self.max_async_workers = max_async_workers
        self.max_thread_workers = max_thread_workers
        self.max_process_workers = max_process_workers

        # TODO Initialize (async, thread and process pools) here.

    async def submit(self, task: BaseTask) -> None | TaskStruct | Exception:
        """"""
        ...  # TODO!

    async def setup(self) -> None:
        """Setup any resources needed for the task runner."""
        ...  # TODO!

    async def teardown(self) -> None:
        """Teardown any resources used by the task runner."""
        ...  # TODO!


async def build(
    tasks: list[BaseTask],
    task_runner: TaskRunnerABC,
    fail_mode: FailMode,
) -> BuildSummary:
    """Build tasks using the provided task runner and fail mode.

    This function is responsible for orchestrating the execution of tasks,
    resolving dependencies, inclding dynamic dependencies (returned from
    `task_runner.submit()`), and handling failures according to the specified fail mode.
    """
    ...  # TODO!
