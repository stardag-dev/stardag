"""Two live builds cannot build one task two different ways.

A task id promises a world state, not a provenance — so re-pointing a task
at a new upstream, or re-partitioning how it is produced, and keeping the id
is correct, and the registry takes the latest declaration as authoritative.
What it will not do is take it while another build is running on the old
one. Both declarations are legitimate, both produce the same target, and
materialising it over two upstream DAGs at once is waste nobody asked for.

Two builds are therefore refused here rather than reconciled, and the
scenario covers both answers to "which one stops":

* by default the **new** build fails, at the trigger, naming the build in
  the way — nobody's running work is cancelled without being asked;
* with ``cancel_conflicting`` the new build cancels it and takes over,
  cascading so the other build's containers stop too.

The second half is only safe because a cascade now stops containers rather
than merely releasing their claims.
"""

from __future__ import annotations

import uuid

import pytest

from stardag_integration_tests.registry_live._guard import registry_live_guard
from stardag_integration_tests.registry_live._wait import (
    describe,
    task_status,
    wait_for_task_status,
    wait_for_terminal,
    wait_until,
)

registry_live_guard()

pytestmark = [
    pytest.mark.registry_live,
    pytest.mark.timeout(900),
]

# The first build's upstream has to still be running when the second and
# third builds are triggered — that is the whole scenario, since a finished
# build is not a conflict.
FIRST_UPSTREAM_SECONDS = 90
SECOND_UPSTREAM_SECONDS = 20

STATUS_TIMEOUT_SECONDS = 300
BUILD_TIMEOUT_SECONDS = 600


def test_a_live_build_is_not_re_pointed_out_from_under(deployment) -> None:
    from stardag.registry import registry_provider
    from stardag_integration_tests.registry_live.dag_app import app
    from stardag_integration_tests.registry_live.tasks import (
        ForkingRoot,
        get_range,
        slow,
    )

    salt = uuid.uuid4().hex
    leaf = get_range(limit=1, salt=salt)
    first_upstream = slow(values=leaf, seconds=FIRST_UPSTREAM_SECONDS)
    second_upstream = slow(values=leaf, seconds=SECOND_UPSTREAM_SECONDS)
    # One root id, two declarations: `upstream_seconds` is excluded from the
    # hash, so this is the same task asking for a different upstream.
    root_one = ForkingRoot(salt=salt, upstream_seconds=FIRST_UPSTREAM_SECONDS)
    root_two = ForkingRoot(salt=salt, upstream_seconds=SECOND_UPSTREAM_SECONDS)
    assert root_one.id == root_two.id, (
        "the two roots must be the same task for this scenario to mean "
        "anything — check that upstream_seconds is still hash-excluded"
    )

    build_one = app.build_trigger(
        root_one,
        reactive=True,
        tick_kwargs={"linger_seconds": 200, "poll_interval_seconds": 3},
    ).build_id
    wait_for_task_status(
        first_upstream.id,
        expected="running",
        build_id=build_one,
        timeout=STATUS_TIMEOUT_SECONDS,
    )

    registry = registry_provider.get()

    # --- default: the newcomer fails rather than re-pointing the task ---
    build_two = app.build_trigger(
        root_two,
        reactive=True,
        tick_kwargs={"linger_seconds": 30, "poll_interval_seconds": 3},
    ).build_id
    status_two = wait_for_terminal(build_two, timeout=BUILD_TIMEOUT_SECONDS)
    assert status_two == "failed", (
        "the second build was allowed to re-point a task the first is "
        "building right now.\n" + describe(build_two)
    )
    message = registry.build_get_summary(build_two).latest_error_message or ""
    assert str(build_one) in message, (
        f"the failure must name the build in the way: {message!r}"
    )
    assert "dependenc" in message.lower(), f"...and why: {message!r}"

    # The first build is untouched: still running, still on its own upstream.
    assert task_status(first_upstream.id) == "running", describe(build_one)

    # --- opt in: cancel the build in the way and take the tasks over ---
    build_three = app.build_trigger(
        root_two,
        reactive=True,
        tick_kwargs={"linger_seconds": 200, "poll_interval_seconds": 3},
        cancel_conflicting=True,
    ).build_id

    wait_until(
        lambda: registry.build_get(build_one).status == "cancelled",
        build_id=build_one,
        timeout=STATUS_TIMEOUT_SECONDS,
        what=f"build {build_one} to be cancelled by the third build",
    )
    status_three = wait_for_terminal(build_three, timeout=BUILD_TIMEOUT_SECONDS)
    assert status_three == "completed", (
        "the third build asked to clear the way and should have run.\n"
        f"--- build one (cancelled) ---\n{describe(build_one)}\n"
        f"--- build three ---\n{describe(build_three)}"
    )
    assert task_status(second_upstream.id) == "completed", (
        "the third build ran its own upstream, not the first build's"
    )
