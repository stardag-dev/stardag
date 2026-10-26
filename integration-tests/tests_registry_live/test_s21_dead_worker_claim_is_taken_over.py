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

**Everything asserted is durable; nothing waits on a transient state.**
The lapse is a window -- RUNNING with a past expiry, listed runnable --
that a takeover closes, and a takeover can land within a second of it: a
tick still lingering, or holding a claim deadline, takes the claim on its
next pass. A scenario that polls for the window misses it and then waits
out its patience on a build that already completed (STA-140). So the
scenario asserts on what the window leaves behind:

* the task completed under a **second** execution, distinct from the dead
  one, whose start event names the dead execution as ``taken_over``;
* the takeover came **no earlier than the claim's expiry** -- both by the
  registry's clock: the expiry is the one the dying worker's renewal was
  answered with, recorded by the worker before it died (see ``DiesOnce``),
  and the takeover is the replacement's ``started_at``;
* the dead execution's late reports are **refused**: a start sent as it
  after the takeover answers ``execution_not_current`` and is recorded
  unapplied, and nothing it sent after the takeover was applied.

**Modal re-runs the killed input** under the same execution id, and that
cannot be turned off without the worker reporting its own death. Rather
than prevented, the restart is made inert (``DiesOnce`` exits at once on a
recorded death, without renewing), so the expiry on record is the lapse.
A restart that lands after the takeover is one more late start, and the
last assertion covers it; one that does not land at all is fine too, which
is why the scenario sends its own.

**What notices the lapse may be a watchdog sweep, and that is the design,
not the harness.** A lapse writes nothing, so no build is flagged. A tick
still alive at the lapse takes it over itself; once none is, time-based
wake-ups belong to the watchdog (design.md, "What carries over"). So
once the registry's clock is past the recorded expiry and no takeover is
on the ledger, the scenario drives sweeps of ``lapse_app``, whose builds
are this scenario's alone.

**Sweeps, plural, because the watchdog is periodic.** A sweep's tick that
finds the scheduler lease held exits without acting, on the premise that
the holder will. A *lingering* holder may not: it polls the flag, and a
lapse sets none. So the first sweep can land while the spawning tick still
lingers -- one CI run did, to the second: the sweep's tick was refused the
lease at the moment the lingering tick released it and exited, and the
build then sat RUNNING. In production the next period recovers it, so the
scenario drives the next period too, every scheduler-lease TTL (the
longest any holder can keep a sweep out) until the takeover is on the
ledger, and only then waits for the build.

The alternatives this rules out: a lapsed claim that is never taken over
(the task RUNNING forever, the build stalled), a takeover before the claim
lapsed (two live holders), a dead execution whose late report still
counts, and a takeover that rewrites the dead execution as ended (the
ledger would claim a report that never came).
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta

import pytest

from stardag_integration_tests.registry_live._events import (
    describe_events,
    describe_ledger,
    executions_of,
    ledger_and_server_now,
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

# How long the dying worker's claim outlives it. Short, so the wait for the
# lapse is short; the sweep that follows is what the wait is for.
LAPSE_SECONDS = 20
# Until the dying worker has recorded its death: a cold start, the
# upstreams, and one container.
DEATH_TIMEOUT_SECONDS = 300
# The ``Date`` header is truncated to the second, so it reads up to a second
# early; this much past the recorded expiry, the claim has lapsed.
DATE_RESOLUTION_SECONDS = 1
# Watchdog periods driven before giving up on the takeover. Often none is
# needed (a live tick takes the claim over), and a second only when the
# first lands while the spawning tick still lingers with the lease.
MAX_SWEEPS = 4
# After the takeover, only the replacement's run and the root are left.
BUILD_TIMEOUT_SECONDS = 300


@pytest.mark.budget(150)
def test_s21_a_dead_worker_lapsed_claim_is_taken_over(deployment: Deployment) -> None:
    from stardag.exceptions import APIError
    from stardag.registry import registry_provider
    from stardag_integration_tests.registry_live._deployed import (
        run_watchdog_sweep,
    )
    from stardag_integration_tests.registry_live.lapse_app import APP_NAME, app
    from stardag_integration_tests.registry_live.tasks import (
        DEATH_ARTIFACT_NAME,
        DiesOnce,
        get_sum,
    )

    salt = uuid.uuid4().hex
    dies = DiesOnce(salt=salt, lapse_seconds=LAPSE_SECONDS)
    registry = registry_provider.get()

    build_id = app.build_trigger(
        get_sum(integers=dies),
        reactive=True,
        tick_kwargs={"linger_seconds": 30, "poll_interval_seconds": 3},
    ).build_id

    def _mine(rows):
        return [r for r in rows if str(r.get("task_id")) == str(dies.id)]

    # The death on record: the execution that died, and the expiry its
    # renewal was answered with. Durable -- an artifact, not a window.
    death = wait_until(
        lambda: task_artifact(deployment, dies.id, DEATH_ARTIFACT_NAME),
        build_id=build_id,
        timeout=DEATH_TIMEOUT_SECONDS,
        poll_interval=3.0,
        what="the dying worker to record its death",
    )
    dead_id = str(death["execution_id"])
    expires_at = datetime.fromisoformat(str(death["claim_expires_at"]))

    # Until the takeover is on the ledger, or the registry's clock is past
    # the expiry -- both of which stay true once they are.
    def _taken_over_or_lapsed() -> bool:
        rows, now = ledger_and_server_now(deployment, build_id)
        lapsed = now >= expires_at + timedelta(seconds=DATE_RESOLUTION_SECONDS)
        return len(_mine(rows)) >= 2 or lapsed

    wait_until(
        _taken_over_or_lapsed,
        build_id=build_id,
        timeout=LAPSE_SECONDS + 60,
        poll_interval=2.0,
        what=f"the takeover, or the registry's clock to pass {expires_at}",
    )

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
    print(
        f"[harness] build {build_id}: takeover after {sweeps} watchdog "
        "sweep(s)" + ("; a live tick took it over" if sweeps == 0 else "")
    )

    # The dead execution reporting late, sent while the replacement holds
    # the claim (or has completed): refused, whatever Modal's restarts did.
    first = executions_of(deployment, dies.id, build_id)[0]
    assert str(first["id"]) == dead_id, describe_ledger(
        executions_of(deployment, dies.id, build_id)
    )
    with pytest.raises(APIError) as refused:
        registry.member_start(
            uuid.UUID(str(first["plan_id"])),
            str(dies.id),
            execution_id=uuid.UUID(dead_id),
            claim=False,
        )
    assert refused.value.code == "execution_not_current", (
        f"The dead execution's late start was refused for the wrong reason: "
        f"{refused.value!r}"
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
    assert str(dead["id"]) == dead_id, ledger_text
    assert str(replacement["id"]) != dead_id, ledger_text
    assert dead["claim_outcome"] == "taken_over", (
        "The dead execution's claim was not closed by the takeover.\n" + ledger_text
    )
    assert dead["ended_at"] is None and dead["outcome"] is None, (
        "The dead execution never reported its end, so the ledger must not "
        "record one.\n" + ledger_text
    )
    assert replacement["outcome"] == "completed", ledger_text

    # No earlier than the lapse, by one clock: the registry's.
    taken_over_at = datetime.fromisoformat(str(replacement["started_at"]))
    assert taken_over_at >= expires_at, (
        f"The claim was taken over at {taken_over_at}, before it lapsed at "
        f"{expires_at} (both by the registry's clock): two live holders.\n"
        + ledger_text
    )

    # The takeover's own record names the execution it took over, and
    # nothing the dead execution sent after it was applied.
    events = task_events(deployment, dies.id)
    takeover = [
        e
        for e in events
        if e.get("event_type") == "task_started"
        and str(e.get("execution_id")) == str(replacement["id"])
        and (e.get("event_metadata") or {}).get("taken_over") == dead_id
    ]
    assert len(takeover) == 1 and takeover[0]["report_applied"], (
        "Expected one applied claiming start naming the dead execution as "
        "taken over.\n" + describe_events(events, build=build_id)
    )
    late = events[events.index(takeover[0]) + 1 :]
    late = [e for e in late if str(e.get("execution_id")) == dead_id]
    assert late, (
        "The harness's late start as the dead execution left no record.\n"
        + describe_events(events, build=build_id)
    )
    assert not any(e["report_applied"] for e in late), (
        "A report from the dead execution was applied after its claim was "
        "taken over.\n" + describe_events(events, build=build_id)
    )
    print(
        f"[harness] build {build_id}: taken over "
        f"{(taken_over_at - expires_at).total_seconds():.1f}s after the lapse; "
        f"{len(late)} late report(s) from the dead execution, all refused"
    )
