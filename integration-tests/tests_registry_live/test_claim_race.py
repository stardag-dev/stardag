"""Two builds want the same task; the registry lets exactly one run it.

``lib/stardag``'s ``test_live_claim_e2e`` covers this shape already, and
covers it well -- but both builds there are coroutines in one event loop
sharing an in-process registry, where, as its own docstring says, the claim
is atomic *by construction*. Single-threaded code cannot interleave inside
the claim, so what that test pins is the engine's behaviour on either side
of an arbitration it cannot get wrong.

Here the arbiter is the deployed API's ``SELECT ... FOR UPDATE``
transaction, reached over the network by two builds that share nothing but
it. Nothing about the outcome is guaranteed by construction: two
independent Modal containers ask, and one of them is told no.

The observable that matters is not "the build finished" -- both do -- but
the spawn count. A task executed twice is the exact failure the claim
exists to prevent, it is silent, and on anything with side effects it is
the expensive kind of silent.
"""

from __future__ import annotations

import uuid

import pytest

from stardag_integration_tests.registry_live._guard import registry_live_guard
from stardag_integration_tests.registry_live._wait import (
    assert_trail_complete,
    describe,
    find_task,
    tick_summaries,
    wait_for_terminal,
)

registry_live_guard()

pytestmark = [
    pytest.mark.registry_live,
    pytest.mark.timeout(900),
]

# Long enough that the second build certainly meets the shared task
# RUNNING rather than already finished.
SHARED_SLEEP_SECONDS = 40

# The second build's tick waits this out inside a single tick. That is
# deliberate here: this scenario is about the claim, and letting one tick
# see the whole story keeps it independent of the wake-up path that
# test_cross_build_wake covers.
LINGER_SECONDS = 150

BUILD_TIMEOUT_SECONDS = 480

# leaf, shared, and one distinct root per build.
DISTINCT_TASKS = 4


def test_a_shared_task_runs_once_across_two_builds() -> None:
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
    # Each build has its own root, so they are genuinely two builds with
    # two plans that happen to overlap -- not one plan triggered twice.
    root_a = get_sum(integers=shared)
    root_b = square(values=shared, offset=7)

    tick_kwargs = {"linger_seconds": LINGER_SECONDS, "poll_interval_seconds": 3}
    build_a = app.build_trigger(root_a, reactive=True, tick_kwargs=tick_kwargs).build_id
    build_b = app.build_trigger(root_b, reactive=True, tick_kwargs=tick_kwargs).build_id

    # Triggered back to back, so the two bootstraps overlap: A has a few
    # seconds' head start and usually wins, but which one wins is not the
    # subject and is not asserted. That exactly one does, is.
    status_a = wait_for_terminal(build_a, timeout=BUILD_TIMEOUT_SECONDS)
    status_b = wait_for_terminal(build_b, timeout=BUILD_TIMEOUT_SECONDS)
    assert status_a == "completed", describe(build_a)
    assert status_b == "completed", describe(build_b)

    summaries_a = tick_summaries(build_a)
    summaries_b = tick_summaries(build_b)
    assert_trail_complete(build_a, summaries_a)
    assert_trail_complete(build_b, summaries_b)

    # The whole point. Four distinct tasks exist across the two plans, and
    # four spawns happened in total: the shared pair ran once, not once per
    # build. Five would mean the claim did not hold.
    spawned = sum(s.get("spawned", 0) for s in (*summaries_a, *summaries_b))
    assert spawned == DISTINCT_TASKS, (
        f"{spawned} spawns for {DISTINCT_TASKS} distinct tasks. More than "
        f"{DISTINCT_TASKS} means the shared task ran in both builds and the "
        "registry's claim did not arbitrate -- or that an interrupted "
        "worker legitimately respawned, so check the trail's interruption "
        "counters first; fewer means a task did not run at all.\n"
        f"--- build A ---\n{describe(build_a)}\n"
        f"--- build B ---\n{describe(build_b)}"
    )

    # Diagnostics, not a second assertion. It is worth *printing* which
    # build ended up owning the shared task, and worth failing if nothing
    # does -- but `latest_status_build_id` is a single column on a row that
    # is unique per (environment, task), and the only builds that ever
    # touch this salted task are A and B. So "the owner is A or B" holds
    # unconditionally, including in the very failure this test exists to
    # catch: if the claim broke and both builds ran the task, the column
    # simply holds whichever wrote last.
    #
    # An earlier version of this test asserted exactly that and described
    # it as "the claim itself rather than a proxy for it". It was neither.
    # The spawn count above is the whole test.
    #
    # `claim_denied` is likewise reported and not asserted: a refused claim
    # only happens when two ticks read the frontier before either has
    # written, which is a genuine race and not a property a test may
    # require. The commoner outcome is the second tick reading a frontier
    # where the task is already RUNNING and never attempting the claim.
    # Both are the registry arbitrating.
    shared_row = find_task(str(shared.id), task_name="Slow")
    owner = shared_row.latest_status_build_id
    denied = sum(s.get("claim_denied", 0) for s in (*summaries_a, *summaries_b))
    assert owner is not None, (
        "The shared task completed but no build owns its status, so the "
        "registry has no record of who ran it.\n"
        f"--- build A ---\n{describe(build_a)}\n"
        f"--- build B ---\n{describe(build_b)}"
    )
    print(
        f"[claim] shared task ran under "
        f"{'A' if owner == build_a else 'B' if owner == build_b else owner}; "
        f"the other build reused it (claim_denied across both: {denied})"
    )
