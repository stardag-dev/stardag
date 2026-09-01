"""A layer wider than one pass may spawn still completes, once per task.

A tick lives in a container with a finite life, so "spawn everything that is
actionable" is a bound on nothing: a wide enough layer outlives the
container and the tick is killed mid-fan-out. The per-pass spawn cap exists
to stop that, and the interesting claim about it is not the number -- it is
that hitting it is a **throttle rather than a stall**. The pass acted, it
did as much as it had budget for, and what it left is picked up on the next
pass. Nothing is dropped and nothing is started twice.

**Why the cap is set explicitly here rather than being provoked by width.**
The cap is derived from the tick container's own wall-clock limit, and for
this deployment that derivation lands near two thousand -- a layer wide
enough to reach it would be thousands of real Modal containers, which is
not a test, it is a bill. The derivation itself is not in question: which
duration it reads, down a four-rung ladder, is covered exhaustively by unit
tests, including the Modal integration passing the deployed ``tick``
function's registered timeout into it. Re-proving that here would be
expensive duplication of something already settled.

What is *not* covered anywhere else is what this asks: a real tick, fanning
out to real workers reporting to a real registry, running into its cap and
carrying on correctly. Setting the cap low makes that path reachable at a
width that costs two dozen containers instead of two thousand.

The width still has to be real, though. The leaves are independent and
carry distinct ids, so they are genuinely N actionable tasks in one layer --
a fan-out rather than a deduplication test -- and registering a plan of this
size against a deployed registry over the network is itself part of what
gets exercised.
"""

from __future__ import annotations

import uuid

import pytest

from stardag_integration_tests.registry_live._guard import registry_live_guard
from stardag_integration_tests.registry_live._wait import (
    assert_trail_complete,
    describe,
    tick_summaries,
    wait_for_terminal,
)

registry_live_guard()

pytestmark = [
    pytest.mark.registry_live,
    pytest.mark.timeout(900),
]

# The width of the one layer. Every leaf is a real Modal container, so this
# is chosen as the smallest number that is comfortably several times the
# cap below -- enough that the fan-out is spread over several passes rather
# than two.
WIDTH = 24

# Deliberately far below anything the deployment would derive, so the pass
# truncates. See the module docstring for why this is set rather than
# provoked.
MAX_SPAWNS_PER_TICK = 8

# Long enough that one tick can carry the whole build, so what is under
# test is a *pass* boundary rather than a container boundary: without it,
# each truncated pass would end the tick and the next pass would arrive as
# a wake-up, which is a different mechanism tested elsewhere.
#
# Nothing below *asserts* that only one tick ran, and deliberately not --
# the spawn-count assertion is the real subject and holds however the
# passes were distributed across containers, so pinning the tick count
# would only add a way to fail for reasons that are not the point.
LINGER_SECONDS = 150

# WIDTH leaves plus the single root that joins them.
TASKS_IN_PLAN = WIDTH + 1

BUILD_TIMEOUT_SECONDS = 600


def test_a_layer_wider_than_one_pass_completes_once_per_task() -> None:
    from stardag_integration_tests.registry_live.dag_app import app
    from stardag_integration_tests.registry_live.tasks import FanIn

    salt = uuid.uuid4().hex

    build_id = app.build_trigger(
        FanIn(salt=salt, width=WIDTH),
        reactive=True,
        tick_kwargs={
            "max_spawns_per_tick": MAX_SPAWNS_PER_TICK,
            "linger_seconds": LINGER_SECONDS,
            "poll_interval_seconds": 3,
        },
    ).build_id

    status = wait_for_terminal(build_id, timeout=BUILD_TIMEOUT_SECONDS)
    assert status == "completed", (
        f"A {WIDTH}-wide layer under a per-pass cap of "
        f"{MAX_SPAWNS_PER_TICK} did not complete. A truncated pass is "
        "supposed to be a throttle, not a stall: the tick spawns what it "
        "has budget for and picks the rest up on its next pass.\n" + describe(build_id)
    )

    summaries = tick_summaries(build_id)
    assert_trail_complete(build_id, summaries)

    # One spawn per task across every pass of every tick. This is the
    # assertion that truncation is clean: the tasks a pass declined to
    # start have to be started later exactly once, and the ones it did
    # start must not be started again when the next pass re-reads a
    # frontier that still lists them as it left them.
    spawned = sum(s.get("spawned", 0) for s in summaries)
    assert spawned == TASKS_IN_PLAN, (
        f"{spawned} spawns for {TASKS_IN_PLAN} tasks. Above it, a "
        "truncated pass re-spawned work the previous pass had already "
        "started -- though an interrupted worker also legitimately "
        "respawns, so check the trail's interruption counters first. Below "
        "it, a task the cap deferred was never picked up.\n" + describe(build_id)
    )

    # No tick died. A tick that ran out of container while fanning out
    # would not report at all, so this catches the other shape: one that
    # reported an error rather than a clean outcome.
    errored = [s for s in summaries if s.get("outcome") == "error"]
    assert not errored, (
        f"{len(errored)} tick(s) ended in an error while fanning out over "
        f"a {WIDTH}-wide layer.\n" + describe(build_id)
    )
