"""A worker's call finishes without reporting its end: the end is
reconciled, not waited out (STA-135).

A worker reports its own end -- completion, failure, yield. When that
report is lost (a rate limit outlasting its retries, an outage) the call
has finished but the ledger still lists the execution running, holding the
task's claim for its TTL: the worker's timeout plus 15 minutes.
Reconciliation's rule R1 (``stardag.build._reactive._reconcile``) probes
the call; one that *returned* with its target complete has the completion
reported for that same execution, as its worker would have.

**The run is real; only the report is withheld.** ``EndGoesUnreported``'s
worker runs the task and writes its output; the reconcile app's
``faults`` replace that worker's completion report with an artifact naming
its execution, and the call returns normally. So the probe sees a call
that returned, and the target is there.

**Everything asserted is durable.** The task has exactly one execution on
the ledger -- the one the worker's artifact names, so the worker that ran
it is the one that withheld its report -- and that execution completed,
with its end no later than the claim's TTL by a wide margin (registry
clock); no second execution ran the task; the build completed.

**What wakes a tick to reconcile.** A withheld end report writes no flag,
and the worker's wake-up of the scheduler is part of the report, so it is
withheld too. The tick that spawned the task is lingering, and its exit
pass at the linger deadline is a pass: if the call has returned by then, it
reconciles there. If not -- a slow cold start outlasting the linger -- that
tick lingers out and nothing else is coming, so the scenario falls back to
the watchdog, as ``test_reconcile_unspawned_claim`` does: one sweep of
``reconcile_app`` per scheduler-lease TTL until the execution has ended.

The alternatives this rules out: an unreported end held until its TTL, a
returned call whose task is re-run rather than completed (a duplicate of
work already done), and a completion recorded against any execution but
the one that ran.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime

import pytest

from stardag_integration_tests.registry_live._events import (
    describe_ledger,
    executions_of,
    task_artifact,
)
from stardag_integration_tests.registry_live._guard import registry_live_guard
from stardag_integration_tests.registry_live._harness import Deployment
from stardag_integration_tests.registry_live._wait import (
    describe,
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
# Long enough that the spawning tick's exit pass usually finds the call
# returned (a warm worker runs this leaf in seconds); the sweeps cover a
# cold start that outlasts it.
LINGER_SECONDS = 45
# Until the worker has run the task and withheld its report: a bootstrap,
# a tick, and one worker's cold start.
RUN_TIMEOUT_SECONDS = 300
MAX_SWEEPS = 4
# The execution must be ended well inside this, by the registry's clock,
# against a claim TTL of 25 minutes.
RECOVERY_BOUND_SECONDS = 600
BUILD_TIMEOUT_SECONDS = 300


@pytest.mark.budget(130)
def test_a_call_that_returned_without_reporting_its_end_is_completed(
    deployment: Deployment,
) -> None:
    from stardag.build._claims import CLAIM_TTL_GRACE_SECONDS
    from stardag.build._reactive._lease import _LEASE_TTL_SECONDS
    from stardag_integration_tests.registry_live._deployed import (
        run_watchdog_sweep,
    )
    from stardag_integration_tests.registry_live._scenario_app import (
        WORKER_TIMEOUT_SECONDS,
    )
    from stardag_integration_tests.registry_live.reconcile_app import APP_NAME, app
    from stardag_integration_tests.registry_live.tasks import (
        UNREPORTED_END_ARTIFACT_NAME,
        EndGoesUnreported,
        get_sum,
    )

    claim_ttl_seconds = WORKER_TIMEOUT_SECONDS + CLAIM_TTL_GRACE_SECONDS
    assert RECOVERY_BOUND_SECONDS < claim_ttl_seconds
    leaf = EndGoesUnreported(salt=uuid.uuid4().hex)

    build_id = app.build_trigger(
        get_sum(integers=leaf),
        reactive=True,
        tick_kwargs={
            "linger_seconds": LINGER_SECONDS,
            "poll_interval_seconds": 3,
            "reconcile_interval_seconds": RECONCILE_INTERVAL_SECONDS,
        },
    ).build_id

    # The run on record: the execution whose worker withheld its end.
    withheld = wait_until(
        lambda: task_artifact(deployment, leaf.id, UNREPORTED_END_ARTIFACT_NAME),
        build_id=build_id,
        timeout=RUN_TIMEOUT_SECONDS,
        poll_interval=3.0,
        what="the worker to run the task and withhold its end report",
    )
    ran_id = str(withheld["execution_id"])

    def _ended() -> bool:
        return any(
            r.get("ended_at") is not None
            for r in executions_of(deployment, leaf.id, build_id)
        )

    # The spawning tick's exit pass first (it is due within the linger),
    # then one sweep per lease TTL.
    sweeps = 0
    deadline = time.monotonic() + LINGER_SECONDS + _LEASE_TTL_SECONDS
    while not _ended():
        if time.monotonic() < deadline:
            time.sleep(5)
            continue
        assert sweeps < MAX_SWEEPS, (
            f"{sweeps} watchdog sweep(s), one every {_LEASE_TTL_SECONDS}s, and "
            "the execution whose end went unreported was never ended.\n"
            f"{describe(build_id)}\n"
            f"{describe_ledger(executions_of(deployment, leaf.id, build_id))}"
        )
        run_watchdog_sweep(
            app_name=APP_NAME, modal_environment=deployment.modal_environment
        )
        sweeps += 1
        deadline = time.monotonic() + _LEASE_TTL_SECONDS
    print(
        f"[harness] build {build_id}: reconciled after {sweeps} watchdog "
        "sweep(s)" + ("; the spawning tick's own pass did it" if sweeps == 0 else "")
    )

    status = wait_for_terminal(build_id, timeout=BUILD_TIMEOUT_SECONDS)
    rows = executions_of(deployment, leaf.id, build_id)
    ledger_text = describe_ledger(rows)
    assert status == "completed", (
        "The build did not complete after a worker's end report was lost.\n"
        f"{describe(build_id)}\n{ledger_text}"
    )
    assert len(rows) == 1, (
        "The call had returned with its output written; the task should be "
        f"completed under that execution, not run again.\n{ledger_text}"
    )
    (only,) = rows
    assert str(only["id"]) == ran_id, (
        f"The worker that withheld its report ran execution {ran_id}.\n" + ledger_text
    )
    assert only["outcome"] == "completed", (
        "The returned call's execution should be completed by "
        f"reconciliation, its target being complete.\n{ledger_text}"
    )
    assert only["executor_ref"] is not None, ledger_text

    started_at = datetime.fromisoformat(str(only["started_at"]))
    ended_at = datetime.fromisoformat(str(only["ended_at"]))
    held = (ended_at - started_at).total_seconds()
    assert held < RECOVERY_BOUND_SECONDS, (
        f"The execution was held {held:.0f}s before its end was reconciled; "
        f"its TTL is {claim_ttl_seconds:.0f}s, and reconciliation should end "
        f"it within {RECOVERY_BOUND_SECONDS}s.\n{ledger_text}"
    )

    # Diagnostic only: the trail may be short its last tick (see
    # ``wait_for_terminal``), so a count read from it is a lower bound.
    reconciled = sum(int(s.get("reconciled") or 0) for s in tick_summaries(build_id))
    print(
        f"[harness] build {build_id}: completed by reconciliation {held:.1f}s "
        f"after the claim (TTL {claim_ttl_seconds:.0f}s); ticks report "
        f"{reconciled} reconciled"
    )
