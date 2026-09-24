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
lapsed claim, the ACTIONABLE rule's own judgement -- and then drives one
sweep of ``lapse_app``, whose builds are this scenario's alone.

The alternatives this rules out: a lapsed claim that is never taken over
(the task RUNNING forever, the build stalled), and a takeover that rewrites
the dead execution as ended (the ledger would claim a report that never
came).
"""

from __future__ import annotations

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
BUILD_TIMEOUT_SECONDS = 600


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

    run_watchdog_sweep(
        app_name=APP_NAME, modal_environment=deployment.modal_environment
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
