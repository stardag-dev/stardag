"""A tick dies between its claim and its spawn: the claim is ended, not
waited out (STA-94).

A tick takes a task's claim, then spawns the worker and records its ref. A
tick that dies in between leaves an execution that holds the claim, names
no executor ref, and will never run: nothing was spawned. Left alone it
holds the task for the claim's TTL -- the worker's timeout plus 15 minutes.
Reconciliation's rule R2 (``stardag.build._reactive._reconcile``) ends such
an execution once it is older than ``unspawned_grace_seconds``, as
``lost``, conditionally on there still being no ref, and the task is
re-driven.

**The death is real, and so is the claim.** ``SpawnDiesWithTheTick``'s
first spawn kills the tick's container with ``os._exit`` right after the
claim (the reconcile app's ``faults``), having recorded which execution it
claimed as an artifact first. Nothing about the claim is synthesised: the
TTL on it is the real one, which is why the scenario can assert the
recovery came long before it.

**Everything asserted is durable.** On the ledger:

* the dead execution -- the one the death artifact names -- ended with
  outcome ``lost`` and ``claim_outcome = cancelled``, never recorded a ref,
  and was ended no sooner than the grace after its start and well before
  its TTL, both by the registry's clock;
* a second execution of the task started after that end, recorded a ref,
  and completed;
* the build completed.

**What wakes a tick to reconcile is a watchdog sweep, by design.** The dead
tick held the scheduler lease and writes nothing as it dies, so no build
is flagged. Modal restarts the killed tick input, but that restart is
refused the lease until the dead holder's lapses, and a refused tick
spawns no successor while the lease reads live. Time-based wake-ups are
the watchdog's (design.md, "What carries over"), so the scenario drives
sweeps of ``reconcile_app``, whose builds are these scenarios' alone, one
per scheduler-lease TTL until the re-drive is on the ledger. A sweep's
tick reconciles on its first pass (a fresh tick's reconciler is due at
once). Should a restarted tick get the lease first, its own pass -- or its
linger's exit pass -- reconciles instead, and no sweep is needed.

The alternatives this rules out: a dead claim held until its TTL (the
build stalled for ~25 minutes), an end before the grace (a slow spawn
mistaken for a dead one), a re-drive that re-uses the dead execution, and
an end recorded as anything that would read as the task having run.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime

import pytest

from stardag_integration_tests.registry_live._events import (
    describe_events,
    describe_ledger,
    executions_of,
    task_artifact,
    task_events,
)
from stardag_integration_tests.registry_live._guard import registry_live_guard
from stardag_integration_tests.registry_live._harness import Deployment
from stardag_integration_tests.registry_live._wait import (
    describe,
    wait_for_terminal,
    wait_until,
)

registry_live_guard()

pytestmark = [
    pytest.mark.registry_live,
    pytest.mark.timeout(900),
]

# Short, so the dead claim is reconcilable seconds after the death; long
# enough that the tick's own spawn of the *re-driven* execution -- one Modal
# call between its claim and its ref -- is never mistaken for a dead one.
UNSPAWNED_GRACE_SECONDS = 15
RECONCILE_INTERVAL_SECONDS = 5
# Until the tick has recorded its death: a bootstrap, one tick's cold start
# and its first pass.
DEATH_TIMEOUT_SECONDS = 300
# Watchdog periods driven before giving up on the re-drive. One is usually
# refused (the dead tick's lease is still live); the next reconciles.
MAX_SWEEPS = 4
# The dead execution must be ended well inside this, by the registry's
# clock: a few lease TTLs, against a claim TTL of 25 minutes.
RECOVERY_BOUND_SECONDS = 600
# After the re-drive: the re-run leaf and the root.
BUILD_TIMEOUT_SECONDS = 300


@pytest.mark.budget(170)
def test_a_claim_whose_tick_died_before_the_spawn_is_ended_and_re_driven(
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
        TICK_DEATH_ARTIFACT_NAME,
        SpawnDiesWithTheTick,
        get_sum,
    )

    claim_ttl_seconds = WORKER_TIMEOUT_SECONDS + CLAIM_TTL_GRACE_SECONDS
    assert RECOVERY_BOUND_SECONDS < claim_ttl_seconds
    leaf = SpawnDiesWithTheTick(salt=uuid.uuid4().hex)

    build_id = app.build_trigger(
        get_sum(integers=leaf),
        reactive=True,
        tick_kwargs={
            "linger_seconds": 30,
            "poll_interval_seconds": 3,
            "reconcile_interval_seconds": RECONCILE_INTERVAL_SECONDS,
            "unspawned_grace_seconds": UNSPAWNED_GRACE_SECONDS,
        },
    ).build_id

    # The death on record: the execution the tick claimed and never spawned.
    death = wait_until(
        lambda: task_artifact(deployment, leaf.id, TICK_DEATH_ARTIFACT_NAME),
        build_id=build_id,
        timeout=DEATH_TIMEOUT_SECONDS,
        poll_interval=3.0,
        what="the tick to record its death between the claim and the spawn",
    )
    dead_id = str(death["execution_id"])

    def _re_driven() -> bool:
        return len(executions_of(deployment, leaf.id, build_id)) >= 2

    # A first window without a sweep: a restarted tick that holds the lease
    # reconciles on its own. Then one sweep per lease TTL, the longest the
    # dead holder can keep a sweep's tick out.
    sweeps = 0
    deadline = time.monotonic() + _LEASE_TTL_SECONDS
    while not _re_driven():
        if time.monotonic() < deadline:
            time.sleep(5)
            continue
        assert sweeps < MAX_SWEEPS, (
            f"{sweeps} watchdog sweep(s), one every {_LEASE_TTL_SECONDS}s, and "
            "the claim the dead tick took was never ended and re-driven.\n"
            f"{describe(build_id)}\n"
            f"{describe_ledger(executions_of(deployment, leaf.id, build_id))}"
        )
        run_watchdog_sweep(
            app_name=APP_NAME, modal_environment=deployment.modal_environment
        )
        sweeps += 1
        deadline = time.monotonic() + _LEASE_TTL_SECONDS
    print(
        f"[harness] build {build_id}: re-driven after {sweeps} watchdog "
        "sweep(s)" + ("; a restarted tick reconciled it" if sweeps == 0 else "")
    )

    status = wait_for_terminal(build_id, timeout=BUILD_TIMEOUT_SECONDS)
    rows = executions_of(deployment, leaf.id, build_id)
    ledger_text = describe_ledger(rows)
    assert status == "completed", (
        "The build did not complete after its tick died between a claim and "
        f"its spawn.\n{describe(build_id)}\n{ledger_text}"
    )
    assert len(rows) == 2, (
        f"Expected the dead execution and one re-driven execution.\n{ledger_text}"
    )
    dead, replacement = rows
    assert str(dead["id"]) == dead_id, ledger_text

    assert dead["executor_ref"] is None, (
        "The dead execution recorded a ref, so something did spawn it.\n" + ledger_text
    )
    assert dead["outcome"] == "lost" and dead["claim_outcome"] == "cancelled", (
        "The unspawned execution should be ended `lost`, releasing its claim "
        f"as `cancelled`.\n{ledger_text}"
    )
    assert replacement["outcome"] == "completed", ledger_text
    assert replacement["executor_ref"] is not None, ledger_text

    # No sooner than the grace, and well before the TTL: the registry's clock.
    started_at = datetime.fromisoformat(str(dead["started_at"]))
    ended_at = datetime.fromisoformat(str(dead["ended_at"]))
    held = (ended_at - started_at).total_seconds()
    assert held >= UNSPAWNED_GRACE_SECONDS, (
        f"The unspawned claim was ended {held:.1f}s after it was taken, inside "
        f"the {UNSPAWNED_GRACE_SECONDS}s grace.\n{ledger_text}"
    )
    assert held < RECOVERY_BOUND_SECONDS, (
        f"The unspawned claim was held {held:.0f}s before it was ended; its "
        f"TTL is {claim_ttl_seconds:.0f}s, and reconciliation should end it "
        f"within {RECOVERY_BOUND_SECONDS}s.\n{ledger_text}"
    )
    re_driven_at = datetime.fromisoformat(str(replacement["started_at"]))
    assert re_driven_at >= ended_at, (
        f"The replacement started at {re_driven_at}, before the dead "
        f"execution was ended at {ended_at}: two live holders.\n{ledger_text}"
    )

    # The end on record is the conditional operator end, applied.
    events = task_events(deployment, leaf.id)
    ends = [
        e
        for e in events
        if e.get("event_type") == "task_cancelled"
        and str(e.get("execution_id")) == dead_id
        and (e.get("event_metadata") or {}).get("outcome") == "lost"
    ]
    assert len(ends) == 1 and ends[0]["report_applied"], (
        "Expected one applied `lost` end of the dead execution.\n"
        + describe_events(events, build=build_id)
    )
    print(
        f"[harness] build {build_id}: the dead claim was ended {held:.1f}s "
        f"after it was taken (grace {UNSPAWNED_GRACE_SECONDS}s, TTL "
        f"{claim_ttl_seconds:.0f}s)"
    )
