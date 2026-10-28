"""A failed attempt that Modal retries is still running to a probe: a
reconciling tick neither ends it nor re-drives it (STA-146).

Reconciliation's rule R1 (``stardag.build._reactive._reconcile``) ends an
execution whose call has *finished*, read off
``ModalTaskExecutor.probe_detached`` -- ``FunctionCall.get(timeout=0)``. A
worker function with Modal ``retries`` set is retried under the same call,
so R1 is only sound if a spawned call exposes its *final* outcome and never
a failed attempt's while the next attempt runs. Were it to, a probe would
read ``"raised"`` mid-retry and report the failure for an execution whose
retry is running; the build would then re-drive the task while the retry
runs it too -- a duplicate execution. The Modal client cannot settle this
(for a spawned call, retries are the server's), so this scenario does.

**The shape.** ``FailsOnceUnreported`` runs on the reconcile app's
``retrying`` worker (``retries=1``). Its first attempt raises with its
failure report withheld by the app's ``faults`` -- the report a rate limit
outlasting its retries would lose, and the only reason a failed attempt's
execution is still running on the ledger at all: a *reported* failure ends
the execution itself, releasing its claim, and Modal's retry then finds its
execution over at its start report. Its retry, under the same call and
execution, holds on a gate.

**From the failure on,** the scenario probes the call itself, with the
very method a tick uses, and requires ``"running"`` on every read: through
the gap before Modal's retry (the app stretches Modal's retry delay to 30s
so that gap can be probed at all, and several reads must land well after
the attempt has raised), and while the retry holds. On every read it also
requires the ledger to show that one execution, unended, with no second
one.

**The tick probes too.** The spawning tick is lingering with a
``reconcile_interval_seconds`` of 5, but a lingering tick passes over its
build only when the build is flagged, and nothing flags it while the call
is retried. So the scenario flags it, every ``NOTIFY_INTERVAL_SECONDS``
(longer than the reconcile interval, so each pass is due to reconcile)
for as long as it probes, and afterwards requires the ticks to have made
at least that many passes and to have reconciled nothing. Then the gate
opens, and the retry must complete the build under the execution it
started with -- no failure recorded against it and no replacement
execution, ever.

The alternatives this rules out: a failed attempt read as the call's end
(``"raised"`` mid-retry), a reconciled failure ending a running execution,
and the task re-driven while its retry runs.
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

import pytest

from stardag_integration_tests.registry_live._events import (
    describe_ledger,
    executions_of,
    task_artifact,
)
from stardag_integration_tests.registry_live._gates import GateSet
from stardag_integration_tests.registry_live._guard import registry_live_guard
from stardag_integration_tests.registry_live._harness import Deployment
from stardag_integration_tests.registry_live._wait import (
    describe,
    require_complete_trail,
    tick_summaries,
    wait_for_terminal,
    wait_until,
)

registry_live_guard()

pytestmark = [
    pytest.mark.registry_live,
    pytest.mark.timeout(900),
]

RECONCILE_INTERVAL_SECONDS = 5
# The spawning tick should still be lingering, and reconciling, while the
# retry holds: a bootstrap, a cold start, a failed attempt, Modal's retry
# delay and the hold fit well inside it. (The tick's lifetime cap, 70% of
# its timeout, bounds it anyway.)
LINGER_SECONDS = 200
# Until the first attempt has failed: a bootstrap, a tick, a cold start.
FAIL_TIMEOUT_SECONDS = 300
# From the failure until the retry has started: the app's retry delay plus
# a container start, with a wide margin.
RETRY_TIMEOUT_SECONDS = 180
# Probes of the gap between the attempts the scenario requires: with the
# app's retry delay (``reconcile_app.RETRY_DELAY_SECONDS``) and this
# interval, several land in it even when the failure is noticed late.
MIN_GAP_PROBES = 3
# A gap read counts towards that only this long after the scenario saw the
# first attempt's record. The record is written before the attempt raises,
# so seeing it means only that the raise is imminent; the margin keeps a
# read of the attempt still running out of the gap's count. The gap's other
# end is the retry's own recorded start, not when the scenario noticed it.
GAP_PROBE_AFTER_SECONDS = 3.0
# How long the retry is held while the call is probed: several of the
# lingering tick's reconcile rounds, and at least this many probes of the
# scenario's own.
HOLD_SECONDS = 6 * RECONCILE_INTERVAL_SECONDS
MIN_RETRY_PROBES = 8
PROBE_INTERVAL_SECONDS = 1.0
# How often the scenario flags the build while it probes, so the lingering
# tick passes, and reconciles, on each (see the module docstring).
NOTIFY_INTERVAL_SECONDS = RECONCILE_INTERVAL_SECONDS + 1
# How long the call may take to read ``"returned"`` once the build is
# terminal: the worker reports its completion before it uploads artifacts
# and wakes the scheduler, so the call can still be running briefly.
RETURN_TIMEOUT_SECONDS = 60
# The retry's own bound on the hold, reached only if the release is lost.
GATE_BOUND_SECONDS = 240
BUILD_TIMEOUT_SECONDS = 300


def _probe(executor_ref: str) -> str:
    """What a reconciling tick's probe reads for the call now."""
    from stardag.integration.modal._executor import ModalTaskExecutor
    from stardag.integration.modal._metadata import MODAL_EXECUTOR_NAME
    from stardag_integration_tests.registry_live.reconcile_app import APP_NAME
    from stardag_integration_tests.registry_live.selectors import (
        registry_live_worker,
    )

    executor = ModalTaskExecutor(
        modal_app_name=APP_NAME, worker_selector=registry_live_worker
    )
    return asyncio.run(executor.probe_detached(MODAL_EXECUTOR_NAME, executor_ref))


class _Prober:
    """Probes the call once a ``PROBE_INTERVAL_SECONDS``, on its own thread.

    Each read is stamped with the wall clock, so the scenario can place it
    against the attempts' own recorded times (``FailsOnceUnreported``). A
    probe that raises is recorded as such and ends the probing: it is a
    fault of the probe, which the scenario reports rather than reads as a
    state.
    """

    def __init__(self, executor_ref: str) -> None:
        self.executor_ref = executor_ref
        self.reads: list[tuple[float, str]] = []
        self.slowest = 0.0
        self.error: BaseException | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=60)

    def _run(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                state = _probe(self.executor_ref)
            except BaseException as error:  # reported by the scenario
                self.error = error
                return
            self.slowest = max(self.slowest, time.monotonic() - started)
            self.reads.append((time.time(), state))
            if state != "running":
                return
            self._stop.wait(PROBE_INTERVAL_SECONDS)

    def count_since(self, at: float) -> int:
        return sum(1 for when, _ in list(self.reads) if when >= at)

    def between(self, start: float, end: float) -> list[str]:
        return [state for when, state in list(self.reads) if start <= when < end]

    def raise_if_not_running(self, context: Callable[[], str]) -> None:
        if self.error is not None:
            raise AssertionError(
                f"Probing call {self.executor_ref!r} raised: {self.error!r}\n"
                + context()
            ) from self.error
        bad = [(when, state) for when, state in list(self.reads) if state != "running"]
        if bad:
            when, state = bad[0]
            raise AssertionError(
                f"Probe of call {self.executor_ref!r} read {state!r} at "
                f"{when:.1f}; a call Modal is retrying must read 'running' "
                f"(all reads {[s for _, s in self.reads]}). A reconciling tick "
                "would act on this -- unless the retry itself failed, which "
                "its container log shows.\n" + context()
            )


def _context(build_id: Any, ledger_text: Callable[[], str]) -> str:
    return f"{describe(build_id)}\n{ledger_text()}"


@pytest.mark.budget(180)
def test_a_retried_attempt_is_not_reconciled_as_failed(
    deployment: Deployment, gates: GateSet
) -> None:
    from stardag_integration_tests.registry_live.reconcile_app import app
    from stardag_integration_tests.registry_live.reconcile_tasks import (
        FAILED_ATTEMPT_ARTIFACT_NAME,
        RETRY_ATTEMPT_ARTIFACT_NAME,
        FailsOnceUnreported,
    )
    from stardag_integration_tests.registry_live.tasks import get_sum

    salt = uuid.uuid4().hex
    held = gates.new("retry", salt=salt)
    leaf = FailsOnceUnreported(salt=salt, seconds=GATE_BOUND_SECONDS, gate=held.key)

    build_id = app.build_trigger(
        get_sum(integers=leaf),
        reactive=True,
        tick_kwargs={
            "linger_seconds": LINGER_SECONDS,
            "poll_interval_seconds": 3,
            "reconcile_interval_seconds": RECONCILE_INTERVAL_SECONDS,
        },
    ).build_id

    def _ledger_text() -> str:
        return describe_ledger(executions_of(deployment, leaf.id, build_id))

    # The first attempt is about to raise: it records itself first.
    failed = wait_until(
        lambda: task_artifact(deployment, leaf.id, FAILED_ATTEMPT_ARTIFACT_NAME),
        build_id=build_id,
        timeout=FAIL_TIMEOUT_SECONDS,
        poll_interval=1.0,
        what="the first attempt to run and fail",
    )
    execution_id = str(failed["execution_id"])
    (row,) = executions_of(deployment, leaf.id, build_id)
    assert str(row["id"]) == execution_id, _ledger_text()
    executor_ref = row["executor_ref"]
    assert executor_ref is not None, _ledger_text()

    # Probe the call, and read the ledger, from the failure on: through the
    # gap before Modal's retry, and for as long as the retry holds. The
    # probes run on their own thread, at their own cadence: the loop below
    # reads the registry, and one slow read there (a 10s read timeout and
    # its retry) used to stall the probing with it, so a loaded run could
    # land only two probes in a 20s gap (STA-168).
    from stardag_integration_tests.registry_live.reconcile_app import (
        RETRY_DELAY_SECONDS,
    )

    from stardag.registry import registry_provider

    registry = registry_provider.get()
    # The first attempt's record is visible, so its upload has returned and
    # it raises next: a safe, if late, lower bound on the failure.
    noticed_failure_at = time.time()
    probes = _Prober(executor_ref)
    probes.start()
    notifies = 0
    last_notify: float | None = None
    retry_started_at: float | None = None
    retry_seen_at = 0.0
    failed_at = time.monotonic()
    try:
        while True:
            now = time.monotonic()
            probes.raise_if_not_running(lambda: _context(build_id, _ledger_text))
            if retry_started_at is None:
                retry = task_artifact(deployment, leaf.id, RETRY_ATTEMPT_ARTIFACT_NAME)
                if retry is not None:
                    assert str(retry["execution_id"]) == execution_id, (
                        "Modal's retry should run under the failed attempt's "
                        f"call, and so its execution.\n{_ledger_text()}"
                    )
                    retry_started_at = float(retry["at"])
                    retry_seen_at = now
                else:
                    assert now - failed_at < RETRY_TIMEOUT_SECONDS, (
                        f"Modal did not retry the failed attempt within "
                        f"{RETRY_TIMEOUT_SECONDS}s.\n{describe(build_id)}\n"
                        f"{_ledger_text()}"
                    )
            elif (
                probes.count_since(retry_started_at) >= MIN_RETRY_PROBES
                and now - retry_seen_at >= HOLD_SECONDS
            ):
                break
            if last_notify is None or now - last_notify >= NOTIFY_INTERVAL_SECONDS:
                # Flag only: the lingering tick is the one meant to pass.
                registry.build_notify(build_id, can_spawn=False)
                notifies += 1
                last_notify = now
            rows = executions_of(deployment, leaf.id, build_id)
            assert len(rows) == 1 and rows[0].get("ended_at") is None, (
                "While Modal retries the call, its execution must be the only "
                f"one and must not be ended.\n{describe(build_id)}\n"
                f"{_ledger_text()}"
            )
            time.sleep(PROBE_INTERVAL_SECONDS)
    finally:
        probes.stop()
    probes.raise_if_not_running(lambda: _context(build_id, _ledger_text))
    assert retry_started_at is not None
    assert retry_started_at - float(failed["at"]) >= RETRY_DELAY_SECONDS / 2, (
        f"The retry started {retry_started_at - float(failed['at']):.0f}s after "
        f"the failure, against Modal's {RETRY_DELAY_SECONDS:.0f}s retry delay: "
        "this was not the gap the scenario means to probe."
    )
    gap_from = noticed_failure_at + GAP_PROBE_AFTER_SECONDS
    gap_reads = probes.between(gap_from, retry_started_at)
    retry_reads = probes.between(retry_started_at, float("inf"))
    assert len(gap_reads) >= MIN_GAP_PROBES, (
        f"Only {len(gap_reads)} probe(s) landed between the failed attempt and "
        f"its retry ({retry_started_at - gap_from:.1f}s from "
        f"{GAP_PROBE_AFTER_SECONDS:.0f}s after the failure was seen to the "
        f"retry starting; {len(probes.reads)} probes in all, "
        f"{probes.slowest:.1f}s the slowest); the scenario needs "
        f"{MIN_GAP_PROBES} to say anything about that gap. Every read was "
        "'running', so nothing failed -- the gap was too short to probe: "
        "raise reconcile_app.RETRY_DELAY_SECONDS."
    )
    print(
        f"[harness] build {build_id}: {len(gap_reads)} probes in the gap "
        f"before the retry ({retry_started_at - gap_from:.1f}s of it probed) "
        f"and {len(retry_reads)} during it, all 'running'; the build flagged "
        f"{notifies} times"
    )

    held.release()
    status = wait_for_terminal(build_id, timeout=BUILD_TIMEOUT_SECONDS)
    rows = executions_of(deployment, leaf.id, build_id)
    ledger_text = describe_ledger(rows)
    assert status == "completed", (
        "The retry was released and should have completed the build.\n"
        f"{describe(build_id)}\n{ledger_text}"
    )
    assert len(rows) == 1, (
        "The task's call was retried by Modal, under one execution; it "
        f"should never have been re-driven.\n{ledger_text}"
    )
    (only,) = rows
    assert str(only["id"]) == execution_id, ledger_text
    assert only["outcome"] == "completed", (
        "The retry completed the task; no failure may be recorded against "
        f"its execution.\n{ledger_text}"
    )
    assert only["executor_ref"] == executor_ref, ledger_text
    wait_until(
        lambda: _probe(executor_ref) == "returned",
        build_id=build_id,
        timeout=RETURN_TIMEOUT_SECONDS,
        poll_interval=2.0,
        what="the retried call to read 'returned'",
    )

    # Read off the tick trail, which may be short its last tick: a count
    # it cannot answer is skipped, not guessed.
    require_complete_trail(build_id, what="the ticks' passes and reconciliations")
    summaries = tick_summaries(build_id)
    passes = sum(int(s.get("iterations") or 0) for s in summaries)
    reconciled = sum(int(s.get("reconciled") or 0) for s in summaries)
    print(
        f"[harness] build {build_id}: ticks "
        f"{[(s.get('outcome'), s.get('iterations')) for s in summaries]}; "
        f"{passes} passes, {reconciled} reconciled"
    )
    assert passes >= notifies, (
        f"The build was flagged {notifies} times while the call was probed, "
        f"{NOTIFY_INTERVAL_SECONDS}s apart, but its ticks made only {passes} "
        "passes: no tick was reconciling through the retry.\n"
        f"{describe(build_id)}\n{ledger_text}"
    )
    assert reconciled == 0, (
        "No execution of this build was dead; a tick reported reconciling "
        f"{reconciled}.\n{describe(build_id)}\n{ledger_text}"
    )
