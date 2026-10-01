"""S38: a task spawned by deployment D1's tick runs under D2, which added a field.

Workers are resolved by name, which is always the app's *current*
deployment. So a D1 tick still lingering when D2 goes live spawns its next
task onto D2's workers: D1 built the task, D2's code runs it. D2 added a
defaulted field (with a compat default, so no task id changed) to a nested
parameter model the task reads. Sent as a D1 pickle, the field is absent on
the D2 worker and the read raises ``AttributeError``; sent as its instance
body, the worker rehydrates it in compat mode and the field takes its
default (STA-124).

The order is held, not raced. The root's upstream holds on a gate. Once it is
RUNNING, D2 is deployed and serving, and only then is the gate released. The
D1 tick lingers holding the build's scheduler lease all along, so the tick
the upstream's completion spawns -- D2's -- is refused the lease, flags the
build, and exits: it is the D1 tick that claims the root, under D1's plan,
and spawns it. Two facts are read back to show that path was taken rather
than assumed: the root's execution belongs to D1's plan (no rollover ran
first), and its body ran D2's code (it recorded the variant it saw).
"""

from __future__ import annotations

import uuid

import pytest

from stardag_integration_tests.registry_live._events import (
    describe_ledger,
    executions_of,
)
from stardag_integration_tests.registry_live._gates import GateSet, observed
from stardag_integration_tests.registry_live._guard import registry_live_guard
from stardag_integration_tests.registry_live._harness import (
    Deployment,
    stop_existing_app,
)
from stardag_integration_tests.registry_live._rollover import (
    ROLLOVER_APP_NAMES,
    deploy_rollover_app,
    trigger_app,
)
from stardag_integration_tests.registry_live._scenario_app import MAX_LINGER_SECONDS
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

APP_NAME = ROLLOVER_APP_NAMES["S38"]

# The upstream's hold, as an upper bound only: the scenario releases it once
# D2 serves. Kept under the D1 tick's linger, so that a lost release still
# completes the upstream while that tick holds the lease.
UPSTREAM_BOUND_SECONDS = 180
# As long as the deployed tick can honour: the D1 tick must still hold the
# lease when the upstream completes, which is after a whole redeploy.
LINGER_SECONDS = MAX_LINGER_SECONDS

STATUS_TIMEOUT_SECONDS = 300
BUILD_TIMEOUT_SECONDS = 600


@pytest.mark.budget(240)
def test_s38_a_task_built_under_d1_runs_under_d2_with_the_added_field_defaulted(
    deployment: Deployment, gates: GateSet
) -> None:
    from stardag.registry import registry_provider
    from stardag_integration_tests.registry_live.tasks import (
        ADDED_FIELD_DEFAULT,
        AddedFieldRoot,
    )

    env = deployment.modal_environment
    code_1, code_2 = uuid.uuid4().hex, uuid.uuid4().hex
    salt = uuid.uuid4().hex
    upstream_gate = gates.new("upstream", salt=salt)
    root = AddedFieldRoot(
        salt=salt, seconds=UPSTREAM_BOUND_SECONDS, gate=upstream_gate.key
    )
    upstream = root.requires()
    registry = registry_provider.get()

    stop_existing_app(APP_NAME, env)
    deploy_rollover_app(APP_NAME, env, code_id=code_1)
    try:
        build_id = (
            trigger_app(APP_NAME)
            .build_trigger(
                root,
                reactive=True,
                tick_kwargs={
                    "linger_seconds": LINGER_SECONDS,
                    "poll_interval_seconds": 3,
                },
            )
            .build_id
        )
        wait_for_task_status(
            upstream.id,
            expected="running",
            build_id=build_id,
            timeout=STATUS_TIMEOUT_SECONDS,
        )
        d1_plan = registry.build_get_frontier(build_id).plan_id

        deploy_rollover_app(APP_NAME, env, code_id=code_2, field_variant="added")
        upstream_gate.release()

        status = wait_for_terminal(build_id, timeout=BUILD_TIMEOUT_SECONDS)
        rows = executions_of(deployment, root.id, build_id)
        seen = observed(env, f"observed-{salt}")
        context = (
            f"{describe(build_id)}\n--- root ledger ---\n"
            f"{describe_ledger(rows, build=build_id)}\n"
            f"--- what the root's run() saw ---\n{seen!r}"
        )

        # The path under test was taken: the root was spawned under D1's
        # plan (by the lingering D1 tick, not after a rollover) ...
        assert rows and str(rows[0]["plan_id"]) == str(d1_plan), (
            "The root was not executed under D1's plan, so no D1 tick spawned "
            "it: the scenario did not reach the cross-deployment path (did the "
            f"D1 tick linger out before the {LINGER_SECONDS}s window?).\n" + context
        )
        # ... and its body ran D2's code.
        assert seen is not None and seen.get("variant") == "added", (
            "The root's run() did not run D2's code, so the D1 spawn reached a "
            "D1 container: the scenario did not reach the cross-deployment "
            "path.\n" + context
        )

        # What STA-124 fixes: the field D2 added is there, at its default,
        # and the build completed rather than failing on AttributeError.
        assert status == "completed", context
        assert seen == {"variant": "added", "base": 1, "added": ADDED_FIELD_DEFAULT}, (
            context
        )
    finally:
        stop_existing_app(APP_NAME, env)
