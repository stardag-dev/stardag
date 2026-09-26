"""One task completing wakes several dormant builds -- each exactly once.

The two-build wake-up next door shows the news travels. This shows it does
not multiply, which is a separate property and the one that decides whether
the mechanism is usable at all.

The hazard is structural rather than incidental. When a shared task
completes, *every* build holding it is flagged in one write, and several
independent things then go looking for flagged builds: the worker that
wrote the status, the owning build's tick on its next pass, and that tick
again on its way out. Nothing coordinates them. The naive outcome is every
drainer spawning a tick for every flagged build -- a container per
(notifier x neighbour) pair, which grows quadratically with exactly the
contention that makes wake-ups matter in the first place.

What prevents it is that the *hand-out* is a write, not a read: asking for
wake candidates stamps each build it returns, and a build already stamped
inside the window is handed to nobody else. So the bound is one tick per
flagged build per window, however many notifiers ask and however close
together.

That is invisible with two builds, where one spawn and a storm look the
same. It needs a neighbourhood.
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
    describe,
    require_complete_trail,
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

# Enough neighbours that one spawn each and a storm are different numbers.
# Three is the smallest count that shows the difference, and each one is a
# real Modal container, so it is the count used.
NEIGHBOURS = 3

# The shared task must outlive every neighbour's linger by a clear margin,
# so that when it completes there is provably no scheduler anywhere for any
# of them.
SHARED_SLEEP_SECONDS = 75
NEIGHBOUR_LINGER_SECONDS = 15

# A build is handed out at most once per this window, server-side. The
# assertion below is really a statement about it: every notifier draining
# in the seconds after the shared task completes falls inside one window.
WAKE_HANDOUT_WINDOW_SECONDS = 120

STATUS_TIMEOUT_SECONDS = 300
BUILD_TIMEOUT_SECONDS = 600


@pytest.mark.budget(175)
def test_many_dormant_builds_are_each_woken_once(deployment: Deployment) -> None:
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

    owner = app.build_trigger(
        get_sum(integers=shared),
        reactive=True,
        # The owner stays resident through its own work: every build whose
        # progress is in question should be a neighbour.
        tick_kwargs={"linger_seconds": 200, "poll_interval_seconds": 3},
    ).build_id

    # Let the owner claim and start the shared task before the neighbours
    # arrive, so they meet it RUNNING and wait rather than racing for it.
    wait_for_task_status(
        shared.id,
        expected="running",
        build_id=owner,
        timeout=STATUS_TIMEOUT_SECONDS,
    )

    # The dormancy precondition, measured rather than assumed. The shared
    # task is already RUNNING, so its *total* duration says nothing about
    # whether the neighbours will go dormant -- what decides that is the
    # work still to come when their ticks start. Comparing the constant
    # would let a slow bootstrap leave a neighbour resident through the
    # completion while SHARED_SLEEP_SECONDS > NEIGHBOUR_LINGER_SECONDS
    # still looked reassuring.
    assert_remaining_work_outlasts_linger(
        deployment,
        shared.id,
        total_seconds=SHARED_SLEEP_SECONDS,
        linger_seconds=NEIGHBOUR_LINGER_SECONDS,
        what="the shared task's remaining work against each neighbour's linger",
    )

    # Each neighbour has a root of its own, so these are genuinely N builds
    # with N plans that overlap, not one plan triggered N times.
    neighbours = [
        app.build_trigger(
            square(values=shared, offset=offset),
            reactive=True,
            tick_kwargs={
                "linger_seconds": NEIGHBOUR_LINGER_SECONDS,
                "poll_interval_seconds": 3,
            },
        ).build_id
        for offset in range(1, NEIGHBOURS + 1)
    ]

    status_owner = wait_for_terminal(owner, timeout=BUILD_TIMEOUT_SECONDS)
    assert status_owner == "completed", describe(owner)

    for index, build_id in enumerate(neighbours):
        status = wait_for_terminal(build_id, timeout=BUILD_TIMEOUT_SECONDS)
        assert status == "completed", (
            f"Neighbour {index} never finished after the shared task "
            "completed.\n"
            f"--- owner ---\n{describe(owner)}\n"
            f"--- neighbour {index} ---\n{describe(build_id)}"
        )

    # Two passes over the neighbours, and the split is load-bearing. The
    # durable assertions run for *every* neighbour first; only then do the
    # trail-only ones, which can skip. `require_complete_trail` skips the
    # whole test, so a single truncated neighbour in a combined loop would
    # carry off the durable checks of every neighbour after it -- turning
    # one preempted tick into three unexamined builds.
    for index, build_id in enumerate(neighbours):
        summaries = tick_summaries(build_id)
        assert_trail_complete(build_id, summaries)

        lingered = sum(1 for s in summaries if s.get("outcome") == "lingered_out")
        print(
            f"[harness] neighbour {index}: {len(summaries)} tick summary(ies) "
            f"retained, {lingered} reporting lingered_out"
            + (" (trail may be truncated)" if trail_may_be_truncated(build_id) else ""),
            file=sys.stderr,
        )

        # It waited for the owner's copy rather than running a second one:
        # its own spawns are its root alone. Counted from the event log --
        # a submitted execution leaves a row naming the call, written
        # before the container reports anything, so a preempted reporter
        # cannot make it short.
        spawned = sum(spawned_executions(deployment, build_id).values())
        assert spawned == 1, (
            f"Neighbour {index} spawned {spawned} task(s); it should have "
            "spawned only its own root, having waited for the shared task "
            "rather than running a second copy.\n" + describe(build_id)
        )

    for index, build_id in enumerate(neighbours):
        # And it *was* dormant -- observed, not predicted. The measured
        # precondition bounds the work left when the neighbours were
        # *triggered*, not when their ticks started, so a slow bootstrap
        # can still eat the margin: necessary, but not sufficient alone.
        # A tick that lingered out is the outcome itself.
        #
        # Through `require_complete_trail`, because this is a trail
        # observation and a preempted terminal tick must not redden the
        # scenario for it -- which is what STA-89 set out to stop. And
        # `any` rather than `summaries[0]`: when the first tick is
        # preempted, `summaries[0]` silently becomes the second one,
        # while the question the scenario means -- some tick lingered out
        # and a later one did the work -- does not depend on the order.
        require_complete_trail(
            build_id, what=f"whether any tick of neighbour {index} lingered out"
        )
        assert any(
            s.get("outcome") == "lingered_out" for s in tick_summaries(build_id)
        ), (
            f"Neighbour {index} never lingered out, so it may have been "
            "resident throughout and seen the shared task finish on its own "
            "poll -- the wake-up path would then not have been exercised.\n"
            + describe(build_id)
        )

        # Not asserted on the *count of ticks*, which a cold container
        # would inflate for reasons that have nothing to do with the
        # hand-out stamp.
        #
        # ``lease_held`` cannot be reached that way: it means a tick
        # arrived while another held the lease for the same build. One is
        # allowed, because the owning tick's exit hand-off can genuinely
        # race a drain. Several is the storm the hand-out stamp exists to
        # prevent -- every notifier spawning for every flagged build.
        #
        # Trail-only, with no durable counterpart, so it may decline to
        # answer -- which is why every durable check above ran first.
        require_complete_trail(build_id, what=f"neighbour {index}'s lease-held count")
        contended = sum(
            1 for s in tick_summaries(build_id) if s.get("outcome") == "lease_held"
        )
        assert contended <= 1, (
            f"Neighbour {index} had {contended} tick(s) find the scheduler "
            "lease already held, so that many redundant containers were "
            "started for one build. At most one is expected (the exit "
            "hand-off racing a drain); more means several notifiers each "
            "spawned a tick for the same build, so the wake-candidate "
            "hand-out is not stamping builds as it hands them out -- "
            f"within one {WAKE_HANDOUT_WINDOW_SECONDS}s window a build "
            "must be handed to exactly one caller.\n" + describe(build_id)
        )
