"""S22: a shared instance's yield outlives the build that yielded it.

Two builds hold the same instance of a fan-out parent (one scope, one
body). A runs it to its yield: the parent's instance gets dynamic edges to
its children C, and A's plan admits them. Then A is cancelled, which
releases A's claims on C (they go CANCELLED -- actionable) while the
parent stays SUSPENDED, holding no claim.

B never ran the parent and never yielded anything, yet its plan must gate
on C, because the parent's edges belong to the instance B shares. Every
frontier read starts with a closure step (design.md, "Registration":
"Closure is kept as a mechanism") that admits C into B's plan; B then runs
C itself and completes. v1 ran closure only at stall, and a plan that
gated on members it did not hold is exactly the STA-41 stall.

The alternative this rules out is membership following the yielding build:
with C only in A's plan, B's gate on the parent would reference tasks B's
frontier cannot see, and B would never complete once A stopped wanting C.

Replaces v1's ``test_structure_scope_dynamic``. The observables: B
completed, B's own ledger holds a submitted execution of every child, and
the parent suspended exactly once (B trusted A's yield rather than re-running
the parent's pre-yield section).
"""

from __future__ import annotations

import uuid

import pytest

from stardag_integration_tests.registry_live._events import (
    describe_events,
    describe_ledger,
    spawned_of,
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

# The children must still be RUNNING under A's claims when A is cancelled,
# so that the cancel -- not their own completion -- is what hands them to B:
# B's bootstrap and registration fit inside this with room to spare.
CHILD_SECONDS = 90
PRE_YIELD_SECONDS = 10
CHILDREN = 2

STATUS_TIMEOUT_SECONDS = 300
BUILD_TIMEOUT_SECONDS = 600


def test_s22_closure_admits_a_cancelled_builds_yield(deployment: Deployment) -> None:
    from stardag.registry import registry_provider
    from stardag_integration_tests.registry_live.dag_app import app
    from stardag_integration_tests.registry_live.tasks import (
        ConfiguredFanOut,
        get_sum,
        square,
    )

    salt = uuid.uuid4().hex
    parent = ConfiguredFanOut(
        salt=salt,
        children=CHILDREN,
        child_seconds=CHILD_SECONDS,
        pre_yield_seconds=PRE_YIELD_SECONDS,
    )
    children = parent.child_tasks()

    build_a = app.build_trigger(
        get_sum(integers=parent),
        reactive=True,
        tick_kwargs={"linger_seconds": 60, "poll_interval_seconds": 3},
    ).build_id
    wait_for_task_status(
        parent.id,
        expected="suspended",
        build_id=build_a,
        timeout=STATUS_TIMEOUT_SECONDS,
    )
    for child in children:
        wait_for_task_status(
            child.id,
            expected="running",
            build_id=build_a,
            timeout=STATUS_TIMEOUT_SECONDS,
        )

    build_b = app.build_trigger(
        square(values=parent, offset=5),
        reactive=True,
        tick_kwargs={"linger_seconds": MAX_LINGER_SECONDS, "poll_interval_seconds": 3},
    ).build_id
    wait_until_registered(deployment, task_id=parent.id, build_id=build_b)

    registry = registry_provider.get()
    frontier_a = registry.build_get_frontier(build_a)
    frontier_b = registry.build_get_frontier(build_b)
    assert (frontier_a.deployment_id, frontier_a.settings_hash) == (
        frontier_b.deployment_id,
        frontier_b.settings_hash,
    ), "The two builds must share a scope (and so the parent's instance)."

    registry.build_cancel(build_a)
    still_running = [c.id for c in children if task_status(c.id) == "running"]
    assert not still_running, (
        "A's cancel did not release its claims on the children, so nothing "
        "hands them to B.\n" + describe(build_a)
    )

    status_b = wait_for_terminal(build_b, timeout=BUILD_TIMEOUT_SECONDS)
    assert status_b == "completed", (
        "B did not complete after A withdrew from the children it yielded. B's "
        "closure step should have admitted them and B run them itself.\n"
        f"--- A ---\n{describe(build_a)}\n--- B ---\n{describe(build_b)}"
    )

    for child in children:
        rows = spawned_of(deployment, child.id, build_a, build_b)
        assert any(r["build_id"] == str(build_b) for r in rows), (
            f"B completed without submitting child {child.id} itself, so the "
            "child was not B's to run -- the closure never admitted it.\n"
            + describe_ledger(rows, A=build_a, B=build_b)
        )

    events = task_events(deployment, parent.id)
    suspensions = [e for e in events if e.get("event_type") == "task_suspended"]
    assert len(suspensions) == 1, (
        f"The parent suspended {len(suspensions)} times: B re-ran its "
        "pre-yield section instead of trusting the shared instance's yield.\n"
        + describe_events(events, A=build_a, B=build_b)
    )
