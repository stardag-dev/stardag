"""A task suspended on its dynamic children must be waited on, not reset.

The hardest case in the cross-build family, because the task under
contention is holding nothing. A worker that registers dynamic children,
yields and returns leaves the task SUSPENDED -- and a suspended task has
**no execution claim**. There is no lease to inspect, no expiry to compare
a clock against. The only thing that says somebody is still progressing it
is the liveness of the build that owns it.

So a second build arriving at that task is being asked to distinguish
"abandoned, mine to take" from "mid-flight, leave it alone" with the
evidence deliberately removed. Getting it wrong is expensive rather than
merely wrong: resetting the task discards every bit of pre-yield work and
races the owning build's own scheduling of the children it just registered.

Note which duration bounds that window, because getting it backwards has
killed runs. It is the **worker timeout**, not the claim TTL -- the TTL is
derived from the timeout plus a grace, so it is strictly the looser of the
two, and a fixture sized against the TTL would be guaranteed to outlive its
own execution budget.

The decisive observable is in the event log, and only there. Status columns
are one row per task overwritten by whoever wrote last, so a build that
reset this task and then watched the other build complete it leaves no mark
on them at all: the reset happened, and the columns show a completion.
``task_retried`` attributed to B is the thing that either exists or does
not.

**B is resident here, and deliberately.** Every other cross-build scenario
makes the second build dormant, because what they test is the wake-up
reaching it. This one is not about who calls the tick -- it is about what a
tick *decides* when it looks at a blocker it does not own. Keeping B's
scheduler alive across the whole window isolates that decision from the
delivery machinery, which is tested on its own next door.

It is also the difference between a scenario that works and one that works
most of the time. A dormant B has to be woken twice here -- once when the
task suspends, once when it completes -- and the server hands a flagged
build out at most once per window (120s), so the second flag can land
inside the first hand-out's window and reach nobody. In the full
concurrent tier another scenario's tick drains it once the window lapses
and B finishes anyway; run alone, nothing drains and B waits forever. That
is the watchdog's job, and this tier deliberately runs none.
"""

from __future__ import annotations

import time
import uuid

import pytest

from stardag_integration_tests.registry_live._events import (
    describe_events,
    resets_by,
    task_events,
    wait_until_registered,
)
from stardag_integration_tests.registry_live._guard import registry_live_guard
from stardag_integration_tests.registry_live._harness import Deployment
from stardag_integration_tests.registry_live._wait import (
    assert_trail_complete,
    describe,
    task_status,
    tick_summaries,
    wait_for_task_status,
    wait_for_terminal,
)

registry_live_guard()

pytestmark = [
    pytest.mark.registry_live,
    pytest.mark.timeout(900),
]

# How long the shared task stays RUNNING before it yields. This is the
# window B has to *register* in, and it is sized against a cold Modal
# container for B's bootstrap rather than against anything in the
# scheduling under test.
#
# Registering any later would not fail -- it would quietly test something
# else. Plan closure follows the dynamic edges once they are written, so a
# late B pulls the children into its own plan, has running tasks of its
# own, is never stalled, and never classifies a blocker at all. Correct
# behaviour, and not this scenario. So the run *checks* that the ordering
# held rather than trusting this number, and says to raise it if it did
# not.
PRE_YIELD_SECONDS = 45

# The SUSPENDED window: how long the children run, and so how long B has
# to be ticked in. B is flagged the moment the task suspends, so this only
# has to cover one wake-up and one cold container -- not the whole of B.
#
# It cannot go to nothing, though, and the reason is what separates this
# scenario from the cross-build one. B's *bootstrap* tick already meets the
# task RUNNING and waits, which is enough to satisfy the counters on its
# own. What makes this a test of the SUSPENDED case specifically is a tick
# of B's landing inside this window.
CHILD_SECONDS = 35

# B stays resident across the whole window -- see the module docstring.
# This costs nothing in wall clock: a tick exits as soon as its build goes
# terminal, so the linger is an upper bound that is never reached.
B_LINGER_SECONDS = 300

STATUS_TIMEOUT_SECONDS = 300
BUILD_TIMEOUT_SECONDS = 600


def test_a_suspended_blocker_is_waited_on_rather_than_reset(
    deployment: Deployment,
) -> None:
    from stardag_integration_tests.registry_live.dag_app import app
    from stardag_integration_tests.registry_live.tasks import (
        SuspendingParent,
        get_sum,
        square,
    )

    salt = uuid.uuid4().hex

    shared = SuspendingParent(
        salt=salt,
        children=2,
        child_seconds=CHILD_SECONDS,
        pre_yield_seconds=PRE_YIELD_SECONDS,
    )

    build_a = app.build_trigger(
        get_sum(integers=shared),
        reactive=True,
        # A lingers long enough to see its own work through, so the only
        # build whose progress is in question is B.
        tick_kwargs={"linger_seconds": 240, "poll_interval_seconds": 3},
    ).build_id

    # Wait for the state B has to arrive in, rather than sleeping a guess
    # at how long it takes to get there.
    wait_for_task_status(
        shared.id,
        expected="running",
        build_id=build_a,
        timeout=STATUS_TIMEOUT_SECONDS,
    )
    running_at = time.monotonic()

    build_b = app.build_trigger(
        square(values=shared, offset=5),
        reactive=True,
        tick_kwargs={
            "linger_seconds": B_LINGER_SECONDS,
            "poll_interval_seconds": 3,
        },
    ).build_id

    # B is in, but "in" has to mean *registered against the shared task*,
    # and registered while it was still RUNNING. Both halves are checked
    # rather than assumed, because a miss leaves the rest of the test
    # passing against a different situation.
    wait_until_registered(deployment, task_id=shared.id, build_id=build_b)
    # Printed on every run, because PRE_YIELD_SECONDS is the one number
    # here that is a guess about infrastructure rather than about
    # scheduling, and this is the evidence for whether it is still the
    # right guess. A margin trending towards zero is the warning that the
    # assertion below is about to start failing.
    margin = PRE_YIELD_SECONDS - (time.monotonic() - running_at)
    print(
        f"[suspended] build B registered with {margin:.0f}s of the "
        f"{PRE_YIELD_SECONDS}s pre-yield window to spare"
    )
    status_when_registered = task_status(shared.id)
    assert status_when_registered == "running", (
        f"Build B registered against the shared task when it was already "
        f"{status_when_registered!r}, not RUNNING. Past the yield, plan "
        "closure pulls the dynamic children into B's plan too, so B has "
        "work of its own, never stalls, and never classifies a blocker -- "
        "the scenario silently becomes a different one. Raise "
        f"PRE_YIELD_SECONDS (currently {PRE_YIELD_SECONDS}s) so B's "
        "bootstrap fits inside the pre-yield window.\n" + describe(build_b)
    )

    # The state under test: the worker registered children, yielded and
    # returned, so the task holds no claim while A progresses them.
    wait_for_task_status(
        shared.id,
        expected="suspended",
        build_id=build_a,
        timeout=STATUS_TIMEOUT_SECONDS,
    )

    # Both builds run to completion. B's is the one in question -- it can
    # only get there by having waited for the blocker rather than taking
    # it.
    status_a = wait_for_terminal(build_a, timeout=BUILD_TIMEOUT_SECONDS)
    assert status_a == "completed", describe(build_a)
    status_b = wait_for_terminal(build_b, timeout=BUILD_TIMEOUT_SECONDS)
    assert status_b == "completed", (
        "Build B never finished. It was blocked on a task suspended under "
        "another build, which then completed.\n"
        f"--- build A ---\n{describe(build_a)}\n"
        f"--- build B ---\n{describe(build_b)}"
    )

    # The decisive observable, and the reason this reads the event log at
    # all: whether B ever reset a task that was not its to take.
    events = task_events(deployment, shared.id)
    resets = resets_by(events, build_b)
    assert not resets, (
        "Build B reset the shared task while it was SUSPENDED under build "
        "A. A suspended task holds no claim, but it is not abandoned: its "
        "worker registered dynamic children and returned, and A is "
        "progressing them. Resetting it discards all the pre-yield work "
        "and races A's scheduling of those children.\n"
        f"--- events on the shared task ---\n"
        f"{describe_events(events, A=build_a, B=build_b)}"
    )

    summaries_b = tick_summaries(build_b)
    assert_trail_complete(build_b, summaries_b)

    # The same conclusion from the ticks' own account of themselves, which
    # is what an operator would read. Summed over the whole trail rather
    # than pinned to one tick: the claim is that *no* tick of B ever reset
    # the blocker, which is both stronger and independent of how many
    # wake-ups B happened to get.
    reset_count = sum(s.get("in_build_blockers_reset", 0) for s in summaries_b)
    assert reset_count == 0, (
        f"Build B's ticks reset a blocker in its own plan {reset_count} "
        "time(s). The shared task is the only blocker there, and it was "
        "not B's to take.\n" + describe(build_b)
    )
    waited = sum(s.get("external_blockers_waited", 0) for s in summaries_b)
    assert waited >= 1, (
        "No tick of build B ever reported waiting on an external blocker, "
        "so B never met the shared task as one -- it may have raced past "
        "the window and scheduled work of its own instead. Nothing above "
        "this line is meaningful in that case.\n" + describe(build_b)
    )
    fatal = sum(s.get("external_blockers_fatal", 0) for s in summaries_b)
    assert fatal == 0, (
        "Build B treated the suspended task as a fatal blocker. A task "
        "mid-flight in another build is a wait, not a failure.\n" + describe(build_b)
    )

    # B never ran a second copy: it waited for A's, then used the output.
    # Its own spawns are its root alone.
    spawned_b = sum(s.get("spawned", 0) for s in summaries_b)
    assert spawned_b == 1, (
        f"Build B spawned {spawned_b} task(s); it should have spawned only "
        "its own root, having waited for the suspended task rather than "
        "starting a second copy of it.\n" + describe(build_b)
    )
