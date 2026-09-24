"""S6: a redeploy of unchanged code is a new scope, and changes nothing else.

Every ``stardag modal deploy`` mints a deployment id (D6), whatever the code:
the deployment is the unit the registry can *know*, where "the code did not
change" is a claim it cannot check. So a redeploy of the same code id is a
new deployment row, a new scope, and a rollover of the app's running
reactive builds -- cheap, since every body is identical, and with no change
in behaviour: nothing runs twice.

The alternative this rules out is keying deployments on code id (v1's
``family--<code_id>`` idea): a redeploy that changed the image, the
environment or a dependency under an unchanged commit would then share a
scope with code it does not match.

The scenario deploys its own app twice under one code id, redeploying while
the chain's middle task is RUNNING. Observables: two deployment rows with the
same code id, the later current; the build's active plan under the second;
the walked instances re-registered under it with identical hashes; every
task run exactly once.
"""

from __future__ import annotations

import uuid

import pytest

from stardag_integration_tests.registry_live._events import (
    describe_ledger,
    ledger,
    spawned_executions,
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

APP_NAME = ROLLOVER_APP_NAMES["S6"]

# The redeploy (30-60 s) must land while the middle task is RUNNING, so the
# rollover happens mid-build rather than after it.
SLOW_SECONDS = 150

STATUS_TIMEOUT_SECONDS = 300
BUILD_TIMEOUT_SECONDS = 900


def test_s6_a_redeploy_of_unchanged_code_replans_without_rerunning(
    deployment: Deployment,
) -> None:
    from stardag.registry import registry_provider
    from stardag_integration_tests.registry_live.tasks import (
        get_range,
        get_sum,
        slow,
    )

    env = deployment.modal_environment
    code_id = uuid.uuid4().hex
    salt = uuid.uuid4().hex
    leaf = get_range(limit=3, salt=salt)
    middle = slow(values=leaf, seconds=SLOW_SECONDS)
    root = get_sum(integers=middle)
    registry = registry_provider.get()

    stop_existing_app(APP_NAME, env)
    deploy_rollover_app(APP_NAME, env, code_id=code_id)
    try:
        build_id = (
            trigger_app(APP_NAME)
            .build_trigger(
                root,
                reactive=True,
                tick_kwargs={"linger_seconds": 30, "poll_interval_seconds": 3},
            )
            .build_id
        )
        wait_for_task_status(
            middle.id,
            expected="running",
            build_id=build_id,
            timeout=STATUS_TIMEOUT_SECONDS,
        )
        first = registry.build_get_frontier(build_id)

        deploy_rollover_app(APP_NAME, env, code_id=code_id)
        middle_status = registry.task_get(str(middle.id)).status
        assert middle_status == "running", (
            f"The redeploy landed after the middle task finished ({middle_status}); "
            f"raise SLOW_SECONDS ({SLOW_SECONDS}s)."
        )

        status = wait_for_terminal(build_id, timeout=BUILD_TIMEOUT_SECONDS)
        assert status == "completed", describe(build_id)

        rows = app_deployments(APP_NAME)
        assert len(rows) == 2 and {r.code_id for r in rows} == {code_id}, rows
        newer, older = rows
        assert newer.is_current and not older.is_current, rows
        assert older.id == first.deployment_id, (first.deployment_id, rows)

        final = registry.build_get_frontier(build_id)
        assert final.deployment_id == newer.id, (
            "The build did not roll over to the redeploy: a new deployment is a "
            f"new scope even with the code unchanged.\n{describe(build_id)}"
        )
        assert final.plan_id != first.plan_id

        # Re-registered, identically: the walked instances exist under both
        # deployments with one body, hence one hash.
        for task in (middle, root):
            by_deployment = {
                i.deployment_id: i.instance_hash
                for i in registry.task_get(str(task.id)).instances
            }
            assert {older.id, newer.id} <= set(by_deployment), by_deployment
            assert by_deployment[older.id] == by_deployment[newer.id], by_deployment

        # No behaviour change: nothing ran twice.
        counts = spawned_executions(deployment, build_id)
        assert all(counts.get(str(t.id)) == 1 for t in (leaf, middle, root)), (
            describe_ledger(ledger(deployment, build_id))
        )
    finally:
        stop_existing_app(APP_NAME, env)
