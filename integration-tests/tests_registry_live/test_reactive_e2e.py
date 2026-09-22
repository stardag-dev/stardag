"""A reactive build drives itself to completion, with nothing resident.

This is the crossing nothing else covers. ``lib/stardag``'s
``test_live_reactive_e2e`` runs the same loop against real Modal workers,
but its registry is in-process and its ticks are driven by the test in a
loop -- so what it proves is that a tick does the right thing when someone
calls it. The question here is who calls it.

The mechanism under test, end to end:

1. the trigger registers the plan and spawns the bootstrap tick;
2. a tick claims a task, spawns a detached Modal worker, lingers briefly
   and **exits** -- at which point no process anywhere is watching;
3. the worker finishes and writes its status to the deployed registry;
4. the registry flags the build as a wake candidate on that write;
5. the **worker** spawns the next tick, because no scheduler is live.

Step 5 is the part that only exists when a real worker can reach a real
registry, and it is the reason this tier had to deploy one.

Nothing else can account for a completed build here. The app deploys no
watchdog, and the assertions below rule out the two remaining ways a build
can finish that would look the same from the outside: a single tick that
lingered through the whole run, and a later tick that *noticed* the
completion itself rather than being told about it.
"""

from __future__ import annotations

import sys
import uuid

import pytest
from stardag_integration_tests.registry_live._events import (
    spawned_executions,
)
from stardag_integration_tests.registry_live._guard import registry_live_guard
from stardag_integration_tests.registry_live._harness import Deployment
from stardag_integration_tests.registry_live._wait import (
    assert_dormancy_is_forced,
    assert_trail_complete,
    describe,
    require_complete_trail,
    tick_summaries,
    trail_may_be_truncated,
    wait_for_terminal,
)

registry_live_guard()

pytestmark = [
    pytest.mark.registry_live,
    # The project-wide pytest-timeout default is 120s, which every scenario
    # here would trip: they wait on real Modal containers.
    pytest.mark.timeout(900),
]

# The leaf sleeps for this long, and the tick that spawns it lingers for
# much less. That ordering is the scenario: the spawning tick has to be
# gone by the time the worker finishes, or the completion is observed by a
# tick that was still around and step 5 never happens.
WORKER_SLEEP_SECONDS = 45
TICK_LINGER_SECONDS = 10

# Generous: it covers a cold Modal container for each worker, plus the
# wake-ups between them.
BUILD_TIMEOUT_SECONDS = 420

# get_range -> slow -> get_sum. Fixed on every run, in a fresh environment
# or a reused one, because the salt is a parameter of the leaf -- see the
# note on `get_range`.
TASKS_IN_PLAN = 3


def test_a_worker_wakes_the_build_that_has_no_scheduler(deployment: Deployment) -> None:
    from stardag_integration_tests.registry_live.dag_app import app
    from stardag_integration_tests.registry_live.tasks import (
        get_range,
        get_sum,
        slow,
    )

    # Fresh task ids for this run -- see the note on `get_range`.
    salt = uuid.uuid4().hex

    leaf = get_range(limit=8, salt=salt)
    paused = slow(values=leaf, seconds=WORKER_SLEEP_SECONDS)
    root = get_sum(integers=paused)

    triggered = app.build_trigger(
        root,
        reactive=True,
        tick_kwargs={
            "linger_seconds": TICK_LINGER_SECONDS,
            "poll_interval_seconds": 3,
        },
    )
    build_id = triggered.build_id

    status = wait_for_terminal(build_id, timeout=BUILD_TIMEOUT_SECONDS)
    assert status == "completed", describe(build_id)

    summaries = tick_summaries(build_id)
    assert_trail_complete(build_id, summaries)

    # The build was dormant before its work finished, which is the whole
    # precondition for the wake-up being a wake-up. Established from the
    # constants rather than from the trail -- see the helper.
    assert_dormancy_is_forced(
        work_seconds=WORKER_SLEEP_SECONDS,
        linger_seconds=TICK_LINGER_SECONDS,
        what="the leaf's sleep against the tick's linger",
    )

    # Diagnostic, never an assertion: what the ticks reported.
    # A trail that shows no lingering tick is worth seeing, but its
    # absence is evidence about the reporters, not about the wake-up.
    lingered = sum(1 for s in summaries if s.get("outcome") == "lingered_out")
    print(
        f"[harness] {len(summaries)} tick summary(ies) retained, "
        f"{lingered} reporting lingered_out"
        + (" (trail may be truncated)" if trail_may_be_truncated(build_id) else ""),
        file=sys.stderr,
    )

    # One spawn per task across every tick: each ran exactly once. Double
    # execution is the thing the claim exists to prevent, and a spawn count
    # above this is what it looks like -- though a Modal preemption also
    # produces a legitimate re-spawn, so read the trail before blaming the
    # claim.
    # Counted from the event log, not from the tick trail. A tick spawns
    # only after the registry grants it the claim, and that grant is a
    # row -- written before the container exists, so nothing the
    # container does later can unwrite it. Summing `spawned` instead
    # would be short whenever a tick was preempted before reporting, and
    # relaxing that to `<=` would pass *because* the evidence is gone.
    claims = spawned_executions(deployment, build_id)
    spawned = sum(claims.values())
    assert spawned == TASKS_IN_PLAN, (
        f"{spawned} spawns for {TASKS_IN_PLAN} tasks. More than "
        f"{TASKS_IN_PLAN} usually means double execution, but an "
        "interrupted worker legitimately respawns -- check the trail for "
        "interruption counters before concluding the claim failed.\n"
        + describe(build_id)
    )

    # At least one completion arrived as a report from its worker rather
    # than being noticed after the fact.
    #
    # A tick self-heals a completion when it finds the work done but
    # unreported -- the resilience path for a registry-less or dead worker.
    # Requiring *zero* of those looks like the tighter assertion and is
    # simply wrong: a lingering tick polls the target for ground truth, so
    # it can legitimately see an output land before the worker's status
    # write arrives, and self-heal a task whose worker was about to report
    # it. The manual verification against a deployed stack records the same
    # thing (`self_healed=2` on a five-task build).
    #
    # What cannot happen if the workers are reaching the registry is *every*
    # completion being self-healed. That is the shape of a missing or wrong
    # stardag-api-key secret, and it is what this rules out.
    require_complete_trail(build_id, what="the self-healed count")
    self_healed = sum(s.get("self_healed", 0) for s in summaries)
    assert self_healed < TASKS_IN_PLAN, (
        f"All {TASKS_IN_PLAN} completions had to be self-healed by a tick, "
        "so no worker reported its own status. The workers are not reaching "
        "the deployed registry -- check that the stardag-api-key secret "
        "landed in this run's Modal environment.\n" + describe(build_id)
    )

    # The database this was all recorded in still exists.
