"""Base interfaces and data structures for the build system.

This module contains:
- Data structures: BuildExitStatus, TaskCount, BuildSummary, FailMode
- The ambient build context: BuildContext (build, plan, scope)
- Task state tracking: TaskExecutionState
- Task executor protocol: TaskExecutorABC, RoutedTaskExecutor
- Claims: ClaimConfig
"""

from __future__ import annotations

import traceback as tb_module
from abc import ABC, abstractmethod
from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import StrEnum
import logging
from typing import (
    Any,
    Awaitable,
    Callable,
    Generator,
    Generic,
    Literal,
    TypeVar,
)
from uuid import UUID

from stardag import BaseTask, TaskStruct
from stardag.exceptions import APIError

logger = logging.getLogger(__name__)

# Type alias for the on_registry_failure parameter
OnRegistryFailure = Literal["warn", "raise"]


@dataclass(frozen=True)
class BuildContext:
    """The build a driver is running: its id, its plan and its scope.

    Set for the duration of a build (resident engine) or a tick, so an
    executor can forward what a worker needs — the build and plan ids its
    reports name, and the settings it applies — without every signature
    carrying them. ``plan_id`` is None while the build has no plan (no
    registry, or before registration).
    """

    build_id: UUID
    plan_id: UUID | None = None
    deployment_id: UUID | None = None
    settings: Mapping[str, str] = field(default_factory=dict)


current_build_context_var: ContextVar[BuildContext | None] = ContextVar(
    "stardag_current_build_context", default=None
)


def get_current_build_context() -> BuildContext | None:
    """The build context of the enclosing build or tick, if any."""
    return current_build_context_var.get()


def get_current_build_id() -> UUID | None:
    """The build id of the enclosing build or tick, if any."""
    context = current_build_context_var.get()
    return None if context is None else context.build_id


# =============================================================================
# Data Structures
# =============================================================================


class BuildExitStatus(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    EXIT_EARLY = "exit_early"  # All remaining tasks claimed by other builds


@dataclass
class TaskCount:
    discovered: int = 0
    # Tasks found complete during discovery, or completed by another
    # execution while this build waited on its claim. Not executed here.
    previously_completed: int = 0
    succeeded: int = 0
    failed: int = 0
    # In-flight tasks terminated by the build engine (e.g. fail-fast siblings).
    cancelled: int = 0
    # Tasks that never started because a dependency failed or was cancelled.
    skipped: int = 0

    @property
    def pending(self) -> int:
        return (
            self.discovered
            - self.previously_completed
            - self.succeeded
            - self.failed
            - self.cancelled
            - self.skipped
        )


class BuildFailed(Exception):
    """Raised by :meth:`BuildSummary.raise_on_failure` when a build has failed."""

    summary: BuildSummary

    def __init__(self, summary: BuildSummary) -> None:
        self.summary = summary
        super().__init__(str(summary))


@dataclass
class BuildSummary:
    """Summary of a build execution."""

    status: BuildExitStatus
    task_count: TaskCount
    build_id: UUID | None = None
    error: BaseException | None = None

    def raise_on_failure(self) -> None:
        """Raise :class:`BuildFailed` if the build status is ``FAILURE``."""
        if self.status == BuildExitStatus.FAILURE:
            raise BuildFailed(self)

    def __repr__(self) -> str:
        """Return a human-readable summary of the build."""
        tc = self.task_count
        status_icon = "✓" if self.status == BuildExitStatus.SUCCESS else "✗"
        lines = [
            f"Build {self.status.value.upper()} {status_icon}",
        ]
        if self.build_id:
            lines.append(f"  Build ID: {self.build_id}")
        lines.extend(
            [
                f"  Discovered: {tc.discovered}",
                f"  Previously completed: {tc.previously_completed}",
                f"  Succeeded: {tc.succeeded}",
                f"  Failed: {tc.failed}",
            ]
        )
        if tc.cancelled > 0:
            lines.append(f"  Cancelled: {tc.cancelled}")
        if tc.skipped > 0:
            lines.append(f"  Skipped: {tc.skipped}")
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


@dataclass
class TaskExecutionError:
    """Wraps a task exception together with its formatted traceback.

    Returned by TaskExecutorABC.submit() on failure so callers can
    re-raise or log with the original traceback even when the exception
    was caught inside the executor (possibly in another thread/process).
    """

    exception: BaseException
    traceback: str
    """Pre-formatted traceback string (from traceback.format_exception)."""

    def __str__(self) -> str:
        return self.traceback or str(self.exception)


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
    # True while this build waits on another execution's claim on the task
    waiting_on_claim: bool = False
    # The execution this build runs for the task: minted before the claim
    # (so before any executor ref exists) and named by the claim, the start
    # recording the ref, the worker's own reports and every renewal. Kept
    # across an in-process yield (the claim is kept, D11); cleared when the
    # execution ends or the claim was not won.
    execution_id: UUID | None = None

    @property
    def all_deps(self) -> list[BaseTask]:
        return self.static_deps + self.dynamic_deps


# =============================================================================
# Task Executor Protocol
# =============================================================================


@dataclass
class ClaimConfig:
    """How a resident build claims its executions (D11: every execution
    claims, when the build has a registry).

    A claim another execution holds is waited on: the build polls the
    target's completion and re-asks for the claim with backoff, for at most
    ``wait_timeout_seconds`` (``0`` means "claim, but do not wait"). A claim
    that lapsed is taken over by the next claiming start on the server, so
    nothing here needs to judge whether a holder is dead.

    An in-process execution's claim carries ``in_process_ttl_seconds`` and
    is renewed every ``renew_interval_seconds`` while it runs, so a resident
    process that dies stops renewing and its claims lapse like any other
    worker's. A detached execution's TTL comes from its executor's timeout.
    """

    wait_timeout_seconds: float | None = 300.0
    wait_initial_interval_seconds: float = 1.0
    wait_max_interval_seconds: float = 15.0
    wait_backoff_factor: float = 2.0
    in_process_ttl_seconds: int = 120
    renew_interval_seconds: float = 30.0


@dataclass
class DetachedHandle:
    """Handle to a detached (orchestrator-independent) task execution.

    A detached execution keeps running even if the process that spawned it
    dies. The engine records ``(executor, ref)`` with the execution's start
    so an operator (``stardag builds stop``) can reach it, and awaits
    ``wait()`` for the result.

    Attributes:
        executor: Name of the execution backend (e.g. ``"modal"``).
        ref: Backend-specific reference to the execution.
        wait: Zero-arg callable returning an awaitable that resolves to the
            task result, with the same contract as
            :meth:`TaskExecutorABC.submit` (``None`` | ``TaskStruct`` |
            ``TaskExecutionError``).
        executor_metadata: Optional backend-descriptive metadata recorded
            with the start (e.g. the Modal app/workspace/function), for the
            UI. Best-effort — ``None`` when unresolvable.
    """

    executor: str
    ref: str
    wait: Callable[[], Awaitable["None | TaskStruct | TaskExecutionError"]]
    executor_metadata: dict[str, Any] | None = None


class TaskExecutorABC(ABC):
    """Abstract base for task executors.

    Receives tasks and executes them according to some policy. The executor
    is responsible for executing tasks in the appropriate context
    (async/thread/process/remote) and for generator suspension. Dependency
    resolution and the registry are the build engine's.
    """

    @abstractmethod
    async def submit(self, task: BaseTask) -> None | TaskStruct | TaskExecutionError:
        """Execute a task in-process (or block on a remote one).

        Returns:
            - None: completed, no dynamic dependencies.
            - TaskStruct: suspended on dynamic dependencies.
            - TaskExecutionError: failed, with the traceback captured where
                it happened.
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

    async def cancel(self, task: BaseTask) -> None:
        """Best-effort cancel an in-flight task.

        Default: no-op; the engine also cancels the asyncio future wrapping
        ``submit()``, which propagates into cooperative awaitables. Executors
        of detached executions must override this to stop the remote work —
        cancelling ``wait()`` does not.
        """
        pass

    async def get_executor_metadata(self, task: BaseTask) -> dict[str, Any] | None:
        """Descriptive executor metadata for executions of ``task``, without
        starting anything (stamped on the claiming start). Best-effort."""
        return None

    def execution_timeout_seconds(self, task: BaseTask) -> float | None:
        """Wall-clock limit this backend enforces on an execution of
        ``task`` — a *fact* (e.g. Modal's per-function ``timeout``), from
        which a detached execution's claim TTL is derived. None when there
        is none or it cannot be resolved. Must not raise or do I/O."""
        return None

    def reports_lifecycle(self, task: BaseTask) -> bool:
        """Whether the *execution side* reports this task's lifecycle.

        When True the worker executing it reports its own start (with its
        executor ref), completion, failure and yields — naming the execution
        the engine claimed — and the engine reports none of them. Default:
        False, the engine reports everything.
        """
        return False

    def deployment_app_name(self) -> str | None:
        """The deployed app this executor runs tasks on, if any.

        A driver whose tasks run on a deployed app plans under that app's
        current deployment (D13) — its workers yield into the plan, and the
        registry refuses a yield from another deployment. Default: None (a
        pure local executor; the build plans under a local deployment).
        """
        return None

    # -- detached executions -------------------------------------------------

    def supports_detached(self, task: BaseTask) -> bool:
        """Whether this executor can run ``task`` as a detached execution
        (one that survives the orchestrator). Default: False."""
        return False

    async def submit_detached(
        self, task: BaseTask, *, execution_id: UUID
    ) -> DetachedHandle:
        """Start a detached execution of ``task`` and return its handle.

        ``execution_id`` is the execution the engine claimed. An executor
        whose workers report their own lifecycle must forward it into the
        execution: every report names it, and the registry applies a report
        only while that execution holds the task's claim.

        Raises:
            Exception: the execution could not be started (the engine
                records the failure against the execution).
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not support detached execution"
        )

    async def cancel_detached(self, task: BaseTask, executor: str, ref: str) -> None:
        """Best-effort stop of a detached execution by its reference.

        Called only for an execution this engine spawned itself whose start
        the registry then refused — an orphan nothing else can find.
        Default: no-op.
        """
        pass

    def can_spawn_scheduler_ticks(self) -> bool:
        """Whether :meth:`spawn_scheduler_tick` reaches a deployed ``tick``
        (a resident build then drains the registry's wake candidates)."""
        return False

    def spawn_scheduler_tick(self, build_id: UUID, app_name: str) -> None:
        """Spawn a reactive scheduler tick for ``build_id`` on ``app_name``."""
        raise NotImplementedError(f"{type(self).__name__} cannot spawn scheduler ticks")


# Type variable for executor routing keys
ExecutorKeyT = TypeVar("ExecutorKeyT")


class RoutedTaskExecutor(TaskExecutorABC, Generic[ExecutorKeyT]):
    """Task executor that routes tasks to different executors.

    Example:
        local_executor = HybridConcurrentTaskExecutor()
        modal_executor = ModalTaskExecutor(modal_app_name="my-app", ...)

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
        self.executors = executors
        self.router = router

    def _executor_for(self, task: BaseTask) -> TaskExecutorABC | None:
        return self.executors.get(self.router(task))

    async def submit(self, task: BaseTask) -> None | TaskStruct | TaskExecutionError:
        key = self.router(task)
        executor = self.executors.get(key)
        if executor is None:
            exc = KeyError(f"No executor found for routing key: {key}")
            return TaskExecutionError(
                exception=exc,
                traceback="".join(tb_module.format_exception(exc)),
            )
        return await executor.submit(task)

    async def setup(self) -> None:
        for executor in self.executors.values():
            await executor.setup()

    async def teardown(self) -> None:
        for executor in self.executors.values():
            await executor.teardown()

    async def cancel(self, task: BaseTask) -> None:
        executor = self._executor_for(task)
        if executor is not None:
            await executor.cancel(task)

    async def get_executor_metadata(self, task: BaseTask) -> dict[str, Any] | None:
        executor = self._executor_for(task)
        return None if executor is None else await executor.get_executor_metadata(task)

    def execution_timeout_seconds(self, task: BaseTask) -> float | None:
        executor = self._executor_for(task)
        return None if executor is None else executor.execution_timeout_seconds(task)

    def reports_lifecycle(self, task: BaseTask) -> bool:
        executor = self._executor_for(task)
        return False if executor is None else executor.reports_lifecycle(task)

    def deployment_app_name(self) -> str | None:
        """The one deployed app the routed executors run on, if any.

        Raises:
            ValueError: Two routed executors run on different apps — a plan
                has one deployment, so their workers could not both yield
                into it.
        """
        apps = {
            app
            for executor in self.executors.values()
            if (app := executor.deployment_app_name()) is not None
        }
        if len(apps) > 1:
            raise ValueError(
                f"RoutedTaskExecutor routes to several deployed apps "
                f"({', '.join(sorted(apps))}); a build plans under one "
                "deployment, so its tasks must run on one app."
            )
        return next(iter(apps), None)

    def supports_detached(self, task: BaseTask) -> bool:
        executor = self._executor_for(task)
        return False if executor is None else executor.supports_detached(task)

    async def submit_detached(
        self, task: BaseTask, *, execution_id: UUID
    ) -> DetachedHandle:
        key = self.router(task)
        executor = self.executors.get(key)
        if executor is None:
            raise KeyError(f"No executor found for routing key: {key}")
        return await executor.submit_detached(task, execution_id=execution_id)

    async def cancel_detached(self, task: BaseTask, executor: str, ref: str) -> None:
        routed = self._executor_for(task)
        if routed is not None:
            await routed.cancel_detached(task, executor, ref)

    def can_spawn_scheduler_ticks(self) -> bool:
        return any(e.can_spawn_scheduler_ticks() for e in self.executors.values())

    def spawn_scheduler_tick(self, build_id: UUID, app_name: str) -> None:
        for executor in self.executors.values():
            if executor.can_spawn_scheduler_ticks():
                executor.spawn_scheduler_tick(build_id, app_name)
                return
        raise NotImplementedError("no routed executor can spawn scheduler ticks")


# =============================================================================
# Registry Error Handling
# =============================================================================


def is_refusal(error: BaseException) -> bool:
    """Whether ``error`` is the registry saying *no* (a 4xx it understood:
    a conflict, an invalid request), as opposed to an outage."""
    return (
        isinstance(error, APIError)
        and error.status_code is not None
        and 400 <= error.status_code < 500
        and error.status_code not in (401, 403, 404, 408, 429)
    )


def handle_registry_error(
    error: Exception,
    message: str,
    on_registry_failure: OnRegistryFailure,
) -> None:
    """Handle a registry call failure based on the configured mode.

    ``"warn"`` tolerates an *outage* — the registry was unreachable or
    errored, and the caller chose to carry on without the record. It never
    tolerates a *refusal* (:func:`is_refusal`): the registry understood the
    call and said no, so carrying on would run the build under a different
    rule than the one it asked for.
    """
    if on_registry_failure == "raise" or is_refusal(error):
        raise error.with_traceback(error.__traceback__)
    logger.warning(f"{message}: {error}")
