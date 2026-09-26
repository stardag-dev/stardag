"""A blocker finishing in one build wakes a different, dormant build.

The bug that started the reactive work was a build failing because another
build held the task it needed. Making it *wait* instead was the first half.
This is the second half, and it is a different mechanism: when the wait
ends, something has to reach across from the build that finished the task
to the build that was waiting for it -- and reaching across is exactly what
a scheduler cannot do from inside a container that has already exited.

The shape is what makes this a test rather than a demonstration, so it is
worth being explicit about the alternatives it rules out:

- **B's own tick did not notice.** B is triggered with a short linger and
  the shared task outlives it, so by the time A completes, B is dormant:
  no container of its own anywhere. Two things pin that down:
  ``assert_remaining_work_outlasts_linger`` before B is triggered, which
  requires the work still to come to outlast B's linger, and -- behind the
  inconclusive guard, since it is a trail reading -- that some tick of B
  did linger out.
- **A watchdog did not sweep it up.** The app deploys none, deliberately.
  A periodic sweep would make every build here eventually complete and
  would make this scenario prove nothing.
- **A worker of B's did not report something.** B has no task of its own
  running; a worker only ever notifies its own build.

So if B completes, the news travelled from A's side: the registry flagged
B as a wake candidate on a status write from A's worker, and a scheduler
drained that flag. There is no other route.
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
    assert_remaining_work_outlasts_linger,
    assert_trail_complete,
    require_complete_trail,
    describe,
    tick_summaries,
    trail_may_be_truncated,
    wait_for_task_status,
    wait_for_terminal,
)

registry_live_guard()

pytestmark = [
    pytest.mark.registry_live,
    pytest.mark.timeout(900),
]

# The shared task must outlive B's tick by a clear margin. These two
# numbers are the scenario; if they ever cross, it silently degrades into
# "a tick watched a task finish" and still passes. The measured
# precondition catches that before B is triggered, and the lingered-out
# observation catches it afterwards, which is what allows these to be sized
# tightly rather than padded.
SHARED_SLEEP_SECONDS = 75
B_LINGER_SECONDS = 15

STATUS_TIMEOUT_SECONDS = 300
BUILD_TIMEOUT_SECONDS = 600


@pytest.mark.budget(125)
def test_a_blockers_completion_wakes_a_dormant_build(deployment: Deployment) -> None:
    from stardag_integration_tests.registry_live.dag_app import app
    from stardag_integration_tests.registry_live.tasks import (
        get_range,
        get_sum,
        slow,
        square,
    )

    salt = uuid.uuid4().hex

    leaf = get_range(limit=8, salt=salt)
    shared = slow(values=leaf, seconds=SHARED_SLEEP_SECONDS)

    build_a = app.build_trigger(
        get_sum(integers=shared),
        reactive=True,
        # A lingers long enough to see its own work through, so that the
        # only build depending on a wake-up is B.
        tick_kwargs={"linger_seconds": 150, "poll_interval_seconds": 3},
    ).build_id

    # Let A claim and start the shared task before B is triggered, so B
    # meets it RUNNING and waits rather than racing for it. Waiting on the
    # status rather than sleeping a guess at how long that takes: the guess
    # is wall clock on every run, and is wrong in the one case that matters
    # -- a slow container start, where it silently produces the racing B it
    # was meant to prevent.
    wait_for_task_status(
        shared.id,
        expected="running",
        build_id=build_a,
        timeout=STATUS_TIMEOUT_SECONDS,
    )

    # The dormancy precondition, measured rather than assumed. The
    # shared task is already RUNNING, so its *total* duration says
    # nothing about whether this build will go dormant -- what
    # decides that is the work still to come when its tick starts.
    # Comparing the constant would let a slow bootstrap leave the
    # build resident through the completion while SHARED_SLEEP_SECONDS >
    # B_LINGER_SECONDS still looked reassuring.
    assert_remaining_work_outlasts_linger(
        deployment,
        shared.id,
        total_seconds=SHARED_SLEEP_SECONDS,
        linger_seconds=B_LINGER_SECONDS,
        what="the shared task's remaining work against B's linger",
    )
    build_b = app.build_trigger(
        square(values=shared, offset=11),
        reactive=True,
        # The point of the run: B's scheduler gives up quickly and is not
        # around when the news arrives.
        tick_kwargs={"linger_seconds": B_LINGER_SECONDS, "poll_interval_seconds": 3},
    ).build_id

    status_a = wait_for_terminal(build_a, timeout=BUILD_TIMEOUT_SECONDS)
    assert status_a == "completed", describe(build_a)

    # B is the subject. If nothing woke it, this is where it hangs.
    status_b = wait_for_terminal(build_b, timeout=BUILD_TIMEOUT_SECONDS)
    assert status_b == "completed", (
        "Build B never finished. It was blocked on a task another build "
        "owned, that task completed, and nothing told B -- which is the "
        "cross-build wake-up failing.\n"
        f"--- build A ---\n{describe(build_a)}\n"
        f"--- build B ---\n{describe(build_b)}"
    )

    summaries_b = tick_summaries(build_b)
    assert_trail_complete(build_b, summaries_b)

    # B really was dormant. Its first tick found the shared task claimed by
    # someone else, waited out its linger and exited with the build still
    # running -- so the tick that finished B afterwards was spawned by the
    # wake-up and not by anything B left behind.

    # Diagnostic, never an assertion: what the ticks reported.
    # A trail that shows no lingering tick is worth seeing, but its
    # absence is evidence about the reporters, not about the wake-up.
    lingered = sum(1 for s in summaries_b if s.get("outcome") == "lingered_out")
    print(
        f"[harness] {len(summaries_b)} tick summary(ies) retained, "
        f"{lingered} reporting lingered_out"
        + (" (trail may be truncated)" if trail_may_be_truncated(build_b) else ""),
        file=sys.stderr,
    )

    # Counted from the event log, not from the tick trail. A tick spawns
    # only after the registry grants it the claim, and that grant is a
    # row -- written before the container exists, so nothing the
    # container does later can unwrite it. Summing `spawned` instead
    # would be short whenever a tick was preempted before reporting, and
    # relaxing that to `<=` would pass *because* the evidence is gone.
    claims_b = spawned_executions(deployment, build_b)
    spawned_b = sum(claims_b.values())
    assert spawned_b == 1, (
        f"Build B spawned {spawned_b} tasks; it should have spawned only its "
        "own root, having waited for the shared task rather than running a "
        "second copy of it.\n" + describe(build_b)
    )

    # And it *was* dormant -- observed, not predicted. The measured
    # precondition bounds the work left when this build was *triggered*,
    # not when its tick actually started, so a slow bootstrap can still
    # eat the margin: necessary, but not sufficient on its own. A tick
    # that lingered out is the outcome itself.
    #
    # Through `require_complete_trail`, because this is a trail
    # observation and a preempted terminal tick must not redden the
    # scenario for it -- which is what STA-89 set out to stop. And `any`
    # rather than `summaries[0]`: when the first tick is preempted,
    # `summaries[0]` silently becomes the second one, while the question
    # the scenario means -- some tick lingered out and a later one did
    # the work -- does not depend on the order.
    require_complete_trail(build_b, what="whether any tick of build B lingered out")
    assert any(s.get("outcome") == "lingered_out" for s in summaries_b), (
        "No tick of build B ever lingered out, so B may have been "
        "resident throughout and seen the shared task finish on its own "
        "poll -- the wake-up path would then not have been exercised.\n"
        + describe(build_b)
    )
