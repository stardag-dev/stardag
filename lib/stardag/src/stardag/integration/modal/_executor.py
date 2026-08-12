"""The orchestrator side: a ``TaskExecutorABC`` that runs tasks on Modal.

Used by whatever is driving a build — a resident ``build`` function
(:class:`stardag.integration.modal.Builder`) or a reactive scheduler tick —
to spawn tasks onto the app's deployed ``worker_*`` functions, re-attach to
still-running ones after a restart, and cancel them.
"""

from __future__ import annotations

import asyncio
import logging
import traceback as tb_module
import typing
from uuid import UUID

import modal

from stardag import BaseTask, TaskStruct
from stardag.build import (
    DetachedExecutionStatus,
    DetachedHandle,
    TaskExecutionError,
    TaskExecutorABC,
    get_current_build_id,
)
from stardag.build._reactive import claim_ttl_seconds
from stardag.integration.modal._metadata import (
    MODAL_EXECUTOR_NAME,
    STARDAG_BUILD_ID_ENV,
    STARDAG_CLAIM_TTL_SECONDS_ENV,
    STARDAG_MODAL_APP_ID_ENV,
    STARDAG_MODAL_APP_NAME_ENV,
    STARDAG_MODAL_ENVIRONMENT_ENV,
    STARDAG_MODAL_FUNCTION_ID_ENV,
    STARDAG_MODAL_FUNCTION_NAME_ENV,
    STARDAG_MODAL_FUNCTION_TIMEOUT_ENV,
    STARDAG_MODAL_WORKSPACE_ENV,
    STARDAG_REACTIVE_ENV,
    _get_modal_app_id_aio,
    _get_modal_environment,
    _get_modal_function_id_aio,
    _get_modal_workspace_aio,
)
from stardag.integration.modal._selector import (
    WorkerSelector,
    _normalize_worker_selection,
)

logger = logging.getLogger(__name__)


def _is_transient_modal_error(exception: BaseException) -> bool:
    """Best-effort classification of transport-level (retryable) errors.

    Anything raised by ``FunctionCall.get`` that reflects the CALL's outcome
    (``modal.exception.RemoteError``, user exceptions re-raised from the
    function, expired results) is terminal; connection/socket/gRPC-transport
    failures are not — they say nothing about the call.
    """
    if isinstance(exception, (OSError, ConnectionError)):
        return True
    module = type(exception).__module__ or ""
    if module.startswith("grpclib") or module.startswith("h2"):
        return True
    return type(exception).__name__ in ("ClientClosed", "StreamTerminatedError")


class ModalTaskExecutor(TaskExecutorABC):
    """Task executor that sends tasks to Modal for remote execution.

    This executor submits tasks to Modal worker functions. Use with
    RoutedTaskExecutor to route some tasks to Modal and others locally.

    By default tasks are executed *detached* (``worker.spawn`` + a tracked
    ``FunctionCall``): the worker invocation survives this process, its
    function call id is recorded in the registry, and a resumed build
    re-attaches to still-running workers instead of re-executing them.
    Pass ``detached=False`` for the legacy blocking ``worker.remote`` mode.

    Example:
        from stardag.build import HybridConcurrentTaskExecutor, RoutedTaskExecutor

        modal_executor = ModalTaskExecutor(
            modal_app_name="my-app",
            worker_selector=lambda task: "gpu" if needs_gpu(task) else "default",
        )
        local_executor = HybridConcurrentTaskExecutor()

        routed = RoutedTaskExecutor(
            executors={"modal": modal_executor, "local": local_executor},
            router=lambda task: "modal" if run_on_modal(task) else "local",
        )
        build([task], task_executor=routed)
    """

    def __init__(
        self,
        *,
        modal_app_name: str,
        worker_selector: WorkerSelector,
        detached: bool = True,
        worker_reports_lifecycle: bool = True,
        reactive: bool = False,
        modal_workspace: str | None = None,
        worker_timeouts: dict[str, int] | None = None,
    ):
        """Initialize Modal executor.

        Args:
            modal_app_name: Name of the Modal app with worker functions.
            worker_selector: Function that selects which Modal worker to use per task.
            detached: Execute tasks as detached spawned function calls
                (restart-safe, re-attachable, explicitly cancellable).
                False restores the legacy blocking ``remote`` calls.
            worker_reports_lifecycle: Whether the deployed workers report the
                task lifecycle (started/completed/suspended/failed events +
                artifacts) themselves via the default :class:`Runner`. When
                True the build engine skips its own completed/suspended/
                resumed reporting for Modal-routed tasks. Set False when
                driving an app deployed with an older stardag version whose
                workers don't self-report, or when using a custom
                ``run_function`` without lifecycle reporting.
            modal_workspace: Explicit Modal workspace name for the executor
                metadata recorded with task starts (UI deep links). Default:
                resolved once from the configured Modal token, best-effort.
            worker_timeouts: Per-worker Modal function ``timeout`` (seconds),
                as declared in the app's ``worker_settings``. Only the
                deploy process can see those settings, so they are passed in
                rather than looked up. Used to derive the execution-claim
                TTL recorded with every start (see
                :meth:`execution_timeout_seconds`); an absent worker simply
                yields no timeout and the registry's default applies.
        """
        self.modal_app_name = modal_app_name
        self.worker_selector = worker_selector
        self.detached = detached
        self.worker_timeouts = dict(worker_timeouts or {})
        self.worker_reports_lifecycle = worker_reports_lifecycle
        # Reactive scheduling: forward the app name + reactive flag so
        # workers register their dynamic deps and wake the scheduler tick.
        self.reactive = reactive
        self.modal_workspace = modal_workspace
        # Executor metadata shared by every start this executor records
        # (per-task starts add the worker function name). Resolved lazily
        # at the first invocation, best-effort — metadata must never fail
        # or delay a task start beyond the one cached lookup.
        self._base_executor_metadata: dict[str, typing.Any] | None = None
        # Cache of worker name -> modal.Function. ``modal.Function.from_name``
        # returns a lazy handle (no network call until invoked), but it is
        # invoked on every ``submit`` so we memoize it per worker name to avoid
        # recreating the handle for every task.
        self._worker_functions: dict[str, modal.Function] = {}
        # Cache of worker name -> resolved function id (``fu-…``), best-effort.
        # A ``None`` value is a *resolved* negative (a failed/timed-out
        # hydration) and is kept so a persistently failing lookup is not
        # re-paid on every task start — membership, not truthiness, marks
        # "resolved". Mirrors the once-resolved memoization of the base
        # metadata dict (which likewise caches a missing app id).
        self._worker_function_ids: dict[str, str | None] = {}
        # One-time (per executor) skew-visibility log; see reports_lifecycle.
        self._reports_lifecycle_logged = False
        # In-flight detached executions by task UUID, for explicit cancel().
        # Asyncio cancellation of ``FunctionCall.get`` does NOT stop the
        # remote call (unlike ``remote.aio``), so FAIL_FAST relies on this.
        self._in_flight: dict[UUID, modal.FunctionCall] = {}

    def _get_worker_function(self, worker_name: str) -> modal.Function:
        """Return the (memoized) ``modal.Function`` handle for a worker."""
        worker_function = self._worker_functions.get(worker_name)
        if worker_function is None:
            worker_function = modal.Function.from_name(
                app_name=self.modal_app_name,
                name=f"worker_{worker_name}",
            )
            self._worker_functions[worker_name] = worker_function
        return worker_function

    async def _get_base_executor_metadata(self) -> dict[str, typing.Any] | None:
        """Resolve the executor metadata shared by all starts (cached).

        Best-effort: resolution failures are logged at debug level and
        yield the identity-only dict — never an exception.
        """
        if self._base_executor_metadata is None:
            metadata: dict[str, typing.Any] = {
                "kind": MODAL_EXECUTOR_NAME,
                "app_name": self.modal_app_name,
            }
            environment: str | None = None
            try:
                workspace = self.modal_workspace or await _get_modal_workspace_aio()
                if workspace:
                    metadata["workspace"] = workspace
                environment = _get_modal_environment()
                if environment:
                    metadata["environment"] = environment
            except Exception:
                logger.debug(
                    "Failed to resolve Modal workspace/environment for "
                    "executor metadata",
                    exc_info=True,
                )
            # App id (``ap-…``): app-wide, so it lives in the base metadata
            # alongside workspace/environment. Resolved once and cached here
            # (the base metadata is memoized per executor). Best-effort —
            # _get_modal_app_id_aio never raises.
            app_id = await _get_modal_app_id_aio(self.modal_app_name, environment)
            if app_id:
                metadata["app_id"] = app_id
            self._base_executor_metadata = metadata
        return self._base_executor_metadata

    async def _metadata_for_worker(
        self, worker_name: str
    ) -> dict[str, typing.Any] | None:
        """Base executor metadata + the worker's function name/id (best-effort)."""
        try:
            base_metadata = await self._get_base_executor_metadata()
            if base_metadata is None:
                return None
            metadata = {**base_metadata, "function_name": f"worker_{worker_name}"}
            # Function id (``fu-…``): per-worker, so it lives here alongside
            # the function name. Best-effort — hydrate the worker handle and
            # read object_id; _get_modal_function_id_aio never raises. Cached
            # per worker name (success *and* failure): a resolved ``None`` is
            # kept so a broken/hung hydration is not re-attempted on every
            # start (membership marks "resolved", not truthiness).
            if worker_name not in self._worker_function_ids:
                self._worker_function_ids[
                    worker_name
                ] = await _get_modal_function_id_aio(
                    self._get_worker_function(worker_name)
                )
            function_id = self._worker_function_ids[worker_name]
            if function_id:
                metadata["function_id"] = function_id
            return metadata
        except Exception:
            logger.debug("Failed to resolve Modal executor metadata", exc_info=True)
            return None

    async def get_executor_metadata(
        self, task: BaseTask
    ) -> dict[str, typing.Any] | None:
        """Executor metadata for ``task`` without spawning anything.

        Runs the worker selector (idempotent) to resolve the function
        name — lets slot-acquiring TASK_STARTED events recorded before
        the spawn carry the same metadata as the post-spawn start.
        """
        try:
            worker_name, _ = _normalize_worker_selection(self.worker_selector(task))
        except Exception:
            logger.debug(
                "Worker selection failed while resolving executor metadata",
                exc_info=True,
            )
            return None
        return await self._metadata_for_worker(worker_name)

    def execution_timeout_seconds(self, task: BaseTask) -> float | None:
        """The Modal ``timeout`` of the worker function this task routes to.

        Modal kills a function call at its ``timeout``, so this is a hard
        upper bound on how long an execution of ``task`` can be alive — the
        one fact that makes an execution claim's expiry defensible rather
        than a guess.

        Returns None when the deployed settings were not passed in (see
        ``worker_timeouts``), when the selected worker declares no timeout,
        or when worker selection fails: none of those is a reason to fail a
        start, and the registry's own default covers them.
        """
        if not self.worker_timeouts:
            return None
        try:
            worker_name, _ = _normalize_worker_selection(self.worker_selector(task))
        except Exception:
            logger.debug(
                "Worker selection failed while resolving the execution timeout",
                exc_info=True,
            )
            return None
        timeout = self.worker_timeouts.get(worker_name)
        return float(timeout) if timeout is not None else None

    async def _prepare_invocation(
        self, task: BaseTask
    ) -> tuple[modal.Function, dict[str, str] | None, dict[str, typing.Any] | None]:
        """Resolve the worker function, env overrides, and executor metadata.

        When ``worker_reports_lifecycle`` and an enclosing build is active,
        the build id is injected as the ``STARDAG_BUILD_ID`` env override so
        the worker-side :class:`Runner` can report lifecycle events. The
        resolved executor metadata rides along the same channel
        (``STARDAG_MODAL_*``) so worker self-reported starts carry it too —
        as does the derived claim TTL, so the worker's own start does not
        re-stamp the claim with the registry's generic default.
        """
        worker_name, env_overrides = _normalize_worker_selection(
            self.worker_selector(task)
        )
        worker_function = self._get_worker_function(worker_name)
        executor_metadata = await self._metadata_for_worker(worker_name)
        if self.worker_reports_lifecycle:
            build_id = get_current_build_id()
            if build_id is not None:
                env_overrides = {
                    **(env_overrides or {}),
                    STARDAG_BUILD_ID_ENV: str(build_id),
                    STARDAG_MODAL_APP_NAME_ENV: self.modal_app_name,
                }
                ttl_seconds = claim_ttl_seconds(task, self)
                if ttl_seconds is not None:
                    env_overrides[STARDAG_CLAIM_TTL_SECONDS_ENV] = str(ttl_seconds)
                # The worker function's own ``timeout``, so the worker can
                # tell a timeout from a cancellation — the two are
                # indistinguishable from inside the container without it.
                # See STARDAG_MODAL_FUNCTION_TIMEOUT_ENV. Same source the
                # claim TTL is derived from, forwarded raw rather than
                # re-derived from the TTL (which has grace folded in).
                timeout_seconds = self.execution_timeout_seconds(task)
                if timeout_seconds is not None:
                    env_overrides[STARDAG_MODAL_FUNCTION_TIMEOUT_ENV] = str(
                        timeout_seconds
                    )
                if executor_metadata is not None:
                    for env_name, key in (
                        (STARDAG_MODAL_WORKSPACE_ENV, "workspace"),
                        (STARDAG_MODAL_ENVIRONMENT_ENV, "environment"),
                        (STARDAG_MODAL_FUNCTION_NAME_ENV, "function_name"),
                        (STARDAG_MODAL_APP_ID_ENV, "app_id"),
                        (STARDAG_MODAL_FUNCTION_ID_ENV, "function_id"),
                    ):
                        value = executor_metadata.get(key)
                        if value:
                            env_overrides[env_name] = value
                if self.reactive:
                    env_overrides[STARDAG_REACTIVE_ENV] = "1"
        return worker_function, env_overrides, executor_metadata

    def reports_lifecycle(self, task: BaseTask) -> bool:
        """Workers self-report lifecycle when enabled and a build is active."""
        active = self.worker_reports_lifecycle and get_current_build_id() is not None
        if active and not self._reports_lifecycle_logged:
            # Make the version-skew failure mode visible: nothing verifies
            # the deployed workers actually self-report. If the app was
            # deployed with an older stardag (or uses a custom run function
            # without lifecycle reporting), tasks will execute fine but sit
            # RUNNING in the registry with artifacts lost.
            self._reports_lifecycle_logged = True
            logger.info(
                "Engine-side lifecycle reporting is suppressed for "
                f"Modal-routed tasks (app {self.modal_app_name!r}): workers "
                "are assumed to self-report started/completed/suspended/"
                "failed events. If the deployed app predates worker "
                "self-reporting or uses a custom run function, pass "
                "ModalTaskExecutor(worker_reports_lifecycle=False) — "
                "otherwise tasks will appear stuck RUNNING in the registry."
            )
        return active

    async def submit(self, task: BaseTask) -> None | TaskStruct | TaskExecutionError:
        """Execute task on Modal (blocking remote call)."""
        try:
            worker_function, env_overrides, _ = await self._prepare_invocation(task)
            res = await worker_function.remote.aio(task, env_overrides=env_overrides)
            return res
        except Exception as e:
            return TaskExecutionError(
                exception=e,
                traceback="".join(tb_module.format_exception(e)),
            )

    # --- Detached execution (spawn + re-attach + cancel) ---

    def supports_detached(self, task: BaseTask) -> bool:
        """Detached mode is per-executor (constructor flag), not per-task."""
        return self.detached

    def _make_handle(
        self,
        task: BaseTask,
        function_call: modal.FunctionCall,
        executor_metadata: dict[str, typing.Any] | None = None,
    ) -> DetachedHandle:
        """Wrap a FunctionCall in a DetachedHandle tracking in-flight state."""
        self._in_flight[task.id] = function_call

        async def wait() -> None | TaskStruct | TaskExecutionError:
            try:
                return await function_call.get.aio()
            except asyncio.CancelledError:
                # The build engine cancels the awaiting future on FAIL_FAST /
                # user cancellation. Unlike ``remote.aio``, cancelling
                # ``get()`` does NOT stop the detached remote call — cancel
                # it explicitly here. This must live in wait() (not only in
                # the executor cancel() hook): the finally below pops the
                # in-flight entry, and the asyncio cancellation typically
                # lands before the hook runs, so the hook would find nothing.
                # Shielded: this coroutine is already being cancelled.
                try:
                    await asyncio.shield(function_call.cancel.aio())
                except Exception as cancel_err:
                    logger.warning(
                        f"Failed to cancel Modal function call "
                        f"{function_call.object_id} for task {task.id} "
                        f"during cancellation: {cancel_err}"
                    )
                raise
            except Exception as e:
                return TaskExecutionError(
                    exception=e,
                    traceback="".join(tb_module.format_exception(e)),
                )
            finally:
                self._in_flight.pop(task.id, None)

        return DetachedHandle(
            executor=MODAL_EXECUTOR_NAME,
            ref=function_call.object_id,
            wait=wait,
            executor_metadata=executor_metadata,
        )

    async def submit_detached(self, task: BaseTask) -> DetachedHandle:
        """Spawn the task on its worker function; return a re-attachable handle."""
        (
            worker_function,
            env_overrides,
            executor_metadata,
        ) = await self._prepare_invocation(task)
        function_call = await worker_function.spawn.aio(
            task, env_overrides=env_overrides
        )
        return self._make_handle(task, function_call, executor_metadata)

    async def reattach(
        self, task: BaseTask, executor: str, ref: str
    ) -> DetachedHandle | None:
        """Re-attach to a spawned function call by id, if it is still live.

        Returns None (→ normal re-execution) when the ref belongs to a
        different backend, the call failed/was cancelled, or the result has
        expired. A call that already finished successfully yields a handle
        resolving immediately to its result.

        Known ambiguity (accepted): Modal's ``get(timeout=0)`` poll timeout
        raises the *builtin* ``TimeoutError`` (modal 1.5), and a task body
        that itself raised ``TimeoutError`` re-raises the same type — such
        a failed call classifies as still-running here. Not narrowable:
        ``modal.exception.TimeoutError`` is not what the poll raises, and
        ``FunctionCall.get_call_graph()`` does not reliably surface input
        status (verified live: stays PENDING after success/cancel). The
        re-attach path self-corrects: awaiting the returned handle's
        ``wait()`` re-raises the failure and the task is recorded failed.
        """
        if executor != MODAL_EXECUTOR_NAME or not self.detached:
            return None
        try:
            function_call = modal.FunctionCall.from_id(ref)
        except Exception:
            return None
        try:
            result = await function_call.get.aio(timeout=0)
        except TimeoutError:
            # Still running (or queued) — the interesting case. Base
            # metadata only: the worker function behind a bare ref isn't
            # known here.
            return self._make_handle(
                task, function_call, await self._get_base_executor_metadata()
            )
        except Exception:
            # Failed, cancelled, expired result, or unknown id — re-execute.
            return None

        async def resolved() -> None | TaskStruct | TaskExecutionError:
            return result

        return DetachedHandle(executor=MODAL_EXECUTOR_NAME, ref=ref, wait=resolved)

    async def detached_status(
        self, task: BaseTask, executor: str, ref: str
    ) -> DetachedExecutionStatus:
        """Non-blocking probe of a spawned function call's state.

        Note: an expired result (>~7 days) and an unknown id also classify
        as FAILED — callers (scheduler ticks) check target existence first,
        so a successfully-finished-long-ago execution is already resolved
        as complete before this is consulted.

        Known ambiguity (accepted, see also ``reattach``): the poll timeout
        is the builtin ``TimeoutError``, indistinguishable from a task body
        that raised ``TimeoutError`` — such a failed execution classifies
        as RUNNING here. In practice the worker's own TASK_FAILED report
        resolves the task first; only a registry-less worker raising
        builtin TimeoutError hits the ambiguity.
        """
        if executor != MODAL_EXECUTOR_NAME or not self.detached:
            return DetachedExecutionStatus.UNKNOWN
        try:
            function_call = modal.FunctionCall.from_id(ref)
            await function_call.get.aio(timeout=0)
        except TimeoutError:
            return DetachedExecutionStatus.RUNNING
        except Exception as e:
            # Transient transport/infrastructure errors must NOT classify as
            # FAILED: callers (claim loser-resolution, tick healing) treat
            # FAILED as proof of death — a network blip mistaken for a dead
            # winner would let a racer record a live execution as failed and
            # spawn the duplicate the claim exists to prevent. UNKNOWN is
            # the safe degradation (leave/wait and re-probe later).
            if _is_transient_modal_error(e):
                logger.warning(
                    f"Transient error probing Modal call {ref!r}; treating "
                    f"as unknown: {e}"
                )
                return DetachedExecutionStatus.UNKNOWN
            return DetachedExecutionStatus.FAILED
        return DetachedExecutionStatus.SUCCEEDED

    async def cancel_detached(self, task: BaseTask, executor: str, ref: str) -> None:
        """Cancel a spawned function call by its recorded id."""
        if executor != MODAL_EXECUTOR_NAME:
            return
        try:
            await modal.FunctionCall.from_id(ref).cancel.aio()
        except Exception as e:
            logger.warning(
                f"Failed to cancel Modal function call {ref!r} for task {task.id}: {e}"
            )

    async def cancel(self, task: BaseTask) -> None:
        """Cancel the tracked in-flight function call for ``task``, if any."""
        function_call = self._in_flight.pop(task.id, None)
        if function_call is None:
            return
        try:
            await function_call.cancel.aio()
        except Exception as e:
            logger.warning(
                f"Failed to cancel Modal function call "
                f"{function_call.object_id} for task {task.id}: {e}"
            )

    async def setup(self) -> None:
        """No setup needed for Modal executor."""
        pass

    async def teardown(self) -> None:
        """No teardown needed for Modal executor."""
        pass
