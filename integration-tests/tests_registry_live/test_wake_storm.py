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


def test_many_dormant_builds_are_each_woken_once() -> None:
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

    for index, build_id in enumerate(neighbours):
        summaries = tick_summaries(build_id)
        assert_trail_complete(build_id, summaries)

        # Dormant when the news arrived: its own tick gave up and left.
        assert summaries[0].get("outcome") == "lingered_out", (
            f"Neighbour {index}'s first tick did not linger out, so it may "
            "have been resident when the shared task completed and noticed "
            f"on its own poll. The shared sleep ({SHARED_SLEEP_SECONDS}s) "
            f"must comfortably outlast the linger "
            f"({NEIGHBOUR_LINGER_SECONDS}s).\n" + describe(build_id)
        )

        # The property this scenario exists for. One tick after the
        # bootstrap is the woken one; a second is tolerated because the
        # owning tick's exit hand-off can genuinely race a drain, and it
        # costs one container that finds the lease held and leaves. Three
        # or more is the storm: several notifiers each spawning for the
        # same build, which is what the hand-out stamp prevents.
        woken = len(summaries) - 1
        assert 1 <= woken <= 2, (
            f"Neighbour {index} ran {woken} tick(s) after its bootstrap; "
            "expected 1 (the wake-up), or 2 if the exit hand-off raced a "
            "drain. More than that means several notifiers each spawned a "
            "tick for the same build, so the wake-candidate hand-out is "
            "not stamping builds as it hands them out -- within one "
            f"{WAKE_HANDOUT_WINDOW_SECONDS}s window a build must be handed "
            "to exactly one caller.\n" + describe(build_id)
        )

        # It waited for the owner's copy rather than running a second one:
        # its own spawns are its root alone.
        spawned = sum(s.get("spawned", 0) for s in summaries)
        assert spawned == 1, (
            f"Neighbour {index} spawned {spawned} task(s); it should have "
            "spawned only its own root, having waited for the shared task "
            "rather than running a second copy.\n" + describe(build_id)
        )
