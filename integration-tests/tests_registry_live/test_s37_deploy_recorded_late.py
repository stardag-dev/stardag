"""S37: new code whose deployment is not on record cannot take a build over.

``stardag modal deploy`` records the deployment before it deploys and
activates it after (design.md, "The deterministic scope"). If the
activation never lands -- the registry was unreachable at that moment and
the operator ignored the non-zero exit -- the new code is live on Modal but
the registry's current deployment for the app is still the old one. Every
tick of the new code then finds that its own deployment is not current and
exits ``superseded``: rollover only moves forward, and only on the
registry's record (design.md, "Rollover"). The build stalls, visibly --
``stardag modal deployments`` shows the unactivated row -- until the record
is re-sent, which is idempotent by the client-minted id; then the next tick
rolls the build over and it completes.

The alternative this rules out is a tick trusting its own code over the
registry's record: a deploy the registry does not know could then roll
builds forward (or, arriving late, *back*) with no row to say so.

The deploy is the real CLI with its activation step replaced by a no-op --
the operator who ignored the exit -- and nothing else. A never-recorded
deploy is not reachable through the CLI (a failed record stops the deploy
before anything is deployed), and the registry sees the two alike: a
deployment id that is not the app's current one.

The wake-up after the re-send is a watchdog sweep of this scenario's own
app: re-sending a record writes no task status, so nothing flags the build,
and time-based and operator-driven wake-ups are the watchdog's.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from stardag_integration_tests.registry_live._events import (
    describe_ledger,
    ledger,
    spawned_executions,
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
    tick_summaries,
    wait_for_task_status,
    wait_for_terminal,
    wait_until,
)

registry_live_guard()

pytestmark = [
    pytest.mark.registry_live,
    pytest.mark.timeout(1200),
]

APP_NAME = ROLLOVER_APP_NAMES["S37"]

# The unactivated deploy (30-60 s) must land while the middle task runs, so
# the tick its completion wakes is the new code's. The middle task holds on a
# gate released after that deploy (see ``_gates``); this is its upper bound,
# the old window, reached only if the release is lost.
SLOW_SECONDS = 120
# The old code's tick linger, and how long the release waits on top of it
# for that tick to report its exit: one still lingering when the middle
# task finishes would drive the build on itself, and the new code's tick
# would never be woken.
LINGER_SECONDS = 30
TICK_EXIT_MARGIN_SECONDS = 90

STATUS_TIMEOUT_SECONDS = 300
BUILD_TIMEOUT_SECONDS = 600


def _deployments_cli() -> str:
    """``stardag modal deployments --app <app>``, as an operator would read it."""
    stardag_cli = Path(sys.executable).with_name("stardag")
    result = subprocess.run(
        [str(stardag_cli), "modal", "deployments", "--app", APP_NAME],
        capture_output=True,
        text=True,
        env={**os.environ, "COLUMNS": "250"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


@pytest.mark.budget(160)
def test_s37_a_deploy_without_its_record_stalls_until_it_is_resent(
    deployment: Deployment, gates: GateSet
) -> None:
    from stardag.registry import registry_provider
    from stardag_integration_tests.registry_live._deployed import (
        run_watchdog_sweep,
    )
    from stardag_integration_tests.registry_live.tasks import (
        get_range,
        get_sum,
        slow,
    )

    env = deployment.modal_environment
    code_1, code_2 = uuid.uuid4().hex, uuid.uuid4().hex
    salt = uuid.uuid4().hex
    leaf = get_range(limit=3, salt=salt)
    held = gates.new("middle", salt=salt)
    middle = slow(values=leaf, seconds=SLOW_SECONDS, gate=held.key)
    root = get_sum(integers=middle)
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
            middle.id,
            expected="running",
            build_id=build_id,
            timeout=STATUS_TIMEOUT_SECONDS,
        )
        original = registry.build_get_frontier(build_id)
        deployment_1 = deployment_of(APP_NAME, code_1)

        deploy_rollover_app(APP_NAME, env, code_id=code_2, activate=False)
        deployment_2 = deployment_of(APP_NAME, code_2)
        row_2 = next(d for d in app_deployments(APP_NAME) if d.id == deployment_2)
        assert row_2.activated_at is None and not row_2.is_current, row_2
        assert registry.task_get(str(middle.id)).status == "running", (
            "The deploy landed after the middle task finished: its hold ran to "
            f"its {SLOW_SECONDS}s bound first, or ended early; the gate lines "
            "in the teardown output say which."
        )
        wait_for_the_ticks_to_exit(
            deployment,
            build_id,
            task_id=middle.id,
            bound_seconds=LINGER_SECONDS + TICK_EXIT_MARGIN_SECONDS,
        )
        held.release()

        # The middle task finishes on the old code and wakes a tick of the
        # new: it is superseded, and the build stays where it was.
        wait_until(
            lambda: any(
                s.get("outcome") == "superseded" for s in tick_summaries(build_id)
            ),
            build_id=build_id,
            timeout=STATUS_TIMEOUT_SECONDS + SLOW_SECONDS,
            what="a tick of the unrecorded code to exit superseded",
        )
        stalled = registry.build_get_frontier(build_id)
        assert registry.build_get(build_id).status == "running"
        assert stalled.plan_id == original.plan_id
        assert stalled.deployment_id == deployment_1
        assert registry.task_get(str(root.id)).status == "pending", describe(build_id)

        # The gap is visible to an operator.
        listing = _deployments_cli()
        assert str(deployment_2) in listing and str(deployment_1) in listing, listing

        # Re-sending the record is idempotent by the client-minted id.
        recreated = registry.deployment_create(
            kind="modal",
            app_name=APP_NAME,
            code_id=code_2,
            deployment_id=deployment_2,
        )
        assert recreated.id == deployment_2 and not recreated.created, recreated
        assert recreated.generation == row_2.generation, recreated
        first = registry.deployment_activate(deployment_2)
        again = registry.deployment_activate(deployment_2)
        assert first.is_current and again.is_current, (first, again)
        assert again.activated_at == first.activated_at, (first, again)

        run_watchdog_sweep(app_name=APP_NAME, modal_environment=env)
        status = wait_for_terminal(build_id, timeout=BUILD_TIMEOUT_SECONDS)
        assert status == "completed", describe(build_id)
        final = registry.build_get_frontier(build_id)
        assert final.deployment_id == deployment_2, describe(build_id)

        counts = spawned_executions(deployment, build_id)
        assert all(counts.get(str(t.id)) == 1 for t in (leaf, middle, root)), (
            describe_ledger(ledger(deployment, build_id))
        )
    finally:
        stop_existing_app(APP_NAME, env)
