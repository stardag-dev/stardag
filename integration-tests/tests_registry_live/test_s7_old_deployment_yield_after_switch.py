"""S7: an old deployment's execution yields after the switch, and restarts.

A fan-out parent is running under deployment D1 when D2 is deployed. Its
container is D1's and finishes on D1: when it yields, its ``/yield`` names
the old plan and carries D1's deployment id, which matches that plan, so
the registry accepts it (design.md, "Rollover"): the instances and edges
are true facts about scope D1, the membership lands in a plan about to be
superseded, and the parent goes SUSPENDED globally. The worker's suspend
spawns the next tick, which runs D2's code and rolls the build over. The new
plan's instance of the parent has no dynamic edges in scope D2, and
SUSPENDED is ACTIONABLE, so the parent is runnable there and is restarted
**under the new code** -- where it yields again, into the new plan, and
completes once those children have. No detection step, no double
execution: the old yield's children are members of a plan nobody drives.

``test_rollover`` (S3) does not assert the late yield; this scenario
forces it. The order is not a race the test wins: the parent holds its
pre-yield section on a gate, and the scenario releases it only once D2 is
the app's live code *and* the D1 tick that claimed the parent has exited --
so nothing spawns a tick until the parent's own suspend, and that tick is
D2's. The restart under D2 finds the gate open and yields at once. The one
condition it rests on, that D2 was activated before the yield, is read
back off the registry and asserted.

The alternatives this rules out: refusing the old execution's yield (its
work lost, the parent failed), and continuing the old plan on the new code
(D2's code running under D1's scope, the contract ``deployment_mismatch``
exists to keep).
"""

from __future__ import annotations

import uuid
from datetime import datetime

import pytest

from stardag_integration_tests.registry_live._events import (
    describe_events,
    describe_ledger,
    executions_of,
    task_events,
)
from stardag_integration_tests.registry_live._gates import (
    GateSet,
    wait_for_the_ticks_to_exit,
)
from stardag_integration_tests.registry_live._guard import registry_live_guard
from stardag_integration_tests.registry_live._harness import (
    Deployment,
    stop_existing_app,
)
from stardag_integration_tests.registry_live._rollover import (
    ROLLOVER_APP_NAMES,
    app_deployments,
    deploy_rollover_app,
    deployment_of,
    trigger_app,
)
from stardag_integration_tests.registry_live._wait import (
    describe,
    wait_for_task_status,
    wait_for_terminal,
)

registry_live_guard()

pytestmark = [
    pytest.mark.registry_live,
    pytest.mark.timeout(1200),
]

APP_NAME = ROLLOVER_APP_NAMES["S7"]

# The pre-yield section's *upper bound*, reached only if the gate's release
# is lost. The parent holds until the scenario releases it after D2 is
# activated (see ``_gates``); the restarts under D2 -- yield, then
# completion -- find it open. Ungated, this was paid three times and was the
# scenario's long pole in CI.
PRE_YIELD_SECONDS = 110
CHILD_SECONDS = 5
CHILDREN = 2
# The D1 tick's linger, and how long the release waits on top of it for that
# tick to report its exit (``wait_for_the_ticks_to_exit``).
LINGER_SECONDS = 30
TICK_EXIT_MARGIN_SECONDS = 90

STATUS_TIMEOUT_SECONDS = 300
BUILD_TIMEOUT_SECONDS = 900


@pytest.mark.budget(180)
def test_s7_an_old_deployment_yield_lands_and_the_parent_restarts_on_new_code(
    deployment: Deployment, gates: GateSet
) -> None:
    from stardag.registry import registry_provider
    from stardag_integration_tests.registry_live.tasks import (
        ConfiguredFanOut,
        get_sum,
    )

    env = deployment.modal_environment
    code_1, code_2 = uuid.uuid4().hex, uuid.uuid4().hex
    salt = uuid.uuid4().hex
    pre_yield = gates.new("pre-yield", salt=salt)
    parent = ConfiguredFanOut(
        salt=salt,
        children=CHILDREN,
        child_seconds=CHILD_SECONDS,
        pre_yield_seconds=PRE_YIELD_SECONDS,
        pre_yield_gate=pre_yield.key,
    )
    registry = registry_provider.get()

    stop_existing_app(APP_NAME, env)
    deploy_rollover_app(APP_NAME, env, code_id=code_1)
    try:
        build_id = (
            trigger_app(APP_NAME)
            .build_trigger(
                get_sum(integers=parent),
                reactive=True,
                # Short on purpose: no D1 tick may still be lingering when
                # the parent yields, or it would drive the old plan on.
                tick_kwargs={
                    "linger_seconds": LINGER_SECONDS,
                    "poll_interval_seconds": 3,
                },
            )
            .build_id
        )
        wait_for_task_status(
            parent.id,
            expected="running",
            build_id=build_id,
            timeout=STATUS_TIMEOUT_SECONDS,
        )
        old_plan = registry.build_get_frontier(build_id).plan_id
        deployment_1 = deployment_of(APP_NAME, code_1)

        deploy_rollover_app(APP_NAME, env, code_id=code_2)
        deployment_2 = deployment_of(APP_NAME, code_2)

        # D2 is live. Let the parent yield once no D1 tick is left to drive
        # the old plan on: the tick its suspend spawns is then D2's.
        wait_for_the_ticks_to_exit(
            deployment,
            build_id,
            task_id=parent.id,
            bound_seconds=LINGER_SECONDS + TICK_EXIT_MARGIN_SECONDS,
        )
        pre_yield.release()

        status = wait_for_terminal(build_id, timeout=BUILD_TIMEOUT_SECONDS)
        events = task_events(deployment, parent.id)
        rows = executions_of(deployment, parent.id, build_id)
        context = (
            f"{describe(build_id)}\n--- parent events ---\n"
            f"{describe_events(events, build=build_id)}\n--- parent ledger ---\n"
            f"{describe_ledger(rows, build=build_id)}"
        )
        assert status == "completed", context

        # The old execution's yield landed in the old plan, after D2 was live.
        yields = [
            e
            for e in events
            if e.get("event_type") == "task_yielded" and e.get("report_applied")
        ]
        assert yields and str(yields[0]["plan_id"]) == str(old_plan), context
        activated_2 = next(
            d.activated_at for d in app_deployments(APP_NAME) if d.id == deployment_2
        )
        assert activated_2 is not None
        assert datetime.fromisoformat(yields[0]["created_at"]) > activated_2, (
            "The parent yielded before D2 was activated, so the yield was not "
            "'after the switch'. Its gate is released only after the deploy, so "
            "the hold ended early or ran to its bound "
            f"({PRE_YIELD_SECONDS}s) before the deploy landed; the gate lines "
            "in the teardown output say which.\n" + context
        )

        # The build rolled over, and the parent restarted under the new plan.
        final = registry.build_get_frontier(build_id)
        assert final.deployment_id == deployment_2 and final.plan_id != old_plan, (
            context
        )
        # The old execution is the old plan's one; everything after it is the
        # new plan's. The restart yields again, into the new plan -- the old
        # yield's children were members of a plan nobody drives any more --
        # suspends, and completes once they have: so two executions under
        # the new plan, the last one completed.
        old_execution, *restarts = rows
        assert str(old_execution["plan_id"]) == str(old_plan), context
        assert old_execution["outcome"] == "suspended", context
        assert restarts and all(
            str(r["plan_id"]) == str(final.plan_id) for r in restarts
        ), context
        assert restarts[-1]["outcome"] == "completed", context

        # Scope D1 keeps its facts; scope D2 has its own expanded instance.
        by_deployment = {
            i.deployment_id: i for i in registry.task_get(str(parent.id)).instances
        }
        assert {deployment_1, deployment_2} <= set(by_deployment), by_deployment
        assert all(
            by_deployment[d].expanded_at is not None
            for d in (deployment_1, deployment_2)
        ), by_deployment
    finally:
        stop_existing_app(APP_NAME, env)
