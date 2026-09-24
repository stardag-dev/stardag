"""The worker side of the registry: a task's lifecycle, reported from its
Modal container.

:class:`_WorkerLifecycleReporter` is created by :class:`~._runner.Runner`
when the orchestrator forwarded a build, a plan and an execution (see
``STARDAG_PLAN_ID`` / ``STARDAG_EXECUTION_ID``) and the container has a
registry. Every report names that execution, and the registry applies a
report only while the execution holds the task's claim — a report from a
container whose claim moved on is recorded as late and changes nothing.

- :meth:`started` — the holder's non-claiming start, with this container's
  call id; a refusal means this execution is no longer wanted.
- :meth:`completed` / :meth:`failed` — the execution's end.
- :meth:`suspended` — one ``/yield``: the yielded children with their static
  closure (walked here, stopping at complete tasks), the dynamic edges, and
  the parent SUSPENDED with its claim released (the container exits). The
  yield carries this container's ``STARDAG_DEPLOYMENT_ID`` and the registry
  refuses it (``deployment_mismatch``) unless it is the plan's. A failure to
  register is **not** swallowed: the task is reported FAILED instead.
- :meth:`interrupted` / :meth:`preempted` — the platform ended (or will
  restart) the execution.

A reactive worker then wakes the build's scheduler.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import typing
from uuid import UUID

import modal

from stardag import BaseTask, TaskStruct, flatten_task_struct
from stardag.build._deployment import own_deployment_id
from stardag.build._registration import send_yield, walk_aio, yield_batches
from stardag.build._task_modules import (
    declared_task_module_patterns,
    format_uncovered_message,
    uncovered_task_classes,
)
from stardag.cancellation import CancellationChecker
from stardag.exceptions import APIError, execution_not_wanted
from stardag.integration.modal._metadata import (
    MODAL_EXECUTOR_NAME,
    STARDAG_BUILD_ID_ENV,
    STARDAG_CLAIM_TTL_SECONDS_ENV,
    STARDAG_EXECUTION_ID_ENV,
    STARDAG_MODAL_APP_ID_ENV,
    STARDAG_MODAL_APP_NAME_ENV,
    STARDAG_MODAL_ENVIRONMENT_ENV,
    STARDAG_MODAL_FUNCTION_ID_ENV,
    STARDAG_MODAL_FUNCTION_NAME_ENV,
    STARDAG_MODAL_WORKSPACE_ENV,
    STARDAG_PLAN_ID_ENV,
    STARDAG_REACTIVE_ENV,
    STARDAG_WORKER_REPORTS_LIFECYCLE_ENV,
)
from stardag.integration.modal._spawn import spawn_tick
from stardag.registry import is_noop_registry, registry_provider

logger = logging.getLogger(__name__)

_T = typing.TypeVar("_T")

# How long the worker waits for an interruption report to land before
# letting the container die: small relative to the ~60s Modal leaves between
# the signal and SIGKILL, which belongs to the task's own checkpointing.
_INTERRUPT_REPORT_TIMEOUT_SECONDS = 10.0


def _uuid(raw: str | None, name: str) -> UUID | None:
    if not raw:
        return None
    try:
        return UUID(raw)
    except ValueError:
        logger.warning(f"Invalid {name}: {raw!r}")
        return None


def _positive_int(raw: str | None, name: str) -> int | None:
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value <= 0:
        logger.warning(f"Invalid {name}: {raw!r}; ignoring it")
        return None
    return value


class _WorkerLifecycleReporter:
    """See the module docstring. All reporting except the yield is
    best-effort: a registry hiccup must never fail a task whose work
    succeeded (a lost completion is re-observed by the next walk: the
    target exists)."""

    def __init__(
        self,
        registry: typing.Any,
        task: BaseTask,
        *,
        build_id: UUID,
        plan_id: UUID,
        execution_id: UUID,
        reactive: bool = False,
        app_name: str | None = None,
        executor_metadata: dict[str, typing.Any] | None = None,
        claim_ttl_seconds: int | None = None,
    ):
        self.registry = registry
        self.task = task
        self.build_id = build_id
        self.plan_id = plan_id
        self.execution_id = execution_id
        self.reactive = reactive
        self.app_name = app_name
        self.executor_metadata = executor_metadata
        # The claim TTL the orchestrator computed for this execution; sent
        # with the start so a restart after a preemption gets a fresh claim
        # of that length rather than the registry's default.
        self.claim_ttl_seconds = claim_ttl_seconds
        self.cancellation = CancellationChecker(self._ask_if_superseded)

    @classmethod
    def create(
        cls, task: BaseTask, env_overrides: dict[str, str] | None
    ) -> "_WorkerLifecycleReporter | None":
        """A reporter for this invocation, or None when there is nothing to
        report to (no build/plan/execution forwarded, reporting switched
        off, or no registry in this container)."""

        def _get(key: str) -> str | None:
            return (env_overrides or {}).get(key) or os.environ.get(key)

        if _get(STARDAG_WORKER_REPORTS_LIFECYCLE_ENV) == "0":
            return None
        build_id = _uuid(_get(STARDAG_BUILD_ID_ENV), STARDAG_BUILD_ID_ENV)
        plan_id = _uuid(_get(STARDAG_PLAN_ID_ENV), STARDAG_PLAN_ID_ENV)
        execution_id = _uuid(_get(STARDAG_EXECUTION_ID_ENV), STARDAG_EXECUTION_ID_ENV)
        if build_id is None or plan_id is None or execution_id is None:
            return None
        registry = registry_provider.get()
        if is_noop_registry(registry):
            return None
        app_name = _get(STARDAG_MODAL_APP_NAME_ENV)
        metadata: dict[str, typing.Any] = {"kind": MODAL_EXECUTOR_NAME}
        if app_name:
            metadata["app_name"] = app_name
        for key, env_name in (
            ("workspace", STARDAG_MODAL_WORKSPACE_ENV),
            ("environment", STARDAG_MODAL_ENVIRONMENT_ENV),
            ("function_name", STARDAG_MODAL_FUNCTION_NAME_ENV),
            ("app_id", STARDAG_MODAL_APP_ID_ENV),
            ("function_id", STARDAG_MODAL_FUNCTION_ID_ENV),
        ):
            value = _get(env_name)
            if value:
                metadata[key] = value
        return cls(
            registry,
            task,
            build_id=build_id,
            plan_id=plan_id,
            execution_id=execution_id,
            reactive=_get(STARDAG_REACTIVE_ENV) == "1",
            app_name=app_name,
            executor_metadata=metadata,
            claim_ttl_seconds=_positive_int(
                _get(STARDAG_CLAIM_TTL_SECONDS_ENV), STARDAG_CLAIM_TTL_SECONDS_ENV
            ),
        )

    @property
    def _task_id(self) -> str:
        return str(self.task.id)

    def _guard(self, fn: typing.Callable[[], _T], what: str) -> "_T | None":
        try:
            return fn()
        except Exception:
            logger.exception(
                f"Worker lifecycle report ({what}) failed for task {self.task.id}"
            )
            return None

    def _ask_if_superseded(self) -> bool:
        """One registry read: is this execution still the one to run? True
        only when the registry positively said no (fail-open, see
        ``stardag.cancellation``)."""
        try:
            unended = self.registry.build_list_executions(self.build_id)
        except Exception:
            logger.debug(
                f"Could not read the executions of build {self.build_id}; "
                "assuming this one is still wanted.",
                exc_info=True,
            )
            return False
        execution = next((e for e in unended if e.id == self.execution_id), None)
        # Absent from the unended list: its end was reported (by a stop, or
        # by another hand) -- as positive a "no" as a released claim.
        if execution is not None and execution.still_wanted:
            return False
        outcome = "ended" if execution is None else execution.claim_outcome
        logger.warning(
            f"Execution {self.execution_id} of task {self.task.id} is no longer "
            f"wanted (claim {outcome or 'released'}); stopping at the next "
            "checkpoint."
        )
        return True

    @staticmethod
    def _executor_ref() -> str | None:
        """This container's call id (the ref recorded with its start)."""
        try:
            return modal.current_function_call_id()
        except Exception:
            return None

    # -- reports -----------------------------------------------------------------

    def started(self) -> None:
        """The non-claiming start — and its answer: a refusal naming a
        claim that moved on is this worker's cheapest cancellation
        checkpoint."""

        def _do() -> None:
            try:
                self.registry.member_start(
                    self.plan_id,
                    self._task_id,
                    execution_id=self.execution_id,
                    claim=False,
                    claim_ttl_seconds=self.claim_ttl_seconds,
                    executor=MODAL_EXECUTOR_NAME,
                    executor_ref=self._executor_ref(),
                    executor_metadata=self.executor_metadata,
                )
            except APIError as e:
                if not execution_not_wanted(e):
                    raise
                self.cancellation.note_cancelled(
                    f"the registry refused its start: {e.code}"
                )

        self._guard(_do, "start")

    def completed(self) -> None:
        self._guard(
            lambda: self.registry.member_complete(
                self.plan_id, self._task_id, execution_id=self.execution_id
            ),
            "complete",
        )

        def _artifacts() -> None:
            artifacts = self.task.artifacts()
            if artifacts:
                self.registry.task_upload_artifacts(
                    self._task_id, artifacts, execution_id=self.execution_id
                )

        self._guard(_artifacts, "artifacts")
        self._guard(self._wake_scheduler, "wake")

    def failed(self, exception: BaseException) -> None:
        self._guard(
            lambda: self.registry.member_fail(
                self.plan_id,
                self._task_id,
                execution_id=self.execution_id,
                error_message=f"{type(exception).__name__}: {exception}",
            ),
            "fail",
        )
        self._guard(self._wake_scheduler, "wake")

    def suspended(self, task_struct: TaskStruct) -> None:
        """Register the yield and suspend (see the module docstring)."""
        try:
            self._yield(task_struct)
        except Exception as e:
            logger.exception(
                f"Registering the dynamic dependencies of task {self.task.id} "
                "failed; reporting the task failed rather than suspending it "
                "on children the registry did not see."
            )
            self.failed(e)
            return
        self._guard(self._wake_scheduler, "wake")

    def _yield(self, task_struct: TaskStruct) -> None:
        deployment_id = own_deployment_id()
        if deployment_id is None:
            raise RuntimeError(
                "This worker has no STARDAG_DEPLOYMENT_ID (it is not running in "
                "a deployment made by `stardag modal deploy`), so it cannot "
                "yield into a plan: the registry accepts a yield only from the "
                "plan's own deployment."
            )
        children = flatten_task_struct(task_struct)
        walk = asyncio.run(walk_aio(children))
        self._warn_uncovered(walk.incomplete)
        send_yield(
            self.registry,
            yield_batches(walk, children, suspend=True),
            plan_id=self.plan_id,
            task_id=self._task_id,
            execution_id=self.execution_id,
            deployment_id=deployment_id,
        )

    @staticmethod
    def _warn_uncovered(tasks: typing.Sequence[BaseTask]) -> None:
        """The bootstrap's coverage pre-flight cannot see dynamic
        dependencies; warn (once per class per process) for the ones a tick
        of this app could not rehydrate. A warning, not a raise: the parent
        has run, and failing its bookkeeping would throw that work away."""
        patterns = declared_task_module_patterns()
        if not patterns:
            return
        uncovered = uncovered_task_classes(tasks, patterns, only_unwarned=True)
        if uncovered:
            logger.warning(
                format_uncovered_message(
                    uncovered,
                    patterns,
                    remedy=(
                        "These were yielded as dynamic dependencies, so the "
                        "bootstrap's pre-flight could not see them; a scheduler "
                        "tick excludes each one it reaches."
                    ),
                )
            )

    def interrupted(self, reason: str) -> None:
        """The platform ended this execution and nothing restarts it:
        INTERRUPTED (actionable), the claim released. Never ``fail``."""
        self._report_in_grace_window(
            lambda: self.registry.member_interrupt(
                self.plan_id,
                self._task_id,
                execution_id=self.execution_id,
                error_message=reason,
            ),
            label="interrupt",
        )

    def preempted(self, reason: str) -> None:
        """The backend restarts this execution itself: no status change, the
        claim kept (due to lapse soon, so a restart that never comes is
        noticed)."""
        self._report_in_grace_window(
            lambda: self.registry.member_preempt(
                self.plan_id, self._task_id, execution_id=self.execution_id
            ),
            label="preempt",
        )

    def _report_in_grace_window(
        self, call: typing.Callable[[], typing.Any], *, label: str
    ) -> None:
        """``call`` and a wake-up, bounded by a deadline: this runs in a
        dying container."""
        done = threading.Event()

        def _report() -> None:
            self._guard(call, label)
            self._guard(self._wake_scheduler, f"{label}-wake")
            done.set()

        threading.Thread(target=_report, daemon=True).start()
        if not done.wait(_INTERRUPT_REPORT_TIMEOUT_SECONDS):
            logger.error(
                f"Reporting the {label} of task {self.task.id} did not finish "
                f"within {_INTERRUPT_REPORT_TIMEOUT_SECONDS}s; giving up so the "
                "container can exit. The claim lapses on its own."
            )

    def _wake_scheduler(self) -> None:
        """Reactive wake-up: flag the build, then spawn a tick — unless the
        registry says a scheduler holds the lease (it re-reads the flag
        after releasing, the exit handshake), or the build wants no tick.
        Unknown (the notify raised) spawns: a redundant tick costs a
        container, a skipped one costs the build its progress."""
        if not self.reactive:
            return
        app_name = self.app_name
        notified = self._guard(
            lambda: self.registry.build_notify(
                self.build_id, can_spawn=app_name is not None
            ),
            "notify",
        )
        if notified is not None and not notified.needs_tick:
            return
        if notified is not None and notified.scheduler_live is True:
            return
        if app_name is None:
            logger.warning(
                "Reactive build without an app name — cannot spawn a scheduler "
                "tick (relying on the watchdog)."
            )
            return
        self._guard(lambda: spawn_tick(self.build_id, app_name), "tick-spawn")
