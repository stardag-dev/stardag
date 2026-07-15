"""Base registry classes and utilities."""

import abc
import inspect
import os
import subprocess
from datetime import datetime
from functools import lru_cache
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any
from uuid import UUID

from stardag.base_model import StardagBaseModel
from stardag.utils.resource_provider import resource_provider

if TYPE_CHECKING:
    from stardag import BaseTask
    from stardag.artifact import Artifact


def _compute_param_info(fn: Any) -> tuple[frozenset[str], bool] | None:
    """(parameter names, accepts **kwargs) of ``fn``, or None if uninspectable."""
    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError):
        return None
    params = signature.parameters
    has_var_keyword = any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
    )
    return frozenset(params), has_var_keyword


@lru_cache(maxsize=256)
def _cached_param_info(fn: Any) -> tuple[frozenset[str], bool] | None:
    return _compute_param_info(fn)


def _param_info(fn: Any) -> tuple[frozenset[str], bool] | None:
    """Cached signature reflection for the accepts_* helpers.

    Bound methods hash by (instance, function), so repeated lookups on the
    same registry hit the cache. Unhashable callables fall back to direct
    computation.
    """
    try:
        return _cached_param_info(fn)
    except TypeError:
        return _compute_param_info(fn)


def accepts_executor_kwargs(fn: Any) -> bool:
    """Whether a ``task_start[_aio]`` implementation accepts the
    ``executor``/``executor_ref`` kwargs.

    Signature inspection (instead of a try/except TypeError fallback, which
    would also mask unrelated TypeErrors raised *inside* an implementation)
    to stay compatible with custom :class:`RegistryABC` implementations
    written against the pre-detached ``(build_id, task)`` signature.
    """
    info = _param_info(fn)
    if info is None:
        return False
    params, has_var_keyword = info
    return has_var_keyword or ("executor" in params and "executor_ref" in params)


def accepts_executor_metadata_kwarg(fn: Any) -> bool:
    """Whether an implementation accepts the ``executor_metadata`` kwarg.

    Same signature-inspection rationale as :func:`accepts_executor_kwargs`:
    custom implementations written before the kwarg existed must keep
    working — callers drop the metadata for those instead of raising.
    """
    info = _param_info(fn)
    if info is None:
        return False
    params, has_var_keyword = info
    return has_var_keyword or "executor_metadata" in params


class FrontierTaskRef(StardagBaseModel):
    """A task in a build's scheduling frontier (see :class:`BuildFrontier`)."""

    task_id: str
    latest_status: str
    latest_executor: str | None = None
    latest_executor_ref: str | None = None
    # Executor-descriptive metadata recorded with the latest start (e.g.
    # Modal app/workspace/environment). None on servers predating the field.
    latest_executor_metadata: dict[str, Any] | None = None
    # When the current status was recorded (None on servers predating the
    # field) — used for staleness bounds on RUNNING-without-ref tasks.
    latest_status_at: datetime | None = None


class BuildFrontier(StardagBaseModel):
    """Scheduling state of a build, consumed by reactive scheduler ticks.

    ``actionable``: tasks with global status pending/suspended/running whose
    upstream dependencies (static + dynamic) are all completed. The
    scheduler partitions them: pending/suspended → spawn; running → probe
    the detached execution ref. ``status_counts`` covers all tasks in the
    build (terminal detection).
    """

    build_id: UUID
    build_status: str
    needs_tick: bool
    root_task_ids: list[str]
    roots: list[FrontierTaskRef]
    status_counts: dict[str, int]
    actionable: list[FrontierTaskRef]
    # All RUNNING tasks in the build, including non-actionable ones (e.g.
    # inside the dynamic-dep registration window) — cancellation targets.
    # Defaults to empty for servers predating the field.
    running: list[FrontierTaskRef] = []
    # Reactive-scheduling marker/owner, moved off the target root into the
    # registry. None means the build is NOT reactively scheduled (a stray
    # tick must no-op on it, so a resident-orchestrator build is never
    # double-scheduled). Non-None is the owning app that drives the tick
    # (ownership guard). Set via ``build_set_reactive_meta``. None also on
    # servers predating the field (the reactive trigger fails loudly against
    # such servers when it PUTs the reactive-meta endpoint, so a tick never
    # observes this).
    reactive_app_name: str | None = None
    # Reactive-scheduler tick configuration (a ``TickConfig`` kwargs dict);
    # None/absent is treated as ``{}``. Read from the frontier only for the
    # backstop marker check — the Modal tick reads it from the lighter
    # ``build_get`` before acquiring the lease.
    reactive_tick_kwargs: dict[str, Any] | None = None


class BuildInfo(StardagBaseModel):
    """Slim build record from ``build_get`` (``GET /builds/{id}``).

    Carries the reactive marker/owner/config a scheduler tick's pre-lease
    gate needs, without the cost of a full frontier computation. Extra
    fields on the server response are ignored.
    """

    id: UUID
    # Reactive-scheduling marker/owner (see ``BuildFrontier``). None = not
    # reactively scheduled.
    reactive_app_name: str | None = None
    # Reactive-scheduler tick configuration; None/absent treated as ``{}``.
    reactive_tick_kwargs: dict[str, Any] | None = None


class StartClaimResult(StardagBaseModel):
    """Outcome of a claiming task start (see ``task_start_claim_aio``).

    ``started=True`` means this caller won: the TASK_STARTED event was
    recorded (and any requested concurrency-limit slots acquired). On a
    denial, ``denied_reason`` says why and — for ``already_running`` — the
    running execution's ``(executor, executor_ref)`` is echoed so the
    caller can re-attach or probe liveness.
    """

    started: bool
    denied_reason: str | None = None  # already_running|already_completed|limit
    executor: str | None = None
    executor_ref: str | None = None
    denied_keys: list[str] = []


class RegisteredTaskInfo(StardagBaseModel):
    """Slim per-task info echoed back from a bulk task registration.

    Carries the task's current *global* execution state (across builds) so
    the build engine learns — with zero extra roundtrips — whether a task is
    already RUNNING with a re-attachable detached execution. The executor
    fields are only meaningful when ``latest_status == "running"``.
    """

    task_id: str
    latest_status: str | None = None
    latest_executor: str | None = None
    latest_executor_ref: str | None = None
    latest_executor_metadata: dict[str, Any] | None = None


class TaskMetadata(StardagBaseModel):
    """Metadata for a registered task in the registry."""

    # Core Task fields
    id: UUID
    body: dict[str, Any]
    name: str
    namespace: str
    version: str
    output_uri: str | None  # only if the task has a FileSystemTarget output
    # Registry Metadata fields
    status: str
    registered_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    error_message: str | None


class RegistryABC(metaclass=abc.ABCMeta):
    """Abstract base class for task registries.

    A registry tracks task execution within builds. Implementations must
    provide at least the `task_register` method. All other methods have default
    no-op implementations for backwards compatibility.

    The registry is stateless with respect to build_id - the build_id is passed
    explicitly to all methods that need it. This allows a single registry instance
    to be reused across multiple builds.

    Method naming convention:
    - Build methods: build_<action> (e.g., build_start, build_complete)
    - Task methods: task_<action> (e.g., task_register, task_start)
    - Async versions: <method>_aio suffix (e.g., build_start_aio, task_register_aio)
    """

    # -------------------------------------------------------------------------
    # Build lifecycle methods
    # -------------------------------------------------------------------------

    def build_start(
        self,
        root_tasks: list["BaseTask"] | None = None,
        description: str | None = None,
        executor_metadata: dict[str, Any] | None = None,
    ) -> UUID:
        """Start a new build session.

        Called at the beginning of a build. Returns a build ID.

        Args:
            root_tasks: The root tasks being built
            description: Optional description of the build
            executor_metadata: Optional metadata describing where/how the
                build is executed (e.g. the Modal app/workspace/environment
                for a triggered build). Backends that don't track it may
                ignore it.

        Returns:
            Build ID (UUID) for the new build session.
        """
        return UUID("00000000-0000-0000-0000-000000000000")

    def build_resume(
        self,
        build_id: UUID,
        executor_metadata: dict[str, Any] | None = None,
    ) -> None:
        """Mark an existing build as resumed.

        Called when ``sd.build(resume_build_id=...)`` reuses an existing
        build (potentially in a terminal state) instead of starting a new
        one. The registry should record a BUILD_RESUMED event so the
        build flips back to RUNNING and the UI can surface a
        "running (resumed)" affordance.

        Default implementation is a no-op so older registry backends
        keep working unchanged.

        Args:
            build_id: The build UUID being resumed.
            executor_metadata: Optional metadata describing where/how the
                resumed build is executed (see :meth:`build_start`).
        """
        pass

    def build_complete(self, build_id: UUID) -> None:
        """Mark a build as completed successfully.

        Args:
            build_id: The build UUID returned by build_start.
        """
        pass

    def build_fail(self, build_id: UUID, error_message: str | None = None) -> None:
        """Mark a build as failed.

        Args:
            build_id: The build UUID returned by build_start.
            error_message: Optional error message describing the failure.
        """
        pass

    def build_cancel(self, build_id: UUID) -> None:
        """Cancel a build.

        Called when a build is explicitly cancelled by the user.

        Args:
            build_id: The build UUID returned by build_start.
        """
        pass

    def build_exit_early(self, build_id: UUID, reason: str | None = None) -> None:
        """Mark a build as exited early.

        Called when all remaining tasks are running in other builds
        and this build should stop waiting.

        Args:
            build_id: The build UUID returned by build_start.
            reason: Optional reason for exiting early.
        """
        pass

    # -------------------------------------------------------------------------
    # Task lifecycle methods
    # -------------------------------------------------------------------------

    @abc.abstractmethod
    def task_register(self, build_id: UUID, task: "BaseTask") -> None:
        """Register a task as pending/scheduled.

        This is called when a task is about to be executed.

        Args:
            build_id: The build UUID returned by build_start.
            task: The task to register.
        """
        pass

    def task_register_bulk(
        self, build_id: UUID, tasks: Sequence["BaseTask"]
    ) -> list[RegisteredTaskInfo] | None:
        """Register many tasks to a build in a single call.

        Default implementation falls back to ``task_register`` per task —
        backends that can batch (e.g. the API registry's bulk endpoint)
        should override this to make one HTTP call instead of N.

        Order of ``tasks`` is significant: the SDK's post-order discover
        walk emits deps before parents so that ``dependency_task_ids``
        lookups inside the registry resolve to existing rows (no phantom
        creation). Backends that process the batch as one transaction
        should preserve array order.

        Args:
            build_id: The build UUID returned by build_start.
            tasks: Tasks to register, in registration order.

        Returns:
            Per-task :class:`RegisteredTaskInfo` (used by the build engine
            to re-attach to detached executions that are still running), or
            None when the backend doesn't provide it.
        """
        for task in tasks:
            self.task_register(build_id, task)
        return None

    def build_list_running(self, limit: int = 100) -> list[UUID]:
        """List ids of builds currently in RUNNING status (most recent first).

        Used by the reactive scheduler watchdog to sweep for builds that
        may need a tick. Default: empty (no reactive-scheduling support).
        """
        return []

    async def build_list_running_aio(self, limit: int = 100) -> list[UUID]:
        """Async version of build_list_running."""
        return self.build_list_running(limit)

    def build_add_roots(self, build_id: UUID, root_task_ids: list[str]) -> None:
        """Append root task ids to a build (reactive re-trigger with new roots).

        Default: no-op.
        """
        pass

    async def build_add_roots_aio(
        self, build_id: UUID, root_task_ids: list[str]
    ) -> None:
        """Async version of build_add_roots."""
        self.build_add_roots(build_id, root_task_ids)

    def task_retry(self, build_id: UUID, task: "BaseTask") -> None:
        """Reset a failed/cancelled/skipped task to pending (retry).

        Backends flip only terminal-but-retryable statuses; completed and
        running tasks are unaffected. Default: no-op.
        """
        pass

    async def task_retry_aio(self, build_id: UUID, task: "BaseTask") -> None:
        """Async version of task_retry."""
        self.task_retry(build_id, task)

    def build_skip_blocked(self, build_id: UUID) -> list[str]:
        """Mark tasks transitively blocked by failures as skipped.

        Returns the skipped task ids. Default: no-op (empty).
        """
        return []

    async def build_skip_blocked_aio(self, build_id: UUID) -> list[str]:
        """Async version of build_skip_blocked."""
        return self.build_skip_blocked(build_id)

    def build_notify(self, build_id: UUID) -> None:
        """Set the build's scheduler wake-up flag (reactive scheduling).

        Default: no-op (backends without reactive-scheduling support).
        """
        pass

    async def build_notify_aio(self, build_id: UUID) -> None:
        """Async version of build_notify."""
        self.build_notify(build_id)

    def build_clear_notify(self, build_id: UUID) -> None:
        """Clear the build's scheduler wake-up flag. Default: no-op."""
        pass

    async def build_clear_notify_aio(self, build_id: UUID) -> None:
        """Async version of build_clear_notify."""
        self.build_clear_notify(build_id)

    def build_get_frontier(self, build_id: UUID) -> BuildFrontier:
        """Return the build's scheduling frontier (reactive scheduling).

        Default: not supported — reactive scheduling requires a registry
        backend that can compute the frontier (e.g. the API registry).
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not support reactive scheduling "
            "(build_get_frontier)"
        )

    async def build_get_frontier_aio(self, build_id: UUID) -> BuildFrontier:
        """Async version of build_get_frontier."""
        return self.build_get_frontier(build_id)

    def build_get(self, build_id: UUID) -> BuildInfo:
        """Return a slim build record (``GET /builds/{id}``).

        Lighter than ``build_get_frontier`` (no frontier computation): used
        by the reactive tick's pre-lease marker/ownership gate, which only
        needs ``reactive_app_name``/``reactive_tick_kwargs``. Default: not
        supported.
        """
        raise NotImplementedError(f"{type(self).__name__} does not support build_get")

    async def build_get_aio(self, build_id: UUID) -> BuildInfo:
        """Async version of build_get."""
        return self.build_get(build_id)

    def build_set_reactive_meta(
        self,
        build_id: UUID,
        *,
        app_name: str,
        tick_kwargs: dict[str, Any] | None = None,
    ) -> None:
        """Mark a build reactively scheduled and store its tick config.

        Upsert (idempotent). ``app_name`` (the marker/owner) is always set
        and surfaces as ``reactive_app_name`` on the build/frontier. When
        ``tick_kwargs`` is None (a bare re-trigger) the stored config is left
        untouched — so a re-trigger with no explicit tick_kwargs preserves
        the existing ones; passing tick_kwargs updates them. Default: no-op
        (backends without reactive support).
        """
        pass

    async def build_set_reactive_meta_aio(
        self,
        build_id: UUID,
        *,
        app_name: str,
        tick_kwargs: dict[str, Any] | None = None,
    ) -> None:
        """Async version of build_set_reactive_meta."""
        self.build_set_reactive_meta(
            build_id, app_name=app_name, tick_kwargs=tick_kwargs
        )

    def task_start(
        self,
        build_id: UUID,
        task: "BaseTask",
        executor: str | None = None,
        executor_ref: str | None = None,
        executor_metadata: dict[str, Any] | None = None,
    ) -> None:
        """Mark a task as started/running.

        Called immediately before a task begins execution. The caller is
        responsible for having already registered the task in the build —
        ``task_start`` only emits the started event.

        ``executor`` / ``executor_ref`` identify a detached execution (e.g.
        executor="modal" with a Modal function call id) so a later resumed
        build can re-attach instead of re-executing.
        ``executor_metadata`` optionally describes the execution backend in
        more detail (e.g. Modal app/workspace/environment/function) for
        surfacing in the UI. Backends that don't track them may ignore all
        three.

        Args:
            build_id: The build UUID returned by build_start.
            task: The task that is starting.
        """
        pass

    def task_complete(self, build_id: UUID, task: "BaseTask") -> None:
        """Mark a task as completed successfully.

        Called after a task finishes execution without errors.

        Args:
            build_id: The build UUID returned by build_start.
            task: The task that completed.
        """
        pass

    def task_fail(
        self, build_id: UUID, task: "BaseTask", error_message: str | None = None
    ) -> None:
        """Mark a task as failed.

        Called when a task raises an exception during execution.

        Args:
            build_id: The build UUID returned by build_start.
            task: The task that failed.
            error_message: Optional error message describing the failure.
        """
        pass

    def task_suspend(self, build_id: UUID, task: "BaseTask") -> None:
        """Mark a task as suspended waiting for dynamic dependencies.

        Called when a task yields dynamic deps that are not yet complete.
        The task will remain suspended until its dynamic deps are built.

        Args:
            build_id: The build UUID returned by build_start.
            task: The task that is suspended.
        """
        pass

    def task_add_dependencies(
        self,
        build_id: UUID,
        task: "BaseTask",
        upstream_tasks: Sequence["BaseTask"],
        is_dynamic: bool = True,
    ) -> None:
        """Record dependency edges for a task.

        Called by the build system when a task yields dynamic deps — the
        edges aren't known at ``task_register`` time (static ``requires()``
        chain only), so this is how they reach the registry so that the
        full DAG renders correctly in the UI.

        Registries that can't write to a graph (the in-memory cases) may
        treat this as a no-op. HTTP-backed implementations should tolerate
        404 from older API versions that don't support the endpoint.

        Args:
            build_id: The build UUID returned by build_start.
            task: The downstream task whose deps are being added.
            upstream_tasks: The yielded deps to record as edges.
            is_dynamic: Marks the edges as dynamic (True by default —
                static ``requires()`` are recorded during task_register).
        """
        pass

    def task_resume(self, build_id: UUID, task: "BaseTask") -> None:
        """Mark a task as resumed after dynamic dependencies completed.

        Called when a task's dynamic dependencies are complete and
        the task is ready to continue execution (either by resuming
        a suspended generator or by re-executing the task).

        Args:
            build_id: The build UUID returned by build_start.
            task: The task that is resuming.
        """
        pass

    def task_cancel(self, build_id: UUID, task: "BaseTask") -> None:
        """Cancel a task.

        Called when a task is cancelled — by the user, or by the build
        engine when terminating in-flight siblings on a fail-fast failure.

        Args:
            build_id: The build UUID returned by build_start.
            task: The task to cancel.
        """
        pass

    def task_skip(self, build_id: UUID, task: "BaseTask") -> None:
        """Mark a task as skipped.

        Called when a task will not run because a dependency failed or
        was cancelled. Distinct from ``task_cancel``: skipped tasks
        never started executing.

        Args:
            build_id: The build UUID returned by build_start.
            task: The task to skip.
        """
        pass

    def task_waiting_for_lock(
        self, build_id: UUID, task: "BaseTask", lock_owner: str | None = None
    ) -> None:
        """Record that a task is waiting for a global lock.

        Called when a task cannot acquire its lock because another
        build is holding it.

        Args:
            build_id: The build UUID returned by build_start.
            task: The task waiting for the lock.
            lock_owner: Optional identifier of who holds the lock.
        """
        pass

    def task_upload_artifacts(
        self, build_id: UUID, task: "BaseTask", artifacts: Sequence["Artifact"]
    ) -> None:
        """Upload artifacts for a completed task.

        Called after a task completes successfully if it has artifacts.

        Args:
            build_id: The build UUID returned by build_start.
            task: The completed task.
            artifacts: List of artifacts to upload.
        """
        pass

    @abc.abstractmethod
    def task_get_metadata(self, task_id: UUID) -> TaskMetadata:
        """Get metadata for a registered task.

        Args:
            task_id: The ID of the task to get metadata for.
        Returns:
            A TaskMetadata object containing task metadata.
        """
        pass

    # -------------------------------------------------------------------------
    # Async versions - default implementations delegate to sync methods
    # -------------------------------------------------------------------------

    async def build_start_aio(
        self,
        root_tasks: list["BaseTask"] | None = None,
        description: str | None = None,
        executor_metadata: dict[str, Any] | None = None,
    ) -> UUID:
        """Async version of build_start.

        Drops ``executor_metadata`` for sync overrides written against the
        pre-metadata signature (detected via signature inspection).
        """
        if executor_metadata is not None and accepts_executor_metadata_kwarg(
            self.build_start
        ):
            return self.build_start(
                root_tasks, description, executor_metadata=executor_metadata
            )
        return self.build_start(root_tasks, description)

    async def build_resume_aio(
        self,
        build_id: UUID,
        executor_metadata: dict[str, Any] | None = None,
    ) -> None:
        """Async version of build_resume.

        Drops ``executor_metadata`` for sync overrides written against the
        pre-metadata signature (detected via signature inspection).
        """
        if executor_metadata is not None and accepts_executor_metadata_kwarg(
            self.build_resume
        ):
            self.build_resume(build_id, executor_metadata=executor_metadata)
            return
        self.build_resume(build_id)

    async def build_complete_aio(self, build_id: UUID) -> None:
        """Async version of build_complete."""
        self.build_complete(build_id)

    async def build_fail_aio(
        self, build_id: UUID, error_message: str | None = None
    ) -> None:
        """Async version of build_fail."""
        self.build_fail(build_id, error_message)

    async def build_cancel_aio(self, build_id: UUID) -> None:
        """Async version of build_cancel."""
        self.build_cancel(build_id)

    async def build_exit_early_aio(
        self, build_id: UUID, reason: str | None = None
    ) -> None:
        """Async version of build_exit_early."""
        self.build_exit_early(build_id, reason)

    async def task_register_aio(self, build_id: UUID, task: "BaseTask") -> None:
        """Async version of task_register."""
        self.task_register(build_id, task)

    async def task_register_bulk_aio(
        self, build_id: UUID, tasks: Sequence["BaseTask"]
    ) -> list[RegisteredTaskInfo] | None:
        """Async version of task_register_bulk.

        Default implementation falls back to ``task_register_aio`` per
        task. Override for backends that can batch (the API registry
        does so with the ``/tasks/bulk`` endpoint).
        """
        for task in tasks:
            await self.task_register_aio(build_id, task)
        return None

    async def task_start_claim_aio(
        self,
        build_id: UUID,
        task: "BaseTask",
        executor: str | None = None,
        executor_ref: str | None = None,
        executor_metadata: dict[str, Any] | None = None,
        limit_keys: Sequence[str] | None = None,
    ) -> StartClaimResult:
        """Mark a task started under an atomic per-task execution claim.

        The claim guarantees at most one concurrent RUNNING execution per
        task (environment-wide, across builds): a start racing an existing
        RUNNING task is denied — with the running execution's ref echoed —
        instead of recorded. COMPLETED tasks deny with
        ``already_completed`` (callers treat this like the lock's
        ALREADY_COMPLETED: verify the target with eventual-consistency
        retries). ``limit_keys`` compose atomically (a denied claim
        consumes no slots).

        **This is the extension seam for custom arbitration backends**: a
        custom ``RegistryABC`` implementation can arbitrate however it
        likes (Redis, DynamoDB, ...), keeping claim, status and completion
        consistent in one backend. The default implementation performs NO
        arbitration — it records the start via :meth:`task_start_aio` and
        reports it as won, preserving pre-claim behavior for backends
        without support.
        """
        await self.task_start_aio(
            build_id, task, executor=executor, executor_ref=executor_ref
        )
        return StartClaimResult(started=True)

    async def task_start_with_limits_aio(
        self,
        build_id: UUID,
        task: "BaseTask",
        executor: str | None = None,
        executor_ref: str | None = None,
        executor_metadata: dict[str, Any] | None = None,
        limit_keys: Sequence[str] | None = None,
    ) -> bool:
        """Mark a task started under named concurrency limits (atomic acquire).

        Returns False when a limit key is at capacity — the task was NOT
        started and no event was recorded; the caller should retry later
        (in reactive scheduling: leave the task in the frontier; a
        slot-holder's completion wakes the scheduler).

        Default implementation performs no limit enforcement: it delegates
        to :meth:`task_start_aio` and returns True. Backends with
        server-side limit support (the API registry) override this.
        """
        await self.task_start_aio(
            build_id,
            task,
            executor=executor,
            executor_ref=executor_ref,
            executor_metadata=executor_metadata,
        )
        return True

    async def task_start_aio(
        self,
        build_id: UUID,
        task: "BaseTask",
        executor: str | None = None,
        executor_ref: str | None = None,
        executor_metadata: dict[str, Any] | None = None,
    ) -> None:
        """Async version of task_start.

        Tolerates subclasses that override the sync ``task_start`` with the
        pre-detached ``(build_id, task)`` signature: refs are dropped for
        those rather than raising (detected via signature inspection, so
        TypeErrors raised *inside* an implementation propagate normally).
        ``executor_metadata`` is likewise dropped for overrides that predate
        the kwarg.
        """
        if (
            executor is None and executor_ref is None and executor_metadata is None
        ) or not accepts_executor_kwargs(self.task_start):
            self.task_start(build_id, task)
            return
        if executor_metadata is not None and accepts_executor_metadata_kwarg(
            self.task_start
        ):
            self.task_start(
                build_id,
                task,
                executor=executor,
                executor_ref=executor_ref,
                executor_metadata=executor_metadata,
            )
            return
        self.task_start(build_id, task, executor=executor, executor_ref=executor_ref)

    async def task_complete_aio(self, build_id: UUID, task: "BaseTask") -> None:
        """Async version of task_complete."""
        self.task_complete(build_id, task)

    async def task_fail_aio(
        self, build_id: UUID, task: "BaseTask", error_message: str | None = None
    ) -> None:
        """Async version of task_fail."""
        self.task_fail(build_id, task, error_message)

    async def task_suspend_aio(self, build_id: UUID, task: "BaseTask") -> None:
        """Async version of task_suspend."""
        self.task_suspend(build_id, task)

    async def task_add_dependencies_aio(
        self,
        build_id: UUID,
        task: "BaseTask",
        upstream_tasks: Sequence["BaseTask"],
        is_dynamic: bool = True,
    ) -> None:
        """Async version of task_add_dependencies."""
        self.task_add_dependencies(build_id, task, upstream_tasks, is_dynamic)

    async def task_resume_aio(self, build_id: UUID, task: "BaseTask") -> None:
        """Async version of task_resume."""
        self.task_resume(build_id, task)

    async def task_cancel_aio(self, build_id: UUID, task: "BaseTask") -> None:
        """Async version of task_cancel."""
        self.task_cancel(build_id, task)

    async def task_skip_aio(self, build_id: UUID, task: "BaseTask") -> None:
        """Async version of task_skip."""
        self.task_skip(build_id, task)

    async def task_waiting_for_lock_aio(
        self, build_id: UUID, task: "BaseTask", lock_owner: str | None = None
    ) -> None:
        """Async version of task_waiting_for_lock."""
        self.task_waiting_for_lock(build_id, task, lock_owner)

    async def task_upload_artifacts_aio(
        self, build_id: UUID, task: "BaseTask", artifacts: Sequence["Artifact"]
    ) -> None:
        """Async version of task_upload_artifacts."""
        self.task_upload_artifacts(build_id, task, artifacts)

    async def task_get_metadata_aio(self, task_id: UUID) -> TaskMetadata:
        """Async version of task_get_metadata."""
        return self.task_get_metadata(task_id)


class NoOpRegistry(RegistryABC):
    """A registry that does nothing.

    Used as a default when no registry is configured.
    """

    def build_start(
        self,
        root_tasks: list["BaseTask"] | None = None,
        description: str | None = None,
        executor_metadata: dict[str, Any] | None = None,
    ) -> UUID:
        """Return a placeholder build ID."""
        return UUID("00000000-0000-0000-0000-000000000000")

    def task_register(self, build_id: UUID, task: "BaseTask") -> None:
        pass

    def task_get_metadata(self, task_id: UUID) -> TaskMetadata:
        raise NotImplementedError("NoOpRegistry does not support task_get_metadata.")


def init_registry() -> RegistryABC:
    """Initialize the default registry based on configuration.

    Returns APIRegistry if registry is configured, otherwise NoOpRegistry.
    """
    from stardag.config import config_provider
    from stardag.registry._api_registry import APIRegistry

    config = config_provider.get()

    if config.registry is not None:
        return APIRegistry()

    return NoOpRegistry()


registry_provider = resource_provider(RegistryABC, init_registry)


@lru_cache
def get_git_commit_hash() -> str:
    """Get the short SHA of the current Git commit."""

    supported_env_vars = ["SHORT_SHA", "COMMIT_HASH"]

    for env_var in supported_env_vars:
        short_sha = os.environ.get(env_var)
        if short_sha:
            return short_sha

    try:
        short_sha = (
            subprocess.check_output(
                ["git", "rev-parse", "--short", "HEAD"], stderr=subprocess.DEVNULL
            )
            .strip()
            .decode("utf-8")
        )
        # Check if there are uncommitted changes
        dirty_flag = subprocess.check_output(
            ["git", "status", "--porcelain"], stderr=subprocess.DEVNULL
        ).strip()

        if dirty_flag:
            short_sha += "-dirty"

        return short_sha

    except subprocess.CalledProcessError:
        raise RuntimeError(
            "Unable to get Git commit short SHA, you need to either run in an "
            "environment where git is available or set one of the env vars SHORT_SHA "
            "or COMMIT_HASH."
        )
