"""S21: a worker that dies without reporting loses its claim to a takeover.

Liveness is the claim: RUNNING plus an expiry, and RUNNING with a past
expiry is ACTIONABLE (design.md, "The runnable rule"). So a worker killed
without a word -- an OOM, a segfault, a host lost -- holds the task only
until its claim lapses; then the next claiming start takes it over, writes
``claim_outcome = taken_over`` on the dead execution's ledger row, and runs
the task afresh. The dead execution's ``ended_at`` stays NULL: no report of
it ending ever arrived, which is exactly what ``builds stop`` lists.

``DiesOnce`` is that worker. Its first execution renews its own claim down
to a few seconds and exits the container with ``os._exit`` -- no reporter,
no failure, no ``finally``. The one synthesised part is the short TTL (see
the task's docstring for why a real detached TTL is too long to wait out);
the lapse, the takeover and the second execution are the registry's own.

**What notices the lapse is a watchdog sweep, and that is the design, not
the harness.** A lapse writes nothing, so no build is flagged, and a
lingering tick polls the flag rather than the frontier: time-based wake-ups
belong to the watchdog (design.md, "What carries over"). The scenario waits
until the registry itself lists the member as runnable -- RUNNING with a
lapsed claim, the ACTIONABLE rule's own judgement -- and then drives
sweeps of ``lapse_app``, whose builds are this scenario's alone.

**Sweeps, plural, because the watchdog is periodic.** A sweep's tick that
finds the scheduler lease held exits without acting, on the premise that
the holder will. A *lingering* holder will not: it polls the flag, and a
lapse sets none. The tick that spawned ``DiesOnce`` lingers
``linger_seconds`` after its last spawn while the claim lapses
``LAPSE_SECONDS`` after it, so the first sweep can land inside that window
-- and one CI run did, to the second: the sweep's tick was refused the
lease at the moment the lingering tick released it and exited, and the
build then sat RUNNING for the rest of the wait. In production the next
period recovers it, so the scenario drives the next period too, every
scheduler-lease TTL (the longest any holder can keep a sweep out) until
the takeover is on the ledger, and only then waits for the build.

The alternatives this rules out: a lapsed claim that is never taken over
(the task RUNNING forever, the build stalled), and a takeover that rewrites
the dead execution as ended (the ledger would claim a report that never
came).
"""

from __future__ import annotations

import time
import uuid

import pytest

from stardag_integration_tests.registry_live._events import (
    describe_ledger,
    executions_of,
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

# How long the dying worker's claim outlives it. Short, so the wait for the
# lapse is short; the sweep that follows is what the wait is for.
LAPSE_SECONDS = 20
LAPSE_TIMEOUT_SECONDS = 300
# Watchdog periods driven before giving up on the takeover. The first sweep
# normally suffices; a second is needed only when the first lands while the
# spawning tick still lingers with the lease.
MAX_SWEEPS = 4
# After the takeover, only the replacement's run and the root are left.
BUILD_TIMEOUT_SECONDS = 300


@pytest.mark.budget(150)
def test_s21_a_dead_worker_lapsed_claim_is_taken_over(deployment: Deployment) -> None:
    from stardag.registry import registry_provider
    from stardag_integration_tests.registry_live._deployed import (
        run_watchdog_sweep,
    )
    from stardag_integration_tests.registry_live.lapse_app import APP_NAME, app
    from stardag_integration_tests.registry_live.tasks import DiesOnce, get_sum

    salt = uuid.uuid4().hex
    dies = DiesOnce(salt=salt, lapse_seconds=LAPSE_SECONDS)
    registry = registry_provider.get()

    build_id = app.build_trigger(
        get_sum(integers=dies),
        reactive=True,
        tick_kwargs={"linger_seconds": 30, "poll_interval_seconds": 3},
    ).build_id

    def _lapsed() -> bool:
        frontier = registry.build_get_frontier(build_id)
        return any(
            m.task_id == str(dies.id) and m.status == "running"
            for m in frontier.runnable
        )

    wait_until(
        _lapsed,
        build_id=build_id,
        timeout=LAPSE_TIMEOUT_SECONDS,
        what="the dead worker's claim to lapse (RUNNING and listed runnable)",
    )
    first = executions_of(deployment, dies.id, build_id)
    assert len(first) == 1 and first[0]["ended_at"] is None, describe_ledger(first)

    # Sized from the lease, not guessed: a sweep's tick is kept out only by a
    # live scheduler lease, and no holder keeps one past its TTL without
    # renewing it -- which a lingering tick stops doing when it exits.
    from stardag.build._reactive._lease import _LEASE_TTL_SECONDS

    def _taken_over() -> bool:
        return len(executions_of(deployment, dies.id, build_id)) >= 2

    sweeps = 0
    while not _taken_over():
        assert sweeps < MAX_SWEEPS, (
            f"{sweeps} watchdog sweep(s), one every {_LEASE_TTL_SECONDS}s, "
            "and the lapsed claim was never taken over.\n"
            f"{describe(build_id)}\n"
            f"{describe_ledger(executions_of(deployment, dies.id, build_id))}"
        )
        run_watchdog_sweep(
            app_name=APP_NAME, modal_environment=deployment.modal_environment
        )
        sweeps += 1
        deadline = time.monotonic() + _LEASE_TTL_SECONDS
        while time.monotonic() < deadline and not _taken_over():
            time.sleep(5)
    if sweeps > 1:
        print(
            f"[harness] build {build_id}: takeover after {sweeps} watchdog "
            "sweeps; the earlier one(s) found the lease held"
        )

    status = wait_for_terminal(build_id, timeout=BUILD_TIMEOUT_SECONDS)
    rows = executions_of(deployment, dies.id, build_id)
    ledger_text = describe_ledger(rows)
    assert status == "completed", (
        "The build did not complete after its worker died. The lapsed claim "
        "should have been taken over and the task run again.\n"
        f"{describe(build_id)}\n{ledger_text}"
    )

    assert len(rows) == 2, (
        f"Expected the dead execution and its replacement.\n{ledger_text}"
    )
    dead, replacement = rows
    assert dead["id"] == first[0]["id"], ledger_text
    assert dead["claim_outcome"] == "taken_over", (
        "The dead execution's claim was not closed by the takeover.\n" + ledger_text
    )
    assert dead["ended_at"] is None and dead["outcome"] is None, (
        "The dead execution never reported its end, so the ledger must not "
        "record one.\n" + ledger_text
    )
    assert replacement["outcome"] == "completed", ledger_text
