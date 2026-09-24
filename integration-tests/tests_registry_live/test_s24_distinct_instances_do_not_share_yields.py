"""S24: two instances of one completion in one scope do not share yields.

The counterpart of ``test_shared_structure_scope``. There, two builds hold
the *same* instance of a fan-out parent, so the second trusts the first's
dynamic edges and the pre-yield section runs once. Here the two builds
construct the parent with a different **non-significant** field
(``ConfiguredFanOut.children``): one task id, two instance hashes, one
scope. Edges belong to the instance (design.md, ``task_instance``), so B's
instance has no dynamic edges while A's children run; SUSPENDED is
actionable, so B claims the parent and runs its pre-yield section itself.
That is the accepted cost of the second identity (design.md, "What this
costs"): the pre-yield part runs once more, and only identical instances
share yields.

The alternative this rules out is sharing dynamic edges by task id, which
would let B's plan gate on A's four children when B's own construction
yields three -- structure that B's body never declared.

The observable is the parent's event log: exactly **two** suspensions (A's
and B's), where a shared instance has one. The rerun after the children
complete yields children that are all complete, so it does not suspend.
"""

from __future__ import annotations

import uuid

import pytest

from stardag_integration_tests.registry_live._events import (
    describe_events,
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

# A's children must still be running when B's instance is claimed and
# yields, so B's yield lands on children A is progressing (and B's run
# suspends rather than completing): B's bootstrap, its Range, and its
# pre-yield section all fit inside this.
CHILD_SECONDS = 120
PRE_YIELD_SECONDS = 15
A_CHILDREN = 3
B_CHILDREN = 2

STATUS_TIMEOUT_SECONDS = 300
BUILD_TIMEOUT_SECONDS = 700


def test_s24_two_instances_in_one_scope_rerun_the_pre_yield_once(
    deployment: Deployment,
) -> None:
    from stardag.registry import registry_provider
    from stardag_integration_tests.registry_live.dag_app import app
    from stardag_integration_tests.registry_live.tasks import (
        ConfiguredFanOut,
        get_sum,
        square,
    )

    salt = uuid.uuid4().hex
    parent_a = ConfiguredFanOut(
        salt=salt,
        children=A_CHILDREN,
        child_seconds=CHILD_SECONDS,
        pre_yield_seconds=PRE_YIELD_SECONDS,
    )
    parent_b = ConfiguredFanOut(
        salt=salt,
        children=B_CHILDREN,
        child_seconds=CHILD_SECONDS,
        pre_yield_seconds=PRE_YIELD_SECONDS,
    )
    assert parent_a.id == parent_b.id, "the width must not be significant"
    tick_kwargs = {"linger_seconds": MAX_LINGER_SECONDS, "poll_interval_seconds": 3}

    build_a = app.build_trigger(
        get_sum(integers=parent_a), reactive=True, tick_kwargs=tick_kwargs
    ).build_id
    wait_for_task_status(
        parent_a.id,
        expected="suspended",
        build_id=build_a,
        timeout=STATUS_TIMEOUT_SECONDS,
    )

    build_b = app.build_trigger(
        square(values=parent_b, offset=5), reactive=True, tick_kwargs=tick_kwargs
    ).build_id
    wait_until_registered(deployment, task_id=parent_a.id, build_id=build_b)

    registry = registry_provider.get()
    frontier_a = registry.build_get_frontier(build_a)
    frontier_b = registry.build_get_frontier(build_b)
    assert (frontier_a.deployment_id, frontier_a.settings_hash) == (
        frontier_b.deployment_id,
        frontier_b.settings_hash,
    ), "The two builds must share a scope for this scenario to mean anything."

    for build_id in (build_a, build_b):
        status = wait_for_terminal(build_id, timeout=BUILD_TIMEOUT_SECONDS)
        assert status == "completed", (
            f"--- A ---\n{describe(build_a)}\n--- B ---\n{describe(build_b)}"
        )

    # Two constructions of one completion in one scope: two instances.
    instances = [
        i
        for i in registry.task_get(str(parent_a.id)).instances
        if (i.deployment_id, i.settings_hash)
        == (frontier_a.deployment_id, frontier_a.settings_hash)
    ]
    assert len({i.instance_hash for i in instances}) == 2, instances
    assert all(i.expanded_at is not None for i in instances), instances

    events = task_events(deployment, parent_a.id)
    suspensions = [e for e in events if e.get("event_type") == "task_suspended"]
    described = describe_events(events, A=build_a, B=build_b)
    assert len(suspensions) == 2, (
        f"The parent suspended {len(suspensions)} times. Two distinct "
        "instances in one scope must not share dynamic edges, so B's instance "
        "runs its own pre-yield section and suspends once too: exactly two.\n"
        f"--- events on the parent ---\n{described}"
    )
    assert {str(e.get("build_id")) for e in suspensions} == {
        str(build_a),
        str(build_b),
    }, f"Each build's instance should have suspended once.\n{described}"
    assert task_status(parent_a.id) == "completed"
