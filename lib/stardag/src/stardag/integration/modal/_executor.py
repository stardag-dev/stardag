"""The orchestrator side: a ``TaskExecutorABC`` that runs tasks on Modal.

Used by whatever is driving a build — a resident ``build`` function
(:class:`stardag.integration.modal.Builder`), a hybrid local build, or a
reactive scheduler tick — to spawn claimed executions onto the app's
deployed ``worker_*`` functions, and to cancel them.
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
    DetachedHandle,
    TaskExecutionError,
    TaskExecutorABC,
    get_current_build_context,
)
from stardag.build._claims import claim_ttl_seconds
from stardag.build._deployment import STARDAG_DEPLOYMENT_ID_ENV
from stardag.integration.modal._metadata import (
    MODAL_EXECUTOR_NAME,
    STARDAG_BUILD_ID_ENV,
    STARDAG_CLAIM_TTL_SECONDS_ENV,
    STARDAG_EXECUTION_ID_ENV,
    STARDAG_PLAN_ID_ENV,
    STARDAG_MODAL_APP_ID_ENV,
    STARDAG_MODAL_APP_NAME_ENV,
    STARDAG_MODAL_ENVIRONMENT_ENV,
    STARDAG_MODAL_FUNCTION_ID_ENV,
    STARDAG_MODAL_FUNCTION_NAME_ENV,
    STARDAG_MODAL_FUNCTION_TIMEOUT_ENV,
    STARDAG_MODAL_WORKSPACE_ENV,
    STARDAG_REACTIVE_ENV,
    STARDAG_WORKER_REPORTS_LIFECYCLE_ENV,
    _get_modal_app_id_aio,
    _get_modal_environment,
    _get_modal_function_id_aio,
    _get_modal_workspace_aio,
)
from stardag.integration.modal._spawn import spawn_tick
from stardag.integration.modal._selector import (
    WorkerSelector,
    _normalize_worker_selection,
)

logger = logging.getLogger(__name__)


class ModalTaskExecutor(TaskExecutorABC):
    """Task executor that sends tasks to Modal for remote execution.

    This executor submits tasks to Modal worker functions. Use with
    RoutedTaskExecutor to route some tasks to Modal and others locally.

    By default tasks are executed *detached* (``worker.spawn`` + a tracked
    ``FunctionCall``): the worker invocation survives this process, its
    function call id is recorded with its start, and the worker reports its
    own lifecycle naming the execution the engine claimed. Pass
    ``detached=False`` for blocking ``worker.remote`` calls, whose lifecycle
    the engine reports.

    A build whose tasks run here plans under the app's current deployment
    (D13, :meth:`deployment_app_name`): its workers yield into the plan, and
    the registry refuses a yield from another deployment.

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
                (restart-safe, explicitly cancellable).
                False restores the legacy blocking ``remote`` calls.
            worker_reports_lifecycle: Whether the deployed workers report the
                task lifecycle (start with ref, completion, failure, yield,
                artifacts) themselves via the default :class:`Runner`, for
                detached executions. When True the engine reports none of
                those for Modal-routed tasks. Set False for a custom
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
        # workers wake the scheduler tick. That is the whole reactive
        # protocol — a worker that does not report cannot record its end or
        # its yield and wakes no tick, so a reactive build with
        # non-reporting workers would sit RUNNING until its claims lapsed.
        if reactive and not worker_reports_lifecycle:
            raise ValueError(
                "ModalTaskExecutor(reactive=True) needs self-reporting workers: "
                "reactive scheduling has no resident orchestrator, so the "
                "worker itself registers the dependencies it yields and wakes "
                "the scheduler. Deploy the app with stardag's default Runner "
                "(worker_reports_lifecycle=True), or drive the build with a "
                "resident builder instead."
            )
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

    def deployment_app_name(self) -> str | None:
        """The app whose current deployment a build of these tasks plans
        under (D13)."""
        return self.modal_app_name

    async def _prepare_invocation(
        self, task: BaseTask, execution_id: UUID | None = None
    ) -> tuple[modal.Function, dict[str, str] | None, dict[str, typing.Any] | None]:
        """Resolve the worker function, its env overrides, and the executor
        metadata.

        The env overrides are layered in the design's precedence: the worker
        selector's per-task env, then the build's settings, then the
        framework's own identifiers **last** — the build and plan ids and
        the execution id the reports name, the claim TTL, the app name and
        the Modal coordinates — so neither a selector (user code) nor
        settings can redirect a worker's reports. ``STARDAG_DEPLOYMENT_ID``
        is never forwarded (it is the container's, baked by the deploy) and
        is removed from the selector's env.
        ``STARDAG_WORKER_REPORTS_LIFECYCLE`` is likewise framework-owned:
        it is always forced to this engine's own ``reports_lifecycle(task)``
        value, never left at whatever a selector's env happened to carry —
        otherwise a selector/deployment env supplying ``...=0`` could
        suppress the worker's reports while the engine still expects them,
        and the task would sit RUNNING until its claim lapses.
        """
        worker_name, selector_env = _normalize_worker_selection(
            self.worker_selector(task)
        )
        worker_function = self._get_worker_function(worker_name)
        executor_metadata = await self._metadata_for_worker(worker_name)
        env: dict[str, str] = dict(selector_env or {})
        env.pop(STARDAG_DEPLOYMENT_ID_ENV, None)
        context = get_current_build_context()
        if context is None:
            return worker_function, env or None, executor_metadata
        env.update(context.settings)
        env[STARDAG_BUILD_ID_ENV] = str(context.build_id)
        env[STARDAG_MODAL_APP_NAME_ENV] = self.modal_app_name
        if not self.reports_lifecycle(task):
            env[STARDAG_WORKER_REPORTS_LIFECYCLE_ENV] = "0"
            return worker_function, env, executor_metadata
        # Reporting is on: clear whatever the selector/deployment env may
        # have set for this framework-owned var, so a stale "0" cannot
        # silently suppress the worker's reports (see the docstring).
        env.pop(STARDAG_WORKER_REPORTS_LIFECYCLE_ENV, None)
        if context.plan_id is not None:
            env[STARDAG_PLAN_ID_ENV] = str(context.plan_id)
        if execution_id is not None:
            env[STARDAG_EXECUTION_ID_ENV] = str(execution_id)
        ttl_seconds = claim_ttl_seconds(task, self)
        if ttl_seconds is not None:
            env[STARDAG_CLAIM_TTL_SECONDS_ENV] = str(ttl_seconds)
        # The worker function's own ``timeout``, so the worker can tell a
        # timeout from a cancellation (see STARDAG_MODAL_FUNCTION_TIMEOUT_ENV).
        timeout_seconds = self.execution_timeout_seconds(task)
        if timeout_seconds is not None:
            env[STARDAG_MODAL_FUNCTION_TIMEOUT_ENV] = str(timeout_seconds)
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
                    env[env_name] = value
        if self.reactive:
            env[STARDAG_REACTIVE_ENV] = "1"
        return worker_function, env, executor_metadata

    def reports_lifecycle(self, task: BaseTask) -> bool:
        """Workers self-report the lifecycle of a *detached* execution of an
        active build (a blocking ``remote`` call has no execution identity
        of its own; the engine reports it)."""
        active = (
            self.worker_reports_lifecycle
            and self.detached
            and get_current_build_context() is not None
        )
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
                "self-report their start, end and yields. A custom run "
                "function without lifecycle reporting needs "
                "ModalTaskExecutor(worker_reports_lifecycle=False) — "
                "otherwise its tasks stay RUNNING until their claims lapse."
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

    async def submit_detached(
        self, task: BaseTask, *, execution_id: UUID
    ) -> DetachedHandle:
        """Spawn the task on its worker function; return a re-attachable handle."""
        (
            worker_function,
            env_overrides,
            executor_metadata,
        ) = await self._prepare_invocation(task, execution_id)
        function_call = await worker_function.spawn.aio(
            task, env_overrides=env_overrides
        )
        return self._make_handle(task, function_call, executor_metadata)

    def can_spawn_scheduler_ticks(self) -> bool:
        return True

    def spawn_scheduler_tick(self, build_id: UUID, app_name: str) -> None:
        """Spawn the deployed ``tick`` of ``app_name`` for ``build_id``.

        What lets a resident build with Modal workers (a hybrid run) wake the
        reactive builds its completions unblocked — the same call a tick
        makes for its neighbours.
        """
        spawn_tick(build_id, app_name)

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
