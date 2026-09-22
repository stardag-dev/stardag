"""Cancelling a build touches its neighbours' executions not at all.

The first half of a production incident, kept; the second half deleted
with its cause (STA-81).

**What is still tested here.** A cancelled build was ticked again --
neighbours kept flagging it -- and it cancelled every RUNNING task in its
*plan*. After plan closure that includes tasks a second build had claimed
and was executing, so it killed their containers and released their
claims. The second build recorded failures, retried, and was killed again
on the next tick; the loop never converged. The route now refuses a cancel
from a build that does not hold the task, and this scenario is what proves
it under real concurrency:

1. A runs a slow shared task. Cancel A, releasing its claims.
2. B is triggered, resets the cancelled task and runs it.
3. A is ticked again, deliberately, while B's copy is running.

B's execution must be untouched and B must finish. Against the code this
fixed, step 3 kills B's container and B never completes.

**What was removed, and why it is not a gap.** This scenario used to
assert a second thing: that A's *own* container was gone, stopped by A's
tick. That was the other defect -- a cascade released the claims while the
containers ran on -- and the fix was a cancel drain in the tick, which
STA-78 has now withdrawn along with every other attempt to stop a
container from a scheduler. Nothing automatic stops A's container any
more; the two replacements are a worker that stops *itself* at a
cooperative checkpoint (covered by
``test_execution_identity.test_a_cancelled_tasks_worker_stops_itself``,
which cancels the build and watches the worker exit) and ``stardag builds
stop`` for a hard stop (``test_builds_stop.py``). The task used here
sleeps with no checkpoint of its own, so it is the wrong instrument for
either -- asserting anything about its container now would be asserting
that the drain still exists.

The first half is the half that was a *claim* bug, and claims are what
this file is about.
"""

from __future__ import annotations

import uuid

import pytest

from stardag_integration_tests.registry_live._guard import registry_live_guard
from stardag_integration_tests.registry_live._wait import (
    describe,
    find_task,
    task_status,
    tick_summaries,
    wait_for_task_status,
    wait_for_terminal,
    wait_until,
)

registry_live_guard()

pytestmark = [
    pytest.mark.registry_live,
    pytest.mark.timeout(900),
]

# Long enough that the shared task is still RUNNING when A is cancelled and
# when B is ticked, and short enough that B's own copy finishes inside the
# scenario. It does NOT have to outlive B's takeover: if A's container
# survives its cancel it completes the task on its own schedule, and that
# is precisely what the completion-owner assertion catches.
SHARED_SLEEP_SECONDS = 60

# A only has to get the shared task started before it is cancelled.
A_LINGER_SECONDS = 30
# B has to see a 60s task through a container start, so it stays resident.
B_LINGER_SECONDS = 180

STATUS_TIMEOUT_SECONDS = 300
TICK_TIMEOUT_SECONDS = 180
BUILD_TIMEOUT_SECONDS = 600


def _owner(task_id: str):
    """The build whose event produced the task's current status."""
    return find_task(task_id, task_name="Slow").latest_status_build_id


def test_a_cancelled_build_stops_its_own_executions_and_no_others() -> None:
    from stardag.integration.modal._spawn import spawn_tick
    from stardag.registry import registry_provider
    from stardag_integration_tests.registry_live.dag_app import APP_NAME, app
    from stardag_integration_tests.registry_live.tasks import (
        get_range,
        get_sum,
        slow,
        square,
    )

    salt = uuid.uuid4().hex
    leaf = get_range(limit=4, salt=salt)
    shared = slow(values=leaf, seconds=SHARED_SLEEP_SECONDS)
    shared_id = str(shared.id)

    build_a = app.build_trigger(
        get_sum(integers=shared),
        reactive=True,
        tick_kwargs={"linger_seconds": A_LINGER_SECONDS, "poll_interval_seconds": 3},
    ).build_id

    wait_for_task_status(
        shared.id,
        expected="running",
        build_id=build_a,
        timeout=STATUS_TIMEOUT_SECONDS,
    )
    assert _owner(shared_id) == build_a, describe(build_a)

    # The cascade releases the claim. Stopping the container it belonged to
    # is not something the server can do -- it can only record that the
    # claim is gone -- so from here the execution is A's engine's to stop,
    # and a tick is the only thing that will.
    registry = registry_provider.get()
    cancelled = registry.build_cancel(build_a, cascade=True)
    assert cancelled is not None
    assert shared_id in cancelled.cascaded_task_ids, (
        "The cascade did not release the shared task's claim, so the rest "
        f"of this scenario cannot happen.\n{describe(build_a)}"
    )

    # B inherits the cancelled task: a revocation is not a result, so B
    # resets it and runs it. Reaching RUNNING under B is what proves the
    # hand-over happened at all.
    build_b = app.build_trigger(
        square(values=shared, offset=11),
        reactive=True,
        tick_kwargs={"linger_seconds": B_LINGER_SECONDS, "poll_interval_seconds": 3},
    ).build_id
    # Wait for the *owner* to be B, not merely for the task to be RUNNING.
    # A's own worker is still starting up around now and self-reports a
    # TASK_STARTED of its own, which can land after the cascade and put the
    # task back to RUNNING under A -- a real behaviour (the server cannot
    # stop anything, so a live worker keeps talking), and one that makes a
    # status-only wait return on the wrong build's execution under load.
    wait_until(
        lambda: task_status(shared.id) == "running" and _owner(shared_id) == build_b,
        build_id=build_b,
        timeout=STATUS_TIMEOUT_SECONDS,
        what=f"build {build_b} to claim the shared task",
    )

    # Tick A again, while B's copy is running. In production this arrived
    # on its own -- a neighbour's drain hands a flagged build out, and A's
    # own workers kept re-flagging it -- but waiting for that would be
    # waiting on a race. Spawning it directly puts the interleaving under
    # test rather than hoping for it.
    ticks_before = len(tick_summaries(build_a))
    spawn_tick(build_a, APP_NAME)
    wait_until(
        lambda: len(tick_summaries(build_a)) > ticks_before,
        build_id=build_a,
        timeout=TICK_TIMEOUT_SECONDS,
        what=f"build {build_a} to report a tick after being cancelled",
    )

    status = task_status(shared.id)
    assert status in ("running", "completed"), (
        "A cancelled build ticked and revoked a task it does not hold. The "
        "execution belonged to another build, which is now running a task "
        "the registry has declared dead.\n"
        f"--- build A (cancelled) ---\n{describe(build_a)}\n"
        f"--- build B ---\n{describe(build_b)}"
    )
    assert _owner(shared_id) == build_b, (
        "The shared task's status is no longer B's doing, so the cancelled "
        "build rewrote it.\n" + describe(build_a)
    )

    status_b = wait_for_terminal(build_b, timeout=BUILD_TIMEOUT_SECONDS)
    assert status_b == "completed", (
        "Build B did not finish. Its executions were being cancelled out "
        "from under it by a build that no longer holds them.\n"
        f"--- build A (cancelled) ---\n{describe(build_a)}\n"
        f"--- build B ---\n{describe(build_b)}"
    )
