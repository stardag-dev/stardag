"""Cooperative cancellation: a worker asks whether it is still wanted.

Nothing reaches into a container to stop it. A cancel marks the build and
releases its claims, and the containers that were running find out by
asking — at checkpoints their own code chooses, where stopping is safe.

That is a **pull**, and the shape matters. A push needs some process to
reconstruct "which containers are mine", which is the question a
short-lived reactive tick cannot answer about executions it did not
start, and answering it eight different ways is what produced this
module's whole backstory. Here the worker holds its own execution
identity and asks about itself; the registry answers from two columns.

**The invariant, and it runs the opposite way from the report rules.**

    A worker exits only on **positive evidence** that it is no longer
    wanted: the registry answered, and the answer said so.

Everything else keeps it running — a transport failure, an unreachable
registry, a server predating the endpoint, a registry implementation with
no opinion, a response that did not parse. The polarity follows from
which error costs more. Stopping a healthy worker destroys work that may
have been running for hours. Letting a superseded one finish writes a
content-addressed output nobody reads, which is what happened in every
release before this and is harmless by construction. The report-validity
rules in the server default the other way, because there the cheap error
is to drop a report rather than to evict a live claim; conflating the two
polarities would be the bug.

**What this cannot protect.** A task with side effects outside its target
— a row written to somebody else's database, an email sent — is not
covered, and never was. Cancellation happens at checkpoints, and anything
already done before one is done. That limit is a property of the design
rather than of this implementation.
"""

from __future__ import annotations

import logging
import os
import time
import typing
from contextlib import contextmanager
from contextvars import ContextVar

from stardag.exceptions import ExecutionCancelled

logger = logging.getLogger(__name__)

CHECK_INTERVAL_ENV = "STARDAG_CANCELLATION_CHECK_INTERVAL_SECONDS"
"""How long a cancellation answer is reused before asking again.

A generator that yields in a tight loop, and a ``run()`` body calling
:func:`cancellation_requested` inside one, would otherwise put a request
per iteration on the registry. The throttle makes the helper safe to call
anywhere, which is the point of offering it at all — a helper you have to
budget for does not get used.

Default 30 seconds. The cost of a stale answer is bounded by it: a
cancelled worker runs at most one interval longer than it had to.
"""

DEFAULT_CHECK_INTERVAL_SECONDS = 30.0


def _configured_interval() -> float:
    raw = os.environ.get(CHECK_INTERVAL_ENV)
    if not raw:
        return DEFAULT_CHECK_INTERVAL_SECONDS
    try:
        value = float(raw)
    except ValueError:
        logger.warning(f"Invalid {CHECK_INTERVAL_ENV}: {raw!r}; using the default.")
        return DEFAULT_CHECK_INTERVAL_SECONDS
    # Zero means "ask every time", which is a legitimate thing to want in
    # a test. Negative is not, and clamping beats raising in a worker.
    return max(value, 0.0)


class CancellationChecker:
    """Answers "is this execution still wanted?", throttled and fail-open.

    Constructed with an ``ask`` callable so this module knows nothing
    about registries or engines: the Modal worker passes one that calls
    ``registry.execution_status``, and a test passes a function. ``ask``
    returns True when the registry said the execution is **no longer**
    wanted, and False for every other outcome including its own failure —
    keeping the invariant's fail-open behaviour in one place, at the seam
    where the registry is actually reached.
    """

    def __init__(
        self,
        ask: typing.Callable[[], bool],
        *,
        min_interval_seconds: float | None = None,
    ):
        self._ask = ask
        self._min_interval = (
            _configured_interval()
            if min_interval_seconds is None
            else max(min_interval_seconds, 0.0)
        )
        self._answer = False
        self._asked_at: float | None = None

    def note_cancelled(self, reason: str) -> None:
        """Record an answer that arrived without being asked for.

        The worker's own start report is a non-claiming start, and the
        registry refuses one naming a superseded execution — so a 409
        there *is* this question already answered, in a request the
        worker makes anyway. Recording it here makes the first checkpoint
        free, and it is sticky: an execution that has been replaced does
        not become current again.
        """
        logger.warning(
            f"This execution is no longer the one its task is waiting for "
            f"({reason}); it will stop at its next checkpoint."
        )
        self._answer = True

    def requested(self, *, force: bool = False) -> bool:
        """Whether this execution should stop, reusing a recent answer.

        ``force`` skips the throttle. Used by the checkpoint the worker
        takes once per attempt, where there is nothing to throttle and
        the answer wants to be fresh.
        """
        if self._answer:
            # Sticky: once superseded or on a build that has stopped,
            # never un-cancelled. Nothing recovers a task from another
            # holder, and re-asking could only produce a "no" from a
            # transport failure.
            return True
        now = time.monotonic()
        if (
            not force
            and self._asked_at is not None
            and now - self._asked_at < self._min_interval
        ):
            return False
        self._asked_at = now
        self._answer = self._ask()
        return self._answer

    def raise_if_cancelled(self, checkpoint: str, *, force: bool = False) -> None:
        """:meth:`requested`, raising :class:`ExecutionCancelled` on a yes."""
        if self.requested(force=force):
            raise ExecutionCancelled(
                f"This execution is no longer the one its task is waiting "
                f"for; stopping at the {checkpoint} checkpoint without "
                f"writing output or reporting completion."
            )


_CURRENT: ContextVar[CancellationChecker | None] = ContextVar(
    "stardag_cancellation_checker", default=None
)


@contextmanager
def cancellation_scope(
    checker: CancellationChecker | None,
) -> typing.Iterator[None]:
    """Make ``checker`` the one :func:`cancellation_requested` consults.

    Installed by the worker around the user's ``run()``. A ``ContextVar``
    rather than a global because a process may drive several tasks —
    the resident engine's thread and process pools do — and each wants to
    ask about its own execution.
    """
    token = _CURRENT.set(checker)
    try:
        yield
    finally:
        _CURRENT.reset(token)


def current_checker() -> CancellationChecker | None:
    """The checker installed by :func:`cancellation_scope`, if any.

    For stardag's own automatic checkpoints, which want to *raise* rather
    than return a boolean. User code calls
    :func:`cancellation_requested` instead.
    """
    return _CURRENT.get()


def cancellation_requested() -> bool:
    """Has this task's build stopped wanting this execution?

    Call it in a long loop inside ``run()`` and stop when it says yes::

        def run(self):
            target = self.target()
            for chunk in self.chunks():
                if sd.cancellation_requested():
                    raise sd.ExecutionCancelled()
                process(chunk)
            target.write(...)

    Or return early and leave the target unwritten — anything that does
    not produce output is a clean stop. Raising
    :class:`~stardag.exceptions.ExecutionCancelled` is the tidier form
    because the worker recognises it and reports nothing for it, where an
    ordinary early ``return`` reads as "the task completed" and *is*
    reported as one.

    Two checkpoints are automatic and need no help: the start of each
    attempt, before ``run()``, and each dynamic-dependency yield. This is
    the opt-in for the case only the task's author can place — a
    long-running body with a safe point somewhere in the middle of it.
    Raising asynchronously into that body instead was considered and
    rejected: it can interrupt a write halfway, which is the one thing
    content-addressed targets exist to prevent.

    Throttled (see :data:`CHECK_INTERVAL_ENV`), so it is cheap to call in
    a loop. Returns False outside a worker, and False whenever the
    registry could not be reached — see the module docstring for why
    "stop" needs positive evidence.
    """
    checker = _CURRENT.get()
    if checker is None:
        return False
    return checker.requested()
