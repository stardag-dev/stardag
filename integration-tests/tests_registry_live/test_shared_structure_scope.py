"""Two builds in one structure scope share discovered structure.

The reason the scope is the deployment plus settings rather than the build.
When a second build registers a fan-out parent that a first build has
already run to its yield, the second build trusts the first's dynamic edges:
it is gated on the same children, admits them into its own plan when it
stalls, waits on or runs them, and completes — without ever re-running the
parent's pre-yield section. Under per-build edges every collaborating build
would re-execute that section once per yield stage.

The observable is the parent's own event log: exactly one suspension. A
second pre-yield run would suspend a second time. The second build's resets
on the parent are asserted empty too, since a reset is the other way a
pre-yield run could be provoked.
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
from stardag_integration_tests.registry_live._scenario_app import MAX_LINGER_SECONDS
from stardag_integration_tests.registry_live._wait import (
    describe,
    task_status,
    wait_for_task_status,
    wait_for_terminal,
)

registry_live_guard()

pytestmark = [
    pytest.mark.registry_live,
    pytest.mark.timeout(900),
]

# B has to register while the parent is RUNNING and before it yields, so
# that B's plan does not yet contain the children and the stall-time
# closure is what brings them in.
PRE_YIELD_SECONDS = 45
CHILD_SECONDS = 35

STATUS_TIMEOUT_SECONDS = 300
BUILD_TIMEOUT_SECONDS = 600


def test_a_scope_mate_reuses_the_parents_yield(deployment: Deployment) -> None:
    from stardag.registry import registry_provider
    from stardag_integration_tests.registry_live.dag_app import app
    from stardag_integration_tests.registry_live.tasks import (
        ConfiguredFanOut,
        get_sum,
        square,
    )

    salt = uuid.uuid4().hex
    parent = ConfiguredFanOut(
        salt=salt, child_seconds=CHILD_SECONDS, pre_yield_seconds=PRE_YIELD_SECONDS
    )

    build_a = app.build_trigger(
        get_sum(integers=parent),
        reactive=True,
        tick_kwargs={"linger_seconds": 240, "poll_interval_seconds": 3},
    ).build_id
    wait_for_task_status(
        parent.id, expected="running", build_id=build_a, timeout=STATUS_TIMEOUT_SECONDS
    )

    build_b = app.build_trigger(
        square(values=parent, offset=5),
        reactive=True,
        tick_kwargs={"linger_seconds": MAX_LINGER_SECONDS, "poll_interval_seconds": 3},
    ).build_id
    wait_until_registered(deployment, task_id=parent.id, build_id=build_b)
    status_when_registered = task_status(parent.id)
    assert status_when_registered == "running", (
        f"Build B registered when the parent was already {status_when_registered!r}; "
        f"raise PRE_YIELD_SECONDS ({PRE_YIELD_SECONDS}s).\n" + describe(build_b)
    )

    # One scope: both builds' active plans are under the same deployment
    # and the same settings, so the instances (and their edges) are shared.
    registry = registry_provider.get()
    frontier_a = registry.build_get_frontier(build_a)
    frontier_b = registry.build_get_frontier(build_b)
    assert (frontier_a.deployment_id, frontier_a.settings_hash) == (
        frontier_b.deployment_id,
        frontier_b.settings_hash,
    ), (frontier_a, frontier_b)

    status_a = wait_for_terminal(build_a, timeout=BUILD_TIMEOUT_SECONDS)
    assert status_a == "completed", describe(build_a)
    status_b = wait_for_terminal(build_b, timeout=BUILD_TIMEOUT_SECONDS)
    assert status_b == "completed", (
        "Build B did not complete.\n"
        f"--- build A ---\n{describe(build_a)}\n--- build B ---\n{describe(build_b)}"
    )

    events = task_events(deployment, parent.id)
    suspensions = [e for e in events if e.get("event_type") == "task_suspended"]
    assert len(suspensions) == 1, (
        f"The parent suspended {len(suspensions)} times, so its pre-yield "
        "section ran more than once across two builds in one scope.\n"
        f"--- events on the parent ---\n{describe_events(events, A=build_a, B=build_b)}"
    )
    assert not resets_by(events, build_b), (
        "Build B reset the parent while build A was progressing its children.\n"
        f"--- events on the parent ---\n{describe_events(events, A=build_a, B=build_b)}"
    )
