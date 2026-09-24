"""Default ``RunFunction`` implementation — what runs inside a worker container.

:class:`Runner` executes one task (sync, async, or dynamic-deps generator) and,
when an orchestrator forwarded a build, a plan and an execution, reports that
execution's lifecycle to the registry from inside the worker via
:class:`~._reporter._WorkerLifecycleReporter`. Reporting from here rather than
from the orchestrator is what makes the events independent of the
orchestrator's lifetime — and it is the only option under reactive
scheduling, where there is no resident orchestrator at all.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import time
import typing

from stardag import BaseTask, TaskStruct, flatten_task_struct
from stardag._core.base_task import _has_custom_run, _has_custom_run_aio
from stardag.cancellation import (
    cancellation_scope,
    current_checker as _current_cancellation_checker,
)
from stardag.exceptions import ExecutionCancelled, ResumableInterruption
from stardag.integration.modal._logging import _setup_logging
from stardag.integration.modal._metadata import STARDAG_MODAL_FUNCTION_TIMEOUT_ENV
from stardag.integration.modal._protocols import RunFunction
from stardag.integration.modal._reporter import _WorkerLifecycleReporter
from stardag.utils.env import temp_env_vars

# Modal's own "this input was cancelled" BaseException, imported so that a
# client that predates or renames it degrades to "we cannot recognise a
# cancellation" instead of failing every worker import.
#
# Import the name out of the submodule rather than reaching for
# ``modal.exception`` off a bare ``import modal``. The attribute form works
# on every modal release we support, but only as a side effect: ``exception``
# is not in modal's ``__all__``, and modal's package ``__getattr__`` raises
# for anything it does not export — it resolves purely because modal's own
# ``__init__`` imports the submodule early, which binds it on the package.
# Depending on that is depending on a private detail of modal's import
# graph. The from-import never consults the parent attribute at all.
try:
    from modal.exception import InputCancellation as _InputCancellationImpl

    _InputCancellation: type[BaseException] | None = _InputCancellationImpl
except ImportError:  # pragma: no cover - depends on the installed modal
    _InputCancellation = None

MODAL_INTERRUPTIONS: tuple[type[BaseException], ...] = tuple(
    e for e in (KeyboardInterrupt, _InputCancellation) if e is not None
)
"""The exceptions Modal ends an execution with when the platform, not the
task, decided — catch these to checkpoint.

``KeyboardInterrupt`` is the preemption signal (Modal reclaiming the
container); ``modal.exception.InputCancellation`` is what a function
**timeout** raises — and also what an explicit ``FunctionCall.cancel()``
raises.

That the two ``InputCancellation`` cases are indistinguishable does not
matter to the worker: what it needs to know is whether the backend will
restart the input, and only a preemption does. So the *type* answers it,
and stardag reads it off the chain of whatever the task raises — see
:func:`_platform_signal`. Which is also why the recipe below keeps
working with ``from None``: that clears ``__cause__``, not ``__context__``.

Provided as a tuple so a task can catch both without importing from
``modal.exception`` itself, and **so it is easy to be specific**::

    from stardag.integration.modal import MODAL_INTERRUPTIONS

    try:
        train(...)
    except MODAL_INTERRUPTIONS:
        save_checkpoint(...)
        raise sd.ResumableInterruption("checkpointed") from None

Never substitute ``except BaseException``. Both members are
``BaseException`` subclasses precisely so an ordinary ``except Exception``
cannot swallow them — but a blanket catch sweeps up real bugs too (a
``NameError`` is a ``BaseException``), and re-raising
``ResumableInterruption`` for one of those turns a deterministic failure
into a task that resumes until its budget runs out.

Note this is Modal-specific by design and lives here rather than in the
core: it is the set *this backend* uses, and another backend would signal
differently.
"""

logger = logging.getLogger(__name__)

# Slack for the elapsed-time fallback in ``_classify_interruption`` — read
# only when the exception chain says nothing, which it does exactly when a
# task raises ``ResumableInterruption`` for its own reasons rather than in
# response to a platform signal.
#
# **It is a fallback because a clock cannot answer this question.** Our
# ``time.monotonic()`` starts inside ``Runner.__call__``, after container
# startup, image load and input deserialisation, all of which are inside
# the backend's window and outside ours — so elapsed systematically
# *under*-reads the input's true age, by however long the container took
# to get going. There is no tolerance that bounds that: startup on a large
# image is tens of seconds. A 86400s worker measured 86392.0s here and
# read as "before the timeout", which is how a timed-out task came to be
# treated as a preemption and left RUNNING for a day.
#
# A second term used to be argued to offset it: elapsed is read after the
# task's own ``except`` block has run, and that block writes a checkpoint,
# which overstates. Measured live against a 15s worker: 17.1s, 17.2s,
# 17.7s. But that only holds while the write is slow — in the incident
# above it failed fast (~1.5s, expired storage credentials) and the
# overstatement it was calibrated against simply was not there.
#
# The value is kept small on purpose. Where it is still consulted, both
# failure directions are benign: the task asked to be resumed, and the
# only question is whether the worker reports now or a later tick
# discovers the dead execution.
_TIMEOUT_DETECTION_SLACK_SECONDS = 5.0

# How many links of an exception chain ``_platform_signal`` will visit.
# A chain is normally one link; this bounds the pathological cases (a
# cycle, a deeply nested re-raise) inside a container that is being killed.
_CHAIN_WALK_LIMIT = 20

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


def _platform_signal(exception: BaseException) -> BaseException | None:
    """The platform interruption this exception was raised in response to.

    Walks ``__cause__`` then ``__context__`` looking for one of the
    exceptions the backend ends an execution with. The documented
    checkpoint recipe is::

        except MODAL_INTERRUPTIONS:
            save_checkpoint(...)
            raise sd.ResumableInterruption("checkpointed") from None

    and ``raise ... from None`` clears ``__cause__`` and suppresses the
    *display* of the chain — it does not clear ``__context__``. So the
    original signal survives both that form and plain re-raising, and it
    says exactly what :func:`_classify_interruption` needs to know.

    ``None`` when the chain holds nothing: a task that raised
    ``ResumableInterruption`` on its own initiative rather than in response
    to a signal (a self-imposed time budget, a spot-price check) — a
    legitimate thing to do, and why the elapsed-time fallback still exists.

    **The set is exactly** :data:`MODAL_INTERRUPTIONS`, the pair the
    platform actually ends an execution with, and nothing wider. A
    ``SystemExit`` is not one of them: read as a preemption it would have
    the runner translate the request back into an interrupt, the backend
    restart the input, the task exit the same way, and the loop repeat —
    ungated by ``retries``, because a backend restart spends no attempt.
    An unrecognised exception on the chain falls through to the clock,
    which is bounded.

    **Both links are followed, not one.** ``__cause__`` and ``__context__``
    are not two names for the same chain: an exception can carry both at
    once, with an explicit cause of its own while the platform signal is
    reachable only through the implicit context. ``raise ... from error``
    inside an ``except MODAL_INTERRUPTIONS`` block produces exactly that,
    and following only ``__cause__`` would walk away from the answer.
    """
    seen: set[int] = set()
    # Breadth-first, so the *nearest* signal wins when a chain forks —
    # and bounded rather than exhaustive: a chain is normally one link, and
    # anything pathological (a cycle, a deeply nested re-raise) must not
    # spin inside a container that is already being killed.
    queue: list[BaseException] = [exception]
    for _ in range(_CHAIN_WALK_LIMIT):
        if not queue:
            return None
        current = queue.pop(0)
        if id(current) in seen:
            continue
        seen.add(id(current))
        if current is not exception and isinstance(current, MODAL_INTERRUPTIONS):
            return current
        queue.extend(
            link
            for link in (current.__cause__, current.__context__)
            if link is not None
        )
    return None


def _classify_interruption(
    exception: BaseException,
    *,
    elapsed_seconds: float,
    function_timeout_seconds: float | None,
) -> str | None:
    """What ended this execution, and therefore who recovers it.

    **The task decides whether it is resumable; the exception it was
    raised from decides who resumes it.** Two questions, in that order:

    1. *Did the task ask to be resumed?* Only ``ResumableInterruption``
       says yes. An interruption the task let propagate is not a request —
       it means the task had no plan for being interrupted, so either it
       hung or the worker's timeout is too small for the work. Both want
       the same answer, and it is not "run it twenty more times": it is a
       failure under the ordinary attempt budget. There is deliberately no
       configuration overriding this, because the task already answered by
       raising or not raising.
    2. *Is anything already going to restart it?* Only a preemption is.
       An escaping ``BaseException`` reads to Modal as a crashed container
       and the input is restarted on the same call id in a few seconds —
       better than a scheduler respawn on every axis (the claim is kept,
       no attempt is spent, no round-trip). Once the function timeout has
       fired nothing is coming (verified live: returning cleanly,
       re-raising an interrupt and raising an ordinary exception all
       resolve to ``FunctionTimeoutError`` with no restart), so a registry
       event is the only way back.

    **Question 2 is answered by the exception, not by a clock.** Modal
    delivers a preemption as ``KeyboardInterrupt`` and delivers both a
    function timeout and an explicit ``FunctionCall.cancel()`` as
    ``InputCancellation`` — and only the first restarts. So the type
    already separates "a restart is coming" from "nothing is coming",
    exactly, and :func:`_platform_signal` recovers it from the chain even
    through the ``from None`` the docs recommend.

    This used to be inferred from ``elapsed >= timeout - slack``, which is
    a strictly *harder* question (timeout vs cancel) answered on a clock
    that cannot measure what it needs — see
    ``_TIMEOUT_DETECTION_SLACK_SECONDS``. And timeout-vs-cancel never
    needed separating here at all: both mean "nothing will restart this",
    so both report, and whether the report *applies* is the registry's to
    decide — it is the one that issued any cancel, and it knows whose
    claim the task is under. Neither is knowable from inside the container.

    Hence the return values:

    - ``_TIMEOUT`` — the task asked to be resumed and no restart is
      coming. Reports ``TASK_INTERRUPTED``, which releases the claim so a
      scheduler can start the task again.
    - ``_PREEMPTION`` — the task asked to be resumed and the backend will
      restart it. Reports ``TASK_PREEMPTED``, which changes no status and
      keeps the claim the restart is about to need, then gets out of the
      way.
    - ``_CANCELLATION`` — a raw platform interruption. Report nothing: on
      a preemption the backend restarts it, and on a timeout the execution
      dies and a later scheduler pass records the failure, which is exactly
      what should happen to a task that did not plan for this.
    - ``None`` — an ordinary exception. A failure, reported as one.

    **The fallback, and why it leans to reporting.** With nothing in the
    chain the clock is all there is, and the asymmetry decides it:
    guessing *preemption* when no restart is coming leaves the task
    RUNNING until its claim lapses, the exact stall this path exists to
    remove; guessing *timeout* is recoverable either way, because if a
    restart does arrive the scheduler's probe finds the ref still live and
    leaves it alone. The same reasoning covers a worker with no declared
    timeout, where the backend still applies its own default (Modal's is
    300s), so "not declared" does not mean "none fired". Both unknowns
    report.
    """
    if isinstance(exception, ResumableInterruption):
        signal = _platform_signal(exception)
        if signal is not None:
            # KeyboardInterrupt is the preemption; the only other member of
            # the set is InputCancellation, which is a timeout or a cancel.
            return _PREEMPTION if isinstance(signal, KeyboardInterrupt) else _TIMEOUT
        timed_out = function_timeout_seconds is None or (
            elapsed_seconds
            >= function_timeout_seconds - _TIMEOUT_DETECTION_SLACK_SECONDS
        )
        return _TIMEOUT if timed_out else _PREEMPTION
    if isinstance(exception, (KeyboardInterrupt, SystemExit)):
        return _CANCELLATION
    if _InputCancellation is not None and isinstance(exception, _InputCancellation):
        return _CANCELLATION
    return None


def _checkpoint_at_yield() -> None:
    """The dynamic-dependency checkpoint, read off the ambient scope.

    Placed here rather than threaded through :meth:`Runner.run` on
    purpose: ``run`` is an override point, and a new parameter on it would
    break every subclass that defines one — the same trade that keeps the
    identity off ``TaskExecutorABC.submit``. The context variable is set
    around ``run()`` by :meth:`Runner.__call__`, so the drivers can ask
    without anybody passing anything.

    A subclass that drives its own generator instead of using these
    simply does not get this checkpoint. It still gets the
    start-of-attempt one and ``stardag.cancellation_requested()``.
    """
    checker = _current_cancellation_checker()
    if checker is not None:
        checker.raise_if_cancelled("dynamic-dependency yield")


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
        """Optional setup logic before the task runs.

        Per *task*, so it runs again for every input a worker container
        serves. Setup that need only happen once per container belongs in
        ``StardagApp(container_setup=...)`` instead — it runs before this.
        """
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
        # Started here, not just before ``run()``: this is compared against
        # the *function's* timeout, and the backend's clock started earlier
        # still (container startup and input deserialisation are inside its
        # window and outside ours). Everything between here and ``run()``
        # — a user ``setup()`` that loads a model, the start report's HTTP
        # call — would otherwise be time the comparison does not see, and
        # understating elapsed time makes a real timeout read as a
        # preemption.
        started_at = time.monotonic()
        try:
            self.setup(task)
            # All lifecycle reporting happens inside the env-overrides
            # context — the selector's env, the build's settings and the
            # framework's identifiers — so a yield walks the children under
            # the build's settings exactly as run() sees them. (Caveat:
            # stardag's config/registry providers cache on first access —
            # registry connection settings come from the container's process
            # environment, i.e. deployment secrets, not overrides.)
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
                    # Checkpoint one: is this execution still the one the
                    # task is waiting for?
                    #
                    # **It costs one registry read per attempt.** The
                    # start report answers half the question for free —
                    # a start naming a superseded execution is refused
                    # and the reporter records that — but a *successful*
                    # start says nothing about whether the build is still
                    # running, which is the commoner way to stop being
                    # wanted. So on the ordinary path this asks. Dropping
                    # ``force`` would not save it either: the checker is
                    # built per attempt, so it has never asked and the
                    # throttle has nothing to reuse.
                    #
                    # Raised from here, outside the try below, so the
                    # end-of-attempt classifier never sees it: a
                    # cancelled execution reports nothing. Teardown still
                    # runs.
                    reporter.cancellation.raise_if_cancelled(
                        "start of attempt", force=True
                    )
                function_timeout = _declared_function_timeout(env_overrides)
                try:
                    # The scope the generator drivers and the user-facing
                    # ``stardag.cancellation_requested()`` read from. None
                    # when nothing reports lifecycle, which disables the
                    # checkpoints rather than breaking them.
                    with cancellation_scope(
                        reporter.cancellation if reporter is not None else None
                    ):
                        result = self.run(task)
                except ExecutionCancelled:
                    # A clean stop, not an end of attempt: no output was
                    # written and nothing is reported -- not a failure,
                    # not an interruption, not a completion. The build
                    # that wanted this task either does not exist any more
                    # or is running it somewhere else, and either way the
                    # honest record is the one already there.
                    #
                    # Re-raised rather than returned. Returning normally
                    # makes the backend call *succeed*, and a scheduler
                    # probing a succeeded call whose target is missing
                    # reads it as "the worker wrote it, eventual
                    # consistency" and records a completion for output
                    # that does not exist. An ordinary Exception leaving
                    # the container is a failed call, which is both true
                    # and recoverable -- and in the cases that get here,
                    # nothing is left probing it anyway.
                    logger.warning(
                        f"Execution of task {task.id} stopped at a "
                        "cooperative cancellation checkpoint; no output "
                        "written and no completion reported."
                    )
                    raise
                except BaseException as e:
                    kind = self._report_end_of_attempt(
                        task,
                        e,
                        reporter=reporter,
                        elapsed_seconds=time.monotonic() - started_at,
                        function_timeout_seconds=function_timeout,
                    )
                    # _PREEMPTION is returned only for
                    # ResumableInterruption, always an ordinary Exception —
                    # the isinstance is a guard so a future classification
                    # change cannot silently start substituting a
                    # KeyboardInterrupt for some other BaseException.
                    if kind == _PREEMPTION and isinstance(e, Exception):
                        # The task raised ``sd.ResumableInterruption`` — an
                        # ordinary Exception, deliberately, so it does not
                        # slip past the user's own error handling. But an
                        # ordinary exception leaving the container is a
                        # *task failure* to the execution backend, which
                        # will not restart the input for it. A BaseException
                        # escaping reads as a crashed container, which it
                        # will. Translating here is what makes
                        # "raise sd.ResumableInterruption" mean what it says.
                        raise KeyboardInterrupt(f"Task asked to be resumed: {e}") from e
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

        **What each outcome writes, and why:**

        - **The task asked to be resumed and no restart is coming** (a
          function timeout, or a cancel). Reports ``TASK_INTERRUPTED``,
          which releases the claim so a scheduler can start the task
          again — the only case where nothing else can recover it, since
          after a timeout fires the call is dead whatever the container
          does next. A *cancel* reports too: the worker cannot tell the
          two apart, and the registry — which issued the cancel — refuses
          the transition on a task it has already cancelled, so guessing
          here would only get it wrong in the other direction.
        - **The task asked to be resumed and the backend will restart it**
          (a preemption). Reports ``TASK_PREEMPTED``, which changes no
          status and keeps the claim: the exception is on its way out, an
          escaping ``BaseException`` reads as a crashed container, and
          Modal restarts the same call id in a few seconds. A *terminal*
          event here would replace that with a slower scheduler respawn
          and release a claim the restart is about to need — but recording
          the preemption costs neither. What it buys is that the restart
          is now expected by somebody: without it, a restart that never
          arrives is indistinguishable from a task running happily, and
          the build waits out the whole claim.
        - **A raw platform interruption** the task did not catch. Records
          nothing. It had no plan for being interrupted, so the right end
          state is a failure under the ordinary attempt budget — which is
          exactly what happens when the worker says nothing and the
          execution dies.
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
            # Three situations reach here, and the message must claim only
            # what its evidence supports — naming a timeout that was never
            # observed is an assertion the worker is in no position to
            # make, and it is what sent the last investigation down the
            # wrong path.
            signal = _platform_signal(exception)
            if signal is not None:
                # Known from the exception: the platform ended it, and no
                # restart is coming. Whether that was the function timeout
                # or a deliberate cancel is not knowable here, and does not
                # need to be — see ``_classify_interruption``.
                detail = (
                    f"after the platform ended its execution "
                    f"({type(signal).__name__}, {elapsed_seconds:.1f}s in)"
                )
            elif function_timeout_seconds is not None:
                detail = (
                    f"{elapsed_seconds:.1f}s in, which is near or past its "
                    f"worker function's {function_timeout_seconds}s timeout"
                )
            else:
                detail = (
                    f"{elapsed_seconds:.1f}s in; the worker function "
                    "declares no timeout, so whether one fired is unknown "
                    "and the interruption is reported to be safe"
                )
            logger.warning(
                f"Task {task.id} checkpointed and asked to be resumed "
                f"{detail}. Reporting an interruption: nothing restarts an "
                "input the platform has finished with, so a scheduler tick "
                "has to."
            )
            reporter.interrupted(f"Task checkpointed and asked to be resumed {detail}")
            return kind
        if kind == _CANCELLATION:
            # A raw platform interruption the task did not catch, or a
            # deliberate cancel. Recording nothing is right for both: a
            # cancel is already owned by whoever issued it, and an uncaught
            # interruption means the task had no plan for one — so it should
            # end up a failure under the ordinary attempt budget, which is
            # what happens when the execution dies unreported.
            logger.info(
                f"Execution of task {task.id} was interrupted or cancelled "
                f"after {elapsed_seconds:.1f}s without the task asking to be "
                "resumed. Recording nothing; if this task is meant to "
                "survive interruptions, catch MODAL_INTERRUPTIONS and raise "
                "stardag.ResumableInterruption."
            )
            return kind
        detail = (
            f"after being preempted {elapsed_seconds:.1f}s in; the "
            "execution backend restarts this input itself"
        )
        logger.warning(
            f"Task {task.id} checkpointed and asked to be resumed {detail}. "
            "Letting the interrupt propagate so that restart happens — "
            "faster than a reschedule, and it keeps the execution claim. "
            "Recording the preemption, which releases nothing: it is what "
            "makes a restart that never arrives visible."
        )
        reporter.preempted(f"Task checkpointed and asked to be resumed {detail}")
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
            # One cooperative checkpoint per yield, *before* acting on
            # what was yielded: a cancelled build should not pay for the
            # completeness checks below, and above all should not reach
            # the caller's ``suspended()``, which registers these deps as
            # children and spawns them. Throttled, so a generator that
            # fast-forwards over many complete batches makes at most one
            # registry call per interval.
            _checkpoint_at_yield()
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
        # See the sync driver: one checkpoint per yield, before the
        # completeness checks and before anything registers children.
        _checkpoint_at_yield()
        deps = flatten_task_struct(yielded)
        incomplete = [dep for dep in deps if not dep.complete()]
        if incomplete:
            return tuple(deps)
    return None


_default_run = Runner()
