from __future__ import annotations

import time
import typing

Decision = typing.Literal["no_window", "opened", "holding", "expired"]
"""What a probe-observed death should do, this pass.

``no_window`` — act now, there is no window to wait for. ``opened`` /
``holding`` — leave it, the worker may still be reporting. ``expired`` —
the window closed with nothing reported; act now.
"""


class _ReportWindow:
    """Executions a probe found gone, held open for the worker's report.

    **The rule.** A probe answers one question — is this execution still
    running on the backend? — and "no" is not a classification. One event
    (the platform ended the input) means different things depending on
    what the task did with it: a task that caught the interruption and
    checkpointed is *interrupted* and is resumed on its own budget, while
    a task that did not is *failed* and spends an attempt. Only the worker
    knows which, and it says so in the grace window the platform gives it
    before the kill.

    So the worker is the authority on how its own execution ended, and the
    tick's probe is the fallback for when no report comes. This holds the
    fallback back for ``grace_seconds`` after the first pass that finds
    the execution gone, so a report already in flight is not pre-empted by
    a failure the scheduler invented — which spends an attempt the task
    never asked to spend, and leaves the worker's report to be refused as
    a statement about an execution the task no longer holds (STA-65).

    **Why a clock and not a state.** Everything else a tick waits on is a
    state it can read back: a claim, a ref that probes live, a status.
    "Did the worker speak?" is one of those too — the task simply stops
    being RUNNING under that ref, and the next pass sees it — but its
    *absence* is not, and a window that never closes is a build that never
    heals. The clock bounds the wait; the state ends it early, every time
    it happens.

    **Scoped to one tick**, deliberately. The window is only useful to a
    scheduler that will still be here when it closes, so a tick that
    cannot wait (``linger_seconds <= 0`` — the watchdog sweep) is
    constructed with ``enabled=False`` and acts immediately, exactly as
    before. A sweep arrives long after the event anyway: if the worker
    were going to report, it would have.

    Keyed by task id and validated against the ref, so a task whose next
    execution also dies opens a fresh window rather than inheriting the
    closed one.
    """

    def __init__(
        self,
        grace_seconds: float,
        *,
        enabled: bool = True,
        clock: "typing.Callable[[], float]" = time.monotonic,
    ) -> None:
        self._grace = grace_seconds
        self._enabled = enabled and grace_seconds > 0
        self._clock = clock
        # task id -> (executor ref, when this pass first found it gone)
        self._open: dict[str, tuple[str, float]] = {}

    def observe(self, task_id: str, ref: str) -> Decision:
        """Record that ``ref`` probed dead for ``task_id``, and decide.

        Called once per task per pass, from the probe phase's coroutines.
        Safe without a lock for the same reason the summary counters are:
        the read and the write are on one event loop with no ``await``
        between them.
        """
        if not self._enabled:
            return "no_window"
        now = self._clock()
        seen = self._open.get(task_id)
        if seen is None or seen[0] != ref:
            self._open[task_id] = (ref, now)
            return "opened"
        if now - seen[1] >= self._grace:
            del self._open[task_id]
            return "expired"
        return "holding"

    def retain(self, task_ids: "typing.AbstractSet[str]") -> None:
        """Drop the windows this pass did not re-observe as dead.

        A task that left RUNNING — the worker reported, or another build
        completed it — is no longer this window's business, and leaving
        its entry behind would hold the tick past its linger for a
        decision nobody is waiting on.
        """
        for task_id in [tid for tid in self._open if tid not in task_ids]:
            del self._open[task_id]

    def seconds_until_due(self) -> float | None:
        """How long until the earliest open window closes, or None.

        The tick adds this to its linger deadline: it must not exit owing
        a decision, or the execution stays RUNNING until the claim lapses.
        """
        if not self._open:
            return None
        now = self._clock()
        return max(
            0.0, min(seen + self._grace - now for _, seen in self._open.values())
        )

    def due(self) -> bool:
        """Whether any open window has closed — i.e. one more pass is owed."""
        now = self._clock()
        return any(now - seen >= self._grace for _, seen in self._open.values())
