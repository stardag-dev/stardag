"""How the Modal runner reads what ended an execution: the platform's
interruption signals (:data:`MODAL_INTERRUPTIONS`), the one carried on an
exception's chain, and from that whether a restart is coming. Split from
:mod:`._runner`, which acts on the answer."""

from __future__ import annotations

import logging
import os

from stardag.exceptions import ResumableInterruption
from stardag.integration.modal._metadata import STARDAG_MODAL_FUNCTION_TIMEOUT_ENV

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

    **An ``InputCancellation`` anywhere on the chain decides, however near
    a ``KeyboardInterrupt`` is.** Once an input is cancelled (its timeout
    fired, or someone cancelled it) nothing restarts it, whatever arrives
    afterwards. And something often does: Modal follows the cancel with a
    grace-period SIGINT, so a checkpoint still being written ~20s after a
    timeout is cut short by a ``KeyboardInterrupt`` raised *inside* the
    handler that caught the ``InputCancellation``. That leaves
    ``ResumableInterruption -> KeyboardInterrupt -> InputCancellation``,
    and "nearest wins" would read the follow-up kill of a cancelled input
    as a preemption: report ``TASK_PREEMPTED``, keep the claim, and wait
    for a restart that is never coming (STA-129). So the walk visits the
    whole (bounded) chain, and returns the nearest ``KeyboardInterrupt``
    only when no ``InputCancellation`` is on it.
    """
    seen: set[int] = set()
    nearest_preemption: BaseException | None = None
    # Breadth-first and bounded rather than exhaustive: a chain is normally
    # one or two links, and anything pathological (a cycle, a deeply nested
    # re-raise) must not spin inside a container that is already being
    # killed.
    queue: list[BaseException] = [exception]
    for _ in range(_CHAIN_WALK_LIMIT):
        if not queue:
            break
        current = queue.pop(0)
        if id(current) in seen:
            continue
        seen.add(id(current))
        if current is not exception and isinstance(current, MODAL_INTERRUPTIONS):
            if not isinstance(current, KeyboardInterrupt):
                # The only other member is InputCancellation: decisive.
                return current
            if nearest_preemption is None:
                nearest_preemption = current
        queue.extend(
            link
            for link in (current.__cause__, current.__context__)
            if link is not None
        )
    return nearest_preemption


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

    **One clock reading still overrides the chain, in one direction.**
    Elapsed at or past the declared timeout itself is a timeout
    whatever the chain holds: the clock starts late, so it can miss a
    timeout but cannot invent one, and an input past its timeout is not
    restarted on the same call id. The reverse never holds — "before the
    timeout" by our clock proves nothing — so below it the chain decides.

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
        # Past the declared timeout nothing restarts the input, whatever
        # the chain says: the clock under-reads an input's age (see
        # ``_TIMEOUT_DETECTION_SLACK_SECONDS``), so it can miss a timeout
        # but never invent one. Compared against the timeout itself, not
        # less the slack: the slack would let a genuine preemption in the
        # last seconds before the timeout read as one, and be rescheduled
        # instead of restarted.
        if (
            function_timeout_seconds is not None
            and elapsed_seconds >= function_timeout_seconds
        ):
            return _TIMEOUT
        signal = _platform_signal(exception)
        if signal is not None:
            # A KeyboardInterrupt is returned only when no InputCancellation
            # (a timeout or a cancel) is anywhere on the chain.
            return _PREEMPTION if isinstance(signal, KeyboardInterrupt) else _TIMEOUT
        # Nothing on the chain: the clock is all there is, with the slack,
        # and with no timeout declared the backend still applies its own,
        # so report.
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
