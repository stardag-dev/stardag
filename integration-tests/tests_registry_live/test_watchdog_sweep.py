"""The watchdog sweep dispatches: it spawns a tick per build and returns.

The sweep is the safety net under everything else here -- what catches a
wake-up that was never delivered, a build abandoned RUNNING, a state change
nobody wrote an event for. It is also the one mechanism in this tier that
nothing else can exercise incidentally, because every other scenario
depends on it *not* running.

The contract has two halves, and the first is a cost:

**It dispatches rather than schedules.** It lists the app's running builds,
spawns one ``tick`` each, and returns -- in seconds, whatever the number of
builds. An earlier design ran every tick body sequentially inside the
sweep's own container, which made three things a function of how many
builds the environment happened to be running: each build's spawn cap (that
one container's timeout divided across the sweep), the latency for the last
build in the list, and whether the sweep finished at all. So the elapsed
time of the call is an observable, not instrumentation: an implementation
that inlined the work would be correct in every other respect and would
show up here as a call that takes as long as the work does.

**Each build gets its own container, and its own full timeout.** Which is
what makes a swept tick indistinguishable from any other tick, rather than
a degraded one.

What a swept tick does *not* get is a linger. That is deliberate and it is
the opposite of a wake-up's tick: a wake-up means something changed and
more probably will, while a sweep's population is by construction builds
where nothing is known to have happened. Lingering there would spend
container time on the builds least likely to have anything to do -- and
because a container's lifetime is the maximum over its live inputs, a
couple of stale RUNNING builds would keep the tick function warm for
nothing.

**Why this scenario has an app to itself.** The sweep lists running builds
scoped by *reactive app name*. Every scenario in this tier runs
concurrently in one Modal environment, so a sweep driven against the shared
app would spawn ticks for whatever else was running at that moment --
waking the dormant builds that four other scenarios assert cannot be woken
by anything but the mechanism they are testing. They would not fail; they
would quietly stop meaning anything. A second app name is the whole
isolation, and it is cheap: the image is identical.
"""

from __future__ import annotations

import uuid

import pytest

from stardag_integration_tests.registry_live._deployed import run_watchdog_sweep
from stardag_integration_tests.registry_live._guard import registry_live_guard
from stardag_integration_tests.registry_live._harness import Deployment
from stardag_integration_tests.registry_live._wait import (
    assert_trail_complete,
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

# Builds for the sweep to find. More than one, because the whole question
# is what the sweep does with N of them; three keeps the container cost
# down while still making "one tick each" a different number from "one tick
# total" and from "N x N".
SWEPT_BUILDS = 3

# Each build's own task runs long enough that all three are still RUNNING
# when the sweep happens -- a build that finished on its own is not a build
# the sweep had anything to do for.
SLOW_SECONDS = 90

# ...and each build's own scheduler gives up well before that, so nothing
# of theirs is alive when the sweep runs. Otherwise a swept tick would find
# the scheduler lease held and exit, and the sweep would look ineffective.
LINGER_SECONDS = 10

# What "returns in seconds" is held to. Generous by an order of magnitude
# against the thing it is distinguishing from: inlining three tick bodies
# would take minutes, since each would linger or poll through real work.
# Loose enough not to flake on a slow container start for the sweep itself.
SWEEP_BUDGET_SECONDS = 60

DORMANT_TIMEOUT_SECONDS = 300
BUILD_TIMEOUT_SECONDS = 600


def test_one_sweep_spawns_one_tick_per_build_and_returns(
    deployment: Deployment,
) -> None:
    from stardag_integration_tests.registry_live.tasks import (
        get_range,
        get_sum,
        slow,
    )
    from stardag_integration_tests.registry_live.watchdog_app import APP_NAME, app

    salt = uuid.uuid4().hex

    builds = [
        app.build_trigger(
            get_sum(
                integers=slow(
                    values=get_range(limit=8 + index, salt=salt),
                    seconds=SLOW_SECONDS,
                )
            ),
            reactive=True,
            tick_kwargs={
                "linger_seconds": LINGER_SECONDS,
                "poll_interval_seconds": 3,
            },
        ).build_id
        for index in range(SWEPT_BUILDS)
    ]

    # Every build's own scheduler must be gone before the sweep, or the
    # sweep's ticks would simply find the lease held. Waiting for the
    # observable -- a first tick that lingered out -- rather than sleeping
    # long enough for it to be likely.
    for index, build_id in enumerate(builds):
        wait_until(
            lambda build_id=build_id: any(
                summary.get("outcome") == "lingered_out"
                for summary in tick_summaries(build_id)
            ),
            build_id=build_id,
            timeout=DORMANT_TIMEOUT_SECONDS,
            poll_interval=3.0,
            what=(
                f"build {index}'s own tick to linger out, leaving it "
                "dormant with work still running"
            ),
        )

    ticks_before = {build_id: len(tick_summaries(build_id)) for build_id in builds}

    elapsed = run_watchdog_sweep(
        app_name=APP_NAME, modal_environment=deployment.modal_environment
    )

    # The cost half of the contract.
    assert elapsed < SWEEP_BUDGET_SECONDS, (
        f"The sweep took {elapsed:.0f}s for {SWEPT_BUILDS} builds. It is "
        "supposed to list them, spawn a tick each and return -- a duration "
        "independent of how many it found. Taking this long means it ran "
        "the tick bodies itself, which also gives each build a share of "
        "one container's timeout instead of its own."
    )

    # ...and the effect half: every build got a tick it did not have
    # before. Counting from a baseline rather than from zero because these
    # builds each ran their own bootstrap tick already.
    for index, build_id in enumerate(builds):
        wait_until(
            lambda build_id=build_id: len(tick_summaries(build_id))
            > ticks_before[build_id],
            build_id=build_id,
            timeout=DORMANT_TIMEOUT_SECONDS,
            poll_interval=3.0,
            what=(
                f"build {index} to record a tick from the sweep (it had "
                f"{ticks_before[build_id]} before)"
            ),
        )

    # The sweep is a safety net, so the builds it swept should also finish.
    for index, build_id in enumerate(builds):
        status = wait_for_terminal(build_id, timeout=BUILD_TIMEOUT_SECONDS)
        assert status == "completed", (
            f"Build {index} did not complete after being swept.\n" + describe(build_id)
        )
        summaries = tick_summaries(build_id)
        assert_trail_complete(build_id, summaries)

        # Each task ran once: a sweep that spawns a tick for a build whose
        # work is already in flight must not start a second copy of it.
        # leaf, slow, root.
        spawned = sum(s.get("spawned", 0) for s in summaries)
        assert spawned == 3, (
            f"Build {index} spawned {spawned} tasks for a three-task plan. "
            "A swept tick arriving while the build's own work is running "
            "must not respawn it.\n" + describe(build_id)
        )
