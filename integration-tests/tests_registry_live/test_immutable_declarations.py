"""A task cannot change what it requires without changing its id.

A task id promises the world state its completion establishes, and that
includes the upstream set it was built from. So re-pointing a task at a
different upstream while keeping its id is a broken promise, not a
refactor — and once the registry has recorded a declaration it can say so.

What this asserts that no unit test can: the refusal reaches a real
reactive trigger and stops the build *before* it is armed as reactive, so
nothing is left half-started for a scheduler to find.

And one thing it asserts deliberately twice, because it is the whole
difference from the design this replaced: the refusal does not depend on
anybody else running. The second build is refused while the first is live;
the third is refused after the first has been cancelled. The rule is about
the record, not about who is in the way.
"""

from __future__ import annotations

import uuid

import pytest

from stardag_integration_tests.registry_live._guard import registry_live_guard
from stardag_integration_tests.registry_live._wait import (
    build_status,
    describe,
    wait_for_task_status,
    wait_until,
)

registry_live_guard()

pytestmark = [
    pytest.mark.registry_live,
    pytest.mark.timeout(900),
]

# The first build's upstream only has to still be registering when the
# second build is triggered. It is not a race the scenario depends on —
# the refusal outlives the first build either way — but running it makes
# the first assertion a live one rather than a paperwork one.
FIRST_UPSTREAM_SECONDS = 90
SECOND_UPSTREAM_SECONDS = 20

STATUS_TIMEOUT_SECONDS = 300
BUILD_TIMEOUT_SECONDS = 600

_TERMINAL = ("failed", "completed", "cancelled")


def _wait_terminal(build_id: uuid.UUID) -> str:
    """Wait on the build's own status, not on a tick summary.

    ``wait_for_terminal`` also waits for the tick that ended the build to
    report. A build refused at registration dies inside its bootstrap and
    never ticks, so there is no summary coming and waiting for one would
    time out on a scenario that worked.
    """
    return wait_until(
        lambda: build_status(build_id) if build_status(build_id) in _TERMINAL else None,
        build_id=build_id,
        timeout=BUILD_TIMEOUT_SECONDS,
        what=f"build {build_id} to reach a terminal status",
    )


def _assert_refused(registry, build_id: uuid.UUID, *, upstream: str) -> None:
    status = _wait_terminal(build_id)
    assert status == "failed", (
        "a build declaring different upstreams for an existing task id was "
        "allowed to start.\n" + describe(build_id)
    )
    assert registry.build_get(build_id).reactive_app_name is None, (
        "a refused build must not be left armed as reactive: nothing should "
        "ever tick it.\n" + describe(build_id)
    )
    message = registry.build_get_summary(build_id).latest_error_message or ""
    assert "dependenc" in message.lower(), f"the failure must say why: {message!r}"
    assert upstream in message, (
        f"...and must name the upstream that changed: {message!r}"
    )


def test_a_task_cannot_be_re_pointed_without_a_new_id(deployment) -> None:
    from stardag.registry import registry_provider
    from stardag_integration_tests.registry_live.dag_app import app
    from stardag_integration_tests.registry_live.tasks import (
        ForkingRoot,
        get_range,
        slow,
    )

    registry = registry_provider.get()
    salt = uuid.uuid4().hex
    leaf = get_range(limit=1, salt=salt)
    first_upstream = slow(values=leaf, seconds=FIRST_UPSTREAM_SECONDS)
    root_one = ForkingRoot(salt=salt, upstream_seconds=FIRST_UPSTREAM_SECONDS)
    root_two = ForkingRoot(salt=salt, upstream_seconds=SECOND_UPSTREAM_SECONDS)
    assert root_one.id == root_two.id, (
        "the two roots must be the same task for this scenario to mean "
        "anything — check that upstream_seconds is still hash-excluded"
    )
    assert root_one.requires().id != root_two.requires().id, (
        "...and they must genuinely require different upstreams"
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

    # --- refused while the first build is live ---
    build_two = app.build_trigger(
        root_two,
        reactive=True,
        tick_kwargs={"linger_seconds": 30, "poll_interval_seconds": 3},
    ).build_id
    _assert_refused(registry, build_two, upstream=str(first_upstream.id))

    # The first build is untouched. Nothing was cancelled, nothing was
    # superseded, and it is still building the upstream it declared.
    assert registry.build_get(build_one).status == "running", describe(build_one)

    # --- and still refused once the first build is gone ---
    # The record is what the task promised. Whether anyone is currently
    # acting on it is beside the point, and this is exactly where the
    # replaced design behaved differently: it would have let this through.
    registry.build_cancel(build_one, cascade=True)
    wait_until(
        lambda: registry.build_get(build_one).status == "cancelled",
        build_id=build_one,
        timeout=STATUS_TIMEOUT_SECONDS,
        what=f"build {build_one} to be cancelled",
    )

    build_three = app.build_trigger(
        root_two,
        reactive=True,
        tick_kwargs={"linger_seconds": 30, "poll_interval_seconds": 3},
    ).build_id
    _assert_refused(registry, build_three, upstream=str(first_upstream.id))
