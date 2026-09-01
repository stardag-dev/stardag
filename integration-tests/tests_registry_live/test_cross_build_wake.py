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
  no container of its own anywhere. The assertion on B's first tick outcome
  is what pins that down.
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

import uuid

import pytest

from stardag_integration_tests.registry_live._guard import registry_live_guard
from stardag_integration_tests.registry_live._wait import (
    assert_trail_complete,
    describe,
    tick_summaries,
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
# "a tick watched a task finish" and still passes. The assertion on B's
# first tick outcome is what catches that, which is what allows these to be
# sized tightly rather than padded.
SHARED_SLEEP_SECONDS = 75
B_LINGER_SECONDS = 15

STATUS_TIMEOUT_SECONDS = 300
BUILD_TIMEOUT_SECONDS = 600


def test_a_blockers_completion_wakes_a_dormant_build() -> None:
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
    assert summaries_b[0].get("outcome") == "lingered_out", (
        "Build B's first tick did not linger out, so it may have been "
        "resident when the blocker completed and noticed on its own poll. "
        f"The shared task's sleep ({SHARED_SLEEP_SECONDS}s) must "
        f"comfortably outlast B's linger ({B_LINGER_SECONDS}s) plus B's "
        "bootstrap.\n" + describe(build_b)
    )
    assert len(summaries_b) > 1, (
        "Build B ran exactly one tick, so it cannot have been woken.\n"
        + describe(build_b)
    )

    # B never ran the shared task: it waited for A's copy and then used it.
    # Its own spawns are its root alone.
    spawned_b = sum(s.get("spawned", 0) for s in summaries_b)
    assert spawned_b == 1, (
        f"Build B spawned {spawned_b} tasks; it should have spawned only its "
        "own root, having waited for the shared task rather than running a "
        "second copy of it.\n" + describe(build_b)
    )
