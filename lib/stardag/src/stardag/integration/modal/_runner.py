"""Default ``RunFunction`` implementation — what runs inside a worker container.

:class:`Runner` executes one task (sync, async, or dynamic-deps generator) and,
when an orchestrator forwarded a build id, reports that task's whole lifecycle
to the registry from inside the worker via :class:`_WorkerLifecycleReporter`.
Reporting from here rather than from the orchestrator is what makes the events
independent of the orchestrator's lifetime — and it is the only option under
reactive scheduling, where there is no resident orchestrator at all.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import threading
import time
import typing
from uuid import UUID

import modal

from stardag import BaseTask, TaskStruct, flatten_task_struct
from stardag._core.base_task import _has_custom_run, _has_custom_run_aio
from stardag.build import BuildTaskStore, discover_and_register_aio
from stardag.build._task_modules import (
    declared_task_module_patterns,
    format_uncovered_message,
    plan_pickle_elision,
    uncovered_task_classes,
)
from stardag.integration.modal._logging import _setup_logging
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
)
from stardag.integration.modal._protocols import RunFunction
from stardag.exceptions import TaskInterrupted, TaskTimedOut
from stardag.registry._base import NoOpRegistry, registry_provider
from stardag.utils.env import temp_env_vars

# Modal's own "this input was cancelled" BaseException. Looked up rather
# than imported so a client that predates or renames it degrades to "we
# cannot recognise a cancellation" instead of failing every worker import.
_InputCancellation: type[BaseException] | None = getattr(
    modal.exception, "InputCancellation", None
)

logger = logging.getLogger(__name__)

# How long the worker will wait for its interruption report to land before
# giving up and letting the container die. Small relative to the ~60s
# window Modal leaves between the timeout signal and SIGKILL: the report is
# one HTTP call, and the rest of the window belongs to the task's own
# checkpointing.
_INTERRUPT_REPORT_TIMEOUT_SECONDS = 10.0

# Slack when deciding whether an InputCancellation arrived *at* the
# function's declared timeout (a timeout) or before it (a cancellation).
#
# Modal delivers the timeout signal at the declared timeout to the
# millisecond, so this is not compensating for jitter in the signal — it
# covers the gap between the clock the *task* started on (this module,
# just before ``run()``) and the clock Modal is measuring, which starts
# earlier and includes container startup. That gap makes measured elapsed
# time run SHORT of Modal's, so the tolerance is one-sided: only "arrived a
# little before the timeout" needs forgiving.
_TIMEOUT_DETECTION_SLACK_SECONDS = 5.0

# What a caught BaseException meant, and therefore what the worker does.
_PREEMPTION = "preemption"
_TIMEOUT = "timeout"
_CANCELLATION = "cancellation"


def _declared_function_timeout(env_overrides: dict[str, str] | None) -> float | None:
    """The worker function's ``timeout``, as forwarded by the orchestrator.

    Unparseable or non-positive values are dropped rather than raised on:
    this feeds a heuristic whose failure mode is "behave as before", and no
    task should die over a malformed diagnostic.
    """
    raw = (env_overrides or {}).get(
        STARDAG_MODAL_FUNCTION_TIMEOUT_ENV
    ) or os.environ.get(STARDAG_MODAL_FUNCTION_TIMEOUT_ENV)
    if not raw:
        return None
    try:
        timeout = float(raw)
    except ValueError:
        logger.warning(f"Invalid {STARDAG_MODAL_FUNCTION_TIMEOUT_ENV}: {raw!r}")
        return None
    return timeout if timeout > 0 else None


def _classify_interruption(
    exception: BaseException,
    *,
    elapsed_seconds: float,
    function_timeout_seconds: float | None,
) -> str | None:
    """What ended this execution, or ``None`` if the task simply raised.

    The rules, and the live-measured behaviour each one encodes (modal
    1.5.0, 2026-08-12):

    - **The task said so.** ``TaskTimedOut`` and ``TaskPreempted`` are
      explicit. A bare ``TaskInterrupted`` resolves to preemption, which is
      the conservative reading: preemption's handling is "report nothing,
      re-raise", i.e. exactly what happens today, so a task that guesses
      wrong degrades rather than changes behaviour.
    - **A ``KeyboardInterrupt`` is the preemption signal.** Modal sends an
      interrupt when it reclaims a container, and an escaping
      ``BaseException`` reads to Modal as a crashed container — which it
      restarts on the same input, ungated by ``retries``, in a few seconds.
      That is strictly better than a scheduler respawn (the claim is kept,
      no attempt is spent, no round-trip), so the right thing here is to
      get out of the way.
    - **An ``InputCancellation`` is ambiguous and has to be timed.** It is
      what a function timeout looks like *and* what
      ``FunctionCall.cancel()`` looks like — same type, same
      ``"Input was cancelled by user"`` message. Since stardag cancels its
      own workers (FAIL_FAST, UI cancel), reading every one as an
      interruption would resurrect tasks the build just cancelled. Only one
      of them arrives at the declared timeout, so that is the test.

    With no declared timeout to compare against, an ``InputCancellation``
    is read as a cancellation — the conservative direction again, since
    cancellation is also handled by doing nothing.
    """
    if isinstance(exception, TaskTimedOut):
        return _TIMEOUT
    if isinstance(exception, TaskInterrupted):
        # Covers TaskPreempted and the bare base class.
        return _PREEMPTION
    if isinstance(exception, (KeyboardInterrupt, SystemExit)):
        return _PREEMPTION
    if _InputCancellation is not None and isinstance(exception, _InputCancellation):
        if function_timeout_seconds is None:
            return _CANCELLATION
        reached_timeout = (
            elapsed_seconds
            >= function_timeout_seconds - _TIMEOUT_DETECTION_SLACK_SECONDS
        )
        return _TIMEOUT if reached_timeout else _CANCELLATION
    return None


class _WorkerLifecycleReporter:
    """Reports a task's lifecycle events from inside a Modal worker.

    Created by :class:`Runner` when a build id was forwarded (see
    ``STARDAG_BUILD_ID_ENV``) and the container has a configured registry.
    Reporting from the worker makes the events independent of the
    orchestrator's lifetime: completion/failure land even if the build
    function died mid-await, and each (re-)invocation's TASK_STARTED
    carries its own function call id for re-attach.

    All reporting is best-effort: a registry hiccup must never fail a task
    whose actual work succeeded — failures are logged loudly and the
    engine-side self-heal (target-existence check on the next build) covers
    a lost completion event.
    """

    def __init__(
        self,
        registry: typing.Any,
        build_id: UUID,
        task: BaseTask,
        *,
        reactive: bool = False,
        app_name: str | None = None,
        executor_metadata: dict[str, typing.Any] | None = None,
        claim_ttl_seconds: int | None = None,
    ):
        self.registry = registry
        self.build_id = build_id
        self.task = task
        self.reactive = reactive
        self.app_name = app_name
        self.executor_metadata = executor_metadata
        self.claim_ttl_seconds = claim_ttl_seconds

    @classmethod
    def create(
        cls, task: BaseTask, env_overrides: dict[str, str] | None
    ) -> "_WorkerLifecycleReporter | None":
        def _get(key: str) -> str | None:
            return (env_overrides or {}).get(key) or os.environ.get(key)

        raw_build_id = _get(STARDAG_BUILD_ID_ENV)
        if not raw_build_id:
            return None
        try:
            build_id = UUID(raw_build_id)
        except ValueError:
            logger.warning(f"Invalid {STARDAG_BUILD_ID_ENV}: {raw_build_id!r}")
            return None
        registry = registry_provider.get()
        # Exact-type check: only the literal do-nothing default suppresses
        # reporting — NoOpRegistry *subclasses* may implement real behavior.
        if type(registry) is NoOpRegistry:
            return None
        app_name = _get(STARDAG_MODAL_APP_NAME_ENV)
        # Executor metadata forwarded by the orchestrator's executor (same
        # dict it records on its own starts). Values missing on older
        # orchestrators are simply omitted.
        executor_metadata: dict[str, typing.Any] = {"kind": MODAL_EXECUTOR_NAME}
        if app_name:
            executor_metadata["app_name"] = app_name
        for key, env_name in (
            ("workspace", STARDAG_MODAL_WORKSPACE_ENV),
            ("environment", STARDAG_MODAL_ENVIRONMENT_ENV),
            ("function_name", STARDAG_MODAL_FUNCTION_NAME_ENV),
            ("app_id", STARDAG_MODAL_APP_ID_ENV),
            ("function_id", STARDAG_MODAL_FUNCTION_ID_ENV),
        ):
            value = _get(env_name)
            if value:
                executor_metadata[key] = value
        # The orchestrator's derived claim TTL, if it sent one. Malformed
        # values are ignored rather than raised on: this is a bound on an
        # expiry, and no worker should fail to report its own start over it.
        raw_ttl = _get(STARDAG_CLAIM_TTL_SECONDS_ENV)
        try:
            ttl_seconds = int(raw_ttl) if raw_ttl else None
        except ValueError:
            logger.warning(f"Invalid {STARDAG_CLAIM_TTL_SECONDS_ENV}: {raw_ttl!r}")
            ttl_seconds = None
        # A syntactically valid but out-of-range value is the same problem
        # as a malformed one, and worse in effect: the server rejects it
        # (422) on `task_start`, so the worker loses its whole lifecycle
        # report over a bound on an expiry. Drop it and let the server pick
        # its default.
        if ttl_seconds is not None and ttl_seconds <= 0:
            logger.warning(
                f"Ignoring {STARDAG_CLAIM_TTL_SECONDS_ENV}={raw_ttl!r}: a claim "
                "TTL must be positive. The server's default applies instead."
            )
            ttl_seconds = None
        return cls(
            registry,
            build_id,
            task,
            reactive=_get(STARDAG_REACTIVE_ENV) == "1",
            app_name=app_name,
            executor_metadata=executor_metadata,
            claim_ttl_seconds=ttl_seconds,
        )

    def _guard(self, fn: typing.Callable[[], None], what: str) -> None:
        try:
            fn()
        except Exception:
            logger.exception(
                f"Worker lifecycle report ({what}) failed for task {self.task.id}"
            )

    def started(self) -> None:
        def _do() -> None:
            ref: str | None = None
            try:
                ref = modal.current_function_call_id()
            except Exception:
                pass
            self.registry.task_start(
                self.build_id,
                self.task,
                executor=MODAL_EXECUTOR_NAME,
                executor_ref=ref,
                executor_metadata=self.executor_metadata,
                claim_ttl_seconds=self.claim_ttl_seconds,
            )

        self._guard(_do, "start")

    def completed(self) -> None:
        self._guard(
            lambda: self.registry.task_complete(self.build_id, self.task),
            "complete",
        )

        def _artifacts() -> None:
            artifacts = self.task.artifacts()
            if artifacts:
                self.registry.task_upload_artifacts(self.build_id, self.task, artifacts)

        self._guard(_artifacts, "artifacts")
        self._wake_scheduler()

    def suspended(self, task_struct: TaskStruct | None = None) -> None:
        if self.reactive and task_struct is not None:
            # No resident orchestrator to pick up the yielded deps: register
            # them (with their requires() subtrees), persist their pickles
            # for the scheduler, and record the dynamic edges — BEFORE the
            # suspend event, so the frontier is consistent when a tick runs.
            self._guard(
                lambda: self._register_dynamic_deps(task_struct), "dynamic-deps"
            )
        self._guard(
            lambda: self.registry.task_suspend(self.build_id, self.task),
            "suspend",
        )
        self._wake_scheduler()

    def failed(self, exception: BaseException) -> None:
        self._guard(
            lambda: self.registry.task_fail(
                self.build_id, self.task, error_message=str(exception)
            ),
            "fail",
        )
        self._wake_scheduler()

    def interrupted(self, reason: str) -> None:
        """Report that the platform ended this execution — not a failure.

        Deliberately never ``task_fail``: a worker-recorded failure lands in
        the next frontier snapshot and, under FAIL_FAST, kills the build
        before any scheduler can retry it. A tick gets away with
        record-then-retry only because both halves happen inside one pass.

        **Bounded, because this runs in a dying container.** Modal gives
        roughly 60s between the timeout signal and the hard kill, shared
        with whatever the task did to checkpoint. A registry that hangs
        must not spend the rest of it — the whole value here is promptness,
        and the fallback (report nothing, let a later tick discover the
        dead execution) is exactly the behaviour that predates this method.
        """
        done = threading.Event()

        def _report() -> None:
            self._guard(
                lambda: self.registry.task_interrupt(
                    self.build_id, self.task, reason=reason
                ),
                "interrupt",
            )
            self._guard(self._wake_scheduler, "interrupt-wake")
            done.set()

        thread = threading.Thread(target=_report, daemon=True)
        thread.start()
        if not done.wait(_INTERRUPT_REPORT_TIMEOUT_SECONDS):
            logger.error(
                f"Reporting the interruption of task {self.task.id} did not "
                f"finish within {_INTERRUPT_REPORT_TIMEOUT_SECONDS}s; giving "
                "up on it so the container can exit. The execution claim "
                "stays held until a scheduler observes the execution is gone."
            )

    def _register_dynamic_deps(self, task_struct: TaskStruct) -> None:
        result = asyncio.run(
            discover_and_register_aio(self.registry, self.build_id, task_struct)
        )
        store = BuildTaskStore(self.build_id)
        # The trigger's pre-flight structurally cannot see dynamically
        # yielded deps — they don't exist until their parent runs — so the
        # coverage check is re-run here, on the app's patterns as published
        # by the deployed worker wrapper. Once per class per process: this
        # runs on every suspending worker invocation.
        #
        # The same elision applies (see
        # :func:`stardag.integration.modal._bootstrap._persist_discovered_tasks`):
        # without it the pickle-free property would hold only until a task
        # yielded its first dynamic dependency, and a build with dynamic
        # deps would still need target-root write access.
        patterns = declared_task_module_patterns()
        if patterns:
            uncovered = uncovered_task_classes(
                result.incomplete.values(), patterns, only_unwarned=True
            )
            if uncovered:
                logger.warning(
                    format_uncovered_message(
                        uncovered,
                        patterns,
                        remedy=(
                            "These were registered as dynamic dependencies, "
                            "so the trigger's pre-flight could not see them."
                        ),
                    )
                )
            plan = plan_pickle_elision(result.incomplete.values(), patterns)
            store.save_tasks(task for task, _ in plan.pickled)
            logger.info(
                f"Build {self.build_id} dynamic deps of task "
                f"{self.task.id}: {plan.summary()}"
            )
        else:
            store.save_tasks(result.incomplete.values())
        deps = flatten_task_struct(task_struct)
        self.registry.task_add_dependencies(
            self.build_id, self.task, deps, is_dynamic=True
        )

    def _wake_scheduler(self) -> None:
        """Reactive wake-up: flag the build dirty, then spawn a tick.

        Order matters: the flag is set *before* the spawn, so if the tick
        finds the scheduler lease held, the holder's linger re-check is
        guaranteed to observe the wake-up.
        """
        if not self.reactive:
            return
        self._guard(lambda: self.registry.build_notify(self.build_id), "notify")
        app_name = self.app_name
        if app_name is None:
            logger.warning(
                "Reactive build without an app name — cannot spawn a "
                "scheduler tick (relying on the watchdog)."
            )
            return

        def _spawn_tick() -> None:
            modal.Function.from_name(app_name=app_name, name="tick").spawn(
                build_id=str(self.build_id)
            )

        self._guard(_spawn_tick, "tick-spawn")


class Runner(RunFunction):
    """Default runner implementation with overridable setup/teardown.

    Override ``setup()``/``teardown()``/``run()`` to customize behavior.
    Pass an instance to ``StardagApp(run_function=MyRunner())``.

    Example:

    .. code-block:: python

        class MyRunner(Runner):
            def setup(self, task):
                super().setup(task)
                torch.cuda.set_device(0)

        stardag_app = StardagApp(
            "my-app",
            run_function=MyRunner(),
            ...
        )
    """

    def __init__(self, *, report_lifecycle: bool = True):
        """Initialize the runner.

        Args:
            report_lifecycle: Report the task's lifecycle events
                (started/completed/suspended/failed + artifacts) to the
                registry from inside the worker, when a build id was
                forwarded by the executor and the container has registry
                credentials. See :class:`_WorkerLifecycleReporter`.
        """
        self.report_lifecycle = report_lifecycle

    def setup(self, task: BaseTask) -> None:
        """Optional setup logic before the task runs."""
        _setup_logging()

    def teardown(self, task: BaseTask, exception: BaseException | None) -> None:
        """Optional teardown logic after the task runs.

        ``exception`` widened from ``Exception`` when the runner started
        catching ``BaseException``: a task killed by a platform interrupt
        is exactly the case where teardown matters most, and it used to
        bypass this hook entirely. Overrides typed against the narrower
        signature keep working — the value is only ever passed in.
        """
        if exception:
            logger.error(f"Task {repr(task)} raised an exception: {repr(exception)}")

    def __call__(
        self, task: BaseTask, *, env_overrides: dict[str, str] | None = None
    ) -> None | TaskStruct:
        """Core logic to execute a single task.

        Returns ``None`` when the task completed, or a ``TaskStruct`` of
        dynamic dependencies that were not yet complete (idempotent
        re-execution pattern — see ``run``).

        Args:
            task: The task instance to execute.
            env_overrides: Optional environment variable overrides (selected by
                the ``worker_selector`` — see :data:`WorkerSelection`). When
                provided, they are set temporarily around the ``run`` call and
                the previous environment is restored afterwards.
        """
        # getattr: tolerate subclasses overriding __init__ without super()
        result: None | TaskStruct = None
        exception: BaseException | None = None
        try:
            self.setup(task)
            # All lifecycle reporting happens inside the env-overrides
            # context, so overrides carrying environment-sensitive config
            # apply to reporting exactly as they do to run(). (Caveat:
            # stardag's config/registry providers cache on first access —
            # registry connection settings should come from the container's
            # process environment, i.e. deployment secrets, not overrides.)
            with temp_env_vars(env_overrides or {}):
                # getattr: tolerate subclasses overriding __init__ w/o super()
                reporter: _WorkerLifecycleReporter | None = None
                if getattr(self, "report_lifecycle", True):
                    try:
                        reporter = _WorkerLifecycleReporter.create(task, env_overrides)
                    except Exception:
                        # Best-effort contract covers creation too: a broken
                        # registry config must not fail a task before it runs.
                        logger.exception(
                            "Worker lifecycle reporter creation failed; "
                            "running without lifecycle reporting."
                        )
                if reporter is not None:
                    reporter.started()
                function_timeout = _declared_function_timeout(env_overrides)
                started_at = time.monotonic()
                try:
                    result = self.run(task)
                except BaseException as e:
                    kind = self._report_end_of_attempt(
                        task,
                        e,
                        reporter=reporter,
                        elapsed_seconds=time.monotonic() - started_at,
                        function_timeout_seconds=function_timeout,
                    )
                    if kind == _PREEMPTION and isinstance(e, Exception):
                        # The task raised ``sd.TaskInterrupted`` — an
                        # ordinary Exception, deliberately, so it does not
                        # slip past the user's own error handling. But an
                        # ordinary exception leaving the container is a
                        # *task failure* to the execution backend, which
                        # will not restart the input for it. A BaseException
                        # escaping reads as a crashed container, which it
                        # will. Translating here is what makes
                        # "raise sd.TaskInterrupted" mean what it says.
                        raise KeyboardInterrupt(
                            f"Task reported an interruption: {e}"
                        ) from e
                    raise
                if reporter is not None:
                    if result is None:
                        reporter.completed()
                    else:
                        reporter.suspended(result)
        except BaseException as e:
            exception = e
            raise
        finally:
            self.teardown(task, exception)
        return result

    def _report_end_of_attempt(
        self,
        task: BaseTask,
        exception: BaseException,
        *,
        reporter: "_WorkerLifecycleReporter | None",
        elapsed_seconds: float,
        function_timeout_seconds: float | None,
    ) -> str | None:
        """Record what ended this attempt — or deliberately record nothing.

        Returns the classification, which the caller needs in order to
        choose how the exception leaves the container (see ``__call__``).

        Three of the four outcomes below report nothing at all, and that is
        the substance of the change rather than an omission:

        - **Preemption.** The exception is on its way out of the container,
          and an escaping ``BaseException`` reads to Modal as a crash — so
          Modal restarts the input itself, on the same call id, in a few
          seconds. Recording a terminal event here would replace that with
          a slower scheduler respawn *and* release a claim the restart is
          about to need. The previous code reached the same outcome by
          accident, because ``except Exception`` did not catch the
          interrupt; the difference now is that a task which caught the
          signal to checkpoint and raised ``sd.TaskInterrupted`` gets it
          too, instead of a permanent failure.
        - **Cancellation.** Somebody cancelled this call — usually stardag
          itself, on FAIL_FAST or a UI cancel, and whoever did it has
          already recorded the outcome. Reporting an interruption here
          would resurrect a task the build just cancelled.
        - **A raised ``BaseException`` that is none of the above** (an
          unclassifiable ``GeneratorExit``, say). Left alone: the previous
          behaviour for anything outside ``except Exception``.

        Only a **timeout** is reported, because it is the only one where
        nothing else will recover the task: after the timeout fires, the
        call is dead whatever the container does next (verified live —
        returning cleanly, re-raising an interrupt and raising an ordinary
        exception all resolve to ``FunctionTimeoutError``), so a registry
        event is the only path back into the frontier.
        """
        kind = _classify_interruption(
            exception,
            elapsed_seconds=elapsed_seconds,
            function_timeout_seconds=function_timeout_seconds,
        )
        if reporter is None:
            return kind
        if kind is None:
            if isinstance(exception, Exception):
                reporter.failed(exception)
            else:
                logger.warning(
                    f"Task {task.id} ended with {type(exception).__name__}, "
                    "which is neither an ordinary failure nor a recognised "
                    "platform interruption; reporting nothing and letting "
                    "it propagate."
                )
            return kind
        if kind == _TIMEOUT:
            # The declared timeout is usually known — it is what the
            # classification compared against. Not always, though: a task
            # that raises ``TaskTimedOut`` itself is believed regardless,
            # and an orchestrator that forwarded no timeout leaves nothing
            # to name. Saying "the worker function's Nones timeout" in an
            # error a user reads is worse than not naming a number.
            of_timeout = (
                f" of {function_timeout_seconds}s"
                if function_timeout_seconds is not None
                else ""
            )
            logger.warning(
                f"Task {task.id} hit its worker function's timeout"
                f"{of_timeout} after {elapsed_seconds:.1f}s. Reporting an "
                "interruption — the task is not at fault, and the scheduler "
                "decides whether to resume it or fail it "
                "(TickConfig.interruption_policy_selector)."
            )
            reporter.interrupted(
                f"Execution hit the worker function's timeout{of_timeout} "
                f"after {elapsed_seconds:.1f}s"
            )
            return kind
        if kind == _CANCELLATION:
            logger.info(
                f"Execution of task {task.id} was cancelled. Recording "
                "nothing: whoever cancelled it owns that record."
            )
            return kind
        logger.warning(
            f"Execution of task {task.id} was interrupted by the platform "
            f"after {elapsed_seconds:.1f}s. Recording nothing and letting "
            "the interrupt propagate, so the execution backend restarts "
            "this input itself — faster than a reschedule, and it keeps "
            "the execution claim."
        )
        return kind

    def run(self, task: BaseTask) -> None | TaskStruct:
        """Default run logic — handles sync, async, and dynamic deps tasks.

        Dispatch policy:

        - **Async-only** (``run_aio`` defined, ``run`` not overridden):
          async generator ``run_aio`` is driven via ``_drive_async_generator``;
          otherwise ``asyncio.run(task.run_aio())``.
        - **Sync-only and dual** (``run`` defined, with or without ``run_aio``):
          ``task.run()`` is called. If it returns a sync generator it is
          driven via ``_drive_sync_generator``. Dual tasks intentionally
          prefer the sync path here because the Modal worker invocation is
          itself synchronous — if you need async execution for a dual task,
          implement it in ``run()`` (e.g. via ``asyncio.run`` internally).

        Generators cannot be serialized across the Modal boundary, so we
        mirror ``_run_task_in_process``: drive forward while yielded batches
        are fully complete, and at the first yield with any incomplete dep
        return the entire yielded ``TaskStruct``. The ``ModalTaskExecutor``
        builds those deps (filtering for incomplete ones) and re-invokes this
        function — on re-execution the generator advances past the
        now-complete batch.
        """
        has_run_aio = _has_custom_run_aio(task)
        has_run = _has_custom_run(task)

        if has_run_aio and not has_run:
            # Async-only task
            if inspect.isasyncgenfunction(type(task).run_aio):
                return asyncio.run(_drive_async_generator(task))
            asyncio.run(task.run_aio())
            return None

        # Sync (or dual) task — run and drive generator if returned.
        # Dual tasks deliberately take the sync path; see method docstring.
        return _drive_sync_generator(task.run())


def _drive_sync_generator(
    result: None | typing.Generator[TaskStruct, None, None] | TaskStruct,
) -> None | TaskStruct:
    """Drive a sync generator result for idempotent re-execution.

    Advances the generator past yield batches whose deps are all complete.
    Stops at the first yield with any incomplete dep and returns **all** of
    that yield's deps — including the already-complete ones, which the
    caller is expected to filter out when scheduling. If the generator
    completes, returns ``None``.

    The returned ``TaskStruct`` is the yielded one **flattened** to a tuple
    of tasks, not its original shape: a task that yields a nested structure
    (a list of lists, a dict of tasks) gets a flat tuple of the same tasks
    back. Nothing downstream needs the nesting — the build engine flattens
    to schedule — and the flat form is what survives the Modal boundary.

    If ``result`` is ``None`` (no dynamic deps) returns ``None``. If
    ``result`` is already a ``TaskStruct`` (unusual but possible when a
    user's ``run()`` returns deps directly) it is returned as-is.
    """
    if result is None:
        return None
    if not hasattr(result, "__next__"):
        # Already a TaskStruct (unusual, but handle it)
        return typing.cast(TaskStruct, result)

    gen = typing.cast(typing.Generator[TaskStruct, None, None], result)
    try:
        while True:
            yielded = next(gen)
            deps = flatten_task_struct(yielded)
            incomplete = [dep for dep in deps if not dep.complete()]
            if incomplete:
                return tuple(deps)
    except StopIteration:
        return None


async def _drive_async_generator(task: BaseTask) -> None | TaskStruct:
    """Drive an async generator ``run_aio`` for idempotent re-execution.

    Same contract as ``_drive_sync_generator``: advances past fully-complete
    yield batches and returns the first batch that contains any incomplete
    dep, flattened to a tuple of tasks and including the already-complete
    ones. Returns ``None`` when the generator finishes.
    """
    agen = typing.cast(
        typing.AsyncGenerator[TaskStruct, None],
        task.run_aio(),  # type: ignore[assignment]
    )
    # No `except StopAsyncIteration` here, unlike the sync driver's
    # `except StopIteration`: `async for` consumes the generator's
    # exhaustion itself and never propagates it, so such a handler could
    # not do the job it appears to do. The only way to reach it would be a
    # StopAsyncIteration raised by the loop *body* — `complete()`, say —
    # and swallowing that would return None, which the caller reads as
    # "task completed" and reports as such. An error must propagate
    # instead. (A generator that raises it internally already surfaces as
    # RuntimeError, so that path never reached the handler either.)
    async for yielded in agen:
        deps = flatten_task_struct(yielded)
        incomplete = [dep for dep in deps if not dep.complete()]
        if incomplete:
            return tuple(deps)
    return None


_default_run = Runner()
