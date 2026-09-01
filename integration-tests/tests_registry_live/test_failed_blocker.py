"""A shared task's failure is a *result*, and a waiting build must leave it.

The mirror image of the suspended case, and the distinction between them is
the whole point. A task another build is mid-flight on must be waited for; a
task that has *failed* must be left entirely alone. Both come down to "do
not reset it", for opposite reasons -- one because the work is still
happening, the other because it has already finished and produced an answer.

A failure belongs to the failing build's ``fail_mode``, which is a policy
its owner chose. FAIL_FAST has already failed that build on the same count;
CONTINUE means "finish what you can, then fail". A second build that reset
the task would override that policy, on nobody's request, and re-run work
that had a verdict. (A *re-trigger* is different and is allowed to reset the
whole retryable set -- there the user did ask.)

Two things about the construction, both of which the scenario would be
meaningless without:

**B must register while the shared task is still RUNNING.** A trigger runs
discovery with ``retry_failed=True`` and resets the retryable set, so a B
triggered after the failure resets the task in its own bootstrap -- correct,
because at trigger time you did ask, and it hides the mid-flight decision
completely. RUNNING is the one status a trigger will not reset, which is why
the task sleeps before failing.

**Both builds run with ``fail_mode=continue``.** Under FAIL_FAST the build
fails on the status count before terminal evaluation ever looks at a
blocker. A additionally gets ``max_attempts=1`` so it does not reset the
task itself -- a reset would put it back to PENDING, where B could claim it
legitimately and the shared-failure shape would be gone.

**B is resident**, unlike the second build in the wake-up scenarios. What
is under test is what a tick *decides* when it looks at a blocker it does
not own, not who called that tick, and keeping B's own scheduler alive
across the window isolates the decision from the delivery machinery --
which is tested on its own elsewhere. It also removes a cold container
start from the middle of the run.
"""

from __future__ import annotations

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

# How long the shared task runs before failing. The window B has to
# register in, sized against a cold Modal container for B's bootstrap --
# not against anything in the decision under test. The run checks the
# ordering actually held rather than trusting the number.
FAIL_AFTER_SECONDS = 60

# B stays resident across the whole window, so its own polling tick is
# the one that meets the failure -- see the module docstring. This costs
# nothing in wall clock: a tick exits as soon as its build goes terminal,
# so the linger is an upper bound that is never reached.
LINGER_SECONDS = 300

STATUS_TIMEOUT_SECONDS = 300
BUILD_TIMEOUT_SECONDS = 600


def test_a_failed_blocker_is_left_alone_by_a_second_build(
    deployment: Deployment,
) -> None:
    from stardag_integration_tests.registry_live.dag_app import app
    from stardag_integration_tests.registry_live.tasks import (
        fails,
        get_range,
        get_sum,
        square,
    )

    salt = uuid.uuid4().hex

    leaf = get_range(limit=7, salt=salt)
    shared = fails(values=leaf, seconds=FAIL_AFTER_SECONDS)

    build_a = app.build_trigger(
        get_sum(integers=shared),
        reactive=True,
        tick_kwargs={
            "fail_mode": "continue",
            # No second attempt: a retry by A would put the task back to
            # PENDING, where B may legitimately claim it, and the shared
            # *failure* this scenario needs would never exist.
            "max_attempts": 1,
            "linger_seconds": 120,
            "poll_interval_seconds": 3,
        },
    ).build_id

    wait_for_task_status(
        shared.id,
        expected="running",
        build_id=build_a,
        timeout=STATUS_TIMEOUT_SECONDS,
    )

    build_b = app.build_trigger(
        square(values=shared, offset=3),
        reactive=True,
        tick_kwargs={
            "fail_mode": "continue",
            # Default attempt budget, and none of it spent on the shared
            # task in B's own round -- so the budget *permits* a reset and
            # the blocker's status is what refuses it. Capping attempts
            # here would let the test pass for the wrong reason.
            "linger_seconds": LINGER_SECONDS,
            "poll_interval_seconds": 3,
        },
    ).build_id

    wait_until_registered(deployment, task_id=shared.id, build_id=build_b)
    status_when_registered = task_status(shared.id)
    assert status_when_registered == "running", (
        f"Build B registered against the shared task when it was already "
        f"{status_when_registered!r}, not RUNNING. A trigger resets the "
        "retryable set, so a B that arrives after the failure resets the "
        "task in its own bootstrap and the mid-flight decision never "
        f"happens. Raise FAIL_AFTER_SECONDS (currently "
        f"{FAIL_AFTER_SECONDS}s) so B's bootstrap fits inside the window.\n"
        + describe(build_b)
    )

    wait_for_task_status(
        shared.id,
        expected="failed",
        build_id=build_a,
        timeout=STATUS_TIMEOUT_SECONDS,
    )

    # Both builds are expected to fail: neither can finish, because the
    # only route to either root is through a task that produced a failure
    # and that nothing here is going to re-run. Waiting for terminal is
    # also what guarantees B's post-failure tick has happened and reported.
    status_a = wait_for_terminal(build_a, timeout=BUILD_TIMEOUT_SECONDS)
    assert status_a == "failed", (
        f"Build A ended {status_a!r}. Its only path runs through a task "
        "that fails deterministically, so it cannot complete.\n" + describe(build_a)
    )
    status_b = wait_for_terminal(build_b, timeout=BUILD_TIMEOUT_SECONDS)
    assert status_b == "failed", (
        f"Build B ended {status_b!r}. It should have failed on a blocker "
        "whose status is a result -- if it completed, it re-ran the shared "
        "task, which is exactly what must not happen.\n" + describe(build_b)
    )

    # The decisive observable. B failed either way; whether it *reset* the
    # task on the way there is what separates correct from expensive, and
    # only the event log records it -- the status columns show whoever
    # wrote last.
    events = task_events(deployment, shared.id)
    resets = resets_by(events, build_b)
    assert not resets, (
        "Build B reset the shared task after it failed under build A. A "
        "failure is a result, and results belong to the failing build's "
        "fail_mode -- resetting it overrides a policy its owner chose and "
        "re-runs work that already has a verdict.\n"
        f"--- events on the shared task ---\n"
        f"{describe_events(events, A=build_a, B=build_b)}"
    )

    summaries_b = tick_summaries(build_b)
    assert_trail_complete(build_b, summaries_b)

    reset_count = sum(s.get("in_build_blockers_reset", 0) for s in summaries_b)
    assert reset_count == 0, (
        f"Build B's ticks reset a blocker in its own plan {reset_count} "
        "time(s). The failed task was the only blocker there.\n" + describe(build_b)
    )

    # B never ran the shared task, nor anything downstream of it -- its
    # root was unreachable from the moment the blocker failed.
    spawned_b = sum(s.get("spawned", 0) for s in summaries_b)
    assert spawned_b == 0, (
        f"Build B spawned {spawned_b} task(s). With its only upstream "
        "failed and left alone, there was nothing for it to run.\n" + describe(build_b)
    )
