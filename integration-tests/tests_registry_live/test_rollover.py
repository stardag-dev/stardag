"""A running build follows the live deployment.

A build is not bound to the code that planned it. Modal keeps one live
deployment per app name, and a redeploy sends every *new* spawn to the new
code — so the first tick that runs on the new deployment must **re-plan**
the build under its own deployment and drive on, not refuse it and leave it
stalled (design.md, "Rollover").

The scenario deploys its own app twice under two code ids, ``A`` and
``B`` (``STARDAG_CODE_ID`` in the deploy's environment, the same override a
CI image uses; ``stardag modal deploy`` records a deployment for each),
triggers a reactive build on ``A`` whose fan-out parent has a long
pre-yield, and redeploys as ``B`` while the parent is still running. What
it then asserts is the rollover as the design states it:

- the build completes;
- its active plan is now under deployment ``B`` (same build id);
- the parent has an expanded instance under ``B`` -- the new plan
  registered it in its own scope; the ``A`` instance is not retracted;
- the registry lists both deployments, ``B`` current.

Every one of those is **durable registry state**, and deliberately so.
The scenario used to also require that some tick *reported* rolling over,
which is the thing a preempted tick cannot do: on 2026-09-21 a tick rolled
this build over and was killed 53 s later, so the rollover was correct and
the assertion failed anyway. Worse than the flake was what it cost the
test's meaning — a genuine rollover regression and a dead reporter failed
in exactly the same words, so a real regression would have read as the
known flake. See ``_rollover_reports`` and STA-87.

An old worker's late yield landing in scope ``A`` is the other half of the
rule (its ``/yield`` names the old plan). It is not forced here: it needs the
redeploy to land inside the parent's pre-yield window *and* the parent's
worker to yield before the new tick re-plans, which is a race this tier
cannot time. The worker side is covered by unit tests
(``TestWorkerScope`` in the SDK suite); this scenario proves the tick side
against a real deployment.

Each deploy takes 30-60 s of the budget; the image is the one the other
scenario apps already built.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from stardag_integration_tests.registry_live._guard import registry_live_guard
from stardag_integration_tests.registry_live._harness import (
    Deployment,
    stop_existing_app,
)
from stardag_integration_tests.registry_live._wait import (
    describe,
    tick_summaries,
    wait_for_task_status,
    wait_for_terminal,
)

registry_live_guard()

pytestmark = [
    pytest.mark.registry_live,
    pytest.mark.timeout(1200),
]

# Long enough that the redeploy lands while the parent is still in its
# pre-yield section: two deploys' worth of margin on top of a container
# start.
PRE_YIELD_SECONDS = 150
CHILD_SECONDS = 5

STATUS_TIMEOUT_SECONDS = 300
BUILD_TIMEOUT_SECONDS = 900

APP_MODULE = "stardag_integration_tests.registry_live.rollover_app"


def _deploy(app_name: str, modal_environment: str, code_id: str) -> None:
    """Deploy the rollover app as ``code_id``, with the CLI of this venv.

    Same shape as ``provision._deploy_dag_apps``, for the same reason: the
    app's functions are serialised by the interpreter that imports the
    module, and every trigger unpickles them in a container built for that
    Python. ``STARDAG_CODE_ID`` is the deploy's code identity — baked into
    the deployment, so every container of it answers ``code_id``.
    """
    stardag_cli = Path(sys.executable).with_name("stardag")
    assert stardag_cli.exists(), f"No stardag CLI next to {sys.executable}"
    result = subprocess.run(
        [str(stardag_cli), "modal", "deploy", "-m", APP_MODULE],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "MODAL_ENVIRONMENT": modal_environment,
            "STARDAG_CODE_ID": code_id,
        },
    )
    assert result.returncode == 0, (
        f"Deploy of {app_name!r} as {code_id[:12]} failed:\n"
        f"{result.stdout}\n{result.stderr}"
    )


def _deployment_ids(app_name: str) -> dict[str, tuple[str, bool]]:
    """``code_id -> (deployment id, is_current)`` for the app's deployments."""
    from stardag.registry import registry_provider

    return {
        d.code_id: (str(d.id), d.is_current)
        for d in registry_provider.get().deployment_list(
            kind="modal", app_name=app_name
        )
    }


def _rollover_reports(summaries: list[dict]) -> str:
    """What the ticks said about rolling over, for a failure message.

    A diagnostic and never an assertion. A tick summary only exists if the
    tick that would have written it survived long enough to, so its
    absence is evidence about the reporter and not about the rollover --
    which is exactly the confusion this scenario was built on.
    """
    reported = sum(1 for summary in summaries if summary.get("rolled_over"))
    return (
        f"  [diagnostic] {reported} of {len(summaries)} retained tick "
        f"summaries report rolling over. Zero is not evidence against the "
        f"rollover: a tick that rolled the build over and was then "
        f"preempted leaves none."
    )


def test_a_running_build_rolls_over_to_the_new_deployment(
    deployment: Deployment,
) -> None:
    from stardag.registry import registry_provider
    from stardag_integration_tests.registry_live.rollover_app import APP_NAME, app
    from stardag_integration_tests.registry_live.tasks import (
        ConfiguredFanOut,
        get_sum,
    )

    modal_environment = deployment.modal_environment
    code_a, code_b = uuid.uuid4().hex, uuid.uuid4().hex
    stop_existing_app(APP_NAME, modal_environment)
    _deploy(APP_NAME, modal_environment, code_a)
    try:
        salt = uuid.uuid4().hex
        parent = ConfiguredFanOut(
            salt=salt,
            child_seconds=CHILD_SECONDS,
            pre_yield_seconds=PRE_YIELD_SECONDS,
        )
        root = get_sum(integers=parent)
        build_id = app.build_trigger(
            root,
            reactive=True,
            tick_kwargs={"linger_seconds": 60, "poll_interval_seconds": 3},
        ).build_id
        wait_for_task_status(
            parent.id,
            expected="running",
            build_id=build_id,
            timeout=STATUS_TIMEOUT_SECONDS,
        )
        registry = registry_provider.get()
        deployment_a, _ = _deployment_ids(APP_NAME)[code_a]
        planned_under = str(registry.build_get_frontier(build_id).deployment_id)
        assert planned_under == deployment_a, (
            f"The bootstrap did not plan the build under deployment A "
            f"({deployment_a}): {planned_under}\n" + describe(build_id)
        )

        # New code takes over the app name while the parent runs. Its
        # in-flight container finishes on A; every spawn from here on lands
        # on B, including the tick that the parent's completion wakes.
        _deploy(APP_NAME, modal_environment, code_b)

        status = wait_for_terminal(build_id, timeout=BUILD_TIMEOUT_SECONDS)
        assert status == "completed", (
            "The build did not complete after the redeploy. A tick on the new "
            "code should have re-planned it under its own scope and driven "
            "it on.\n" + describe(build_id)
        )

        # The rollover itself, as registry state: *this* build -- the same
        # build id, re-planned in place -- has its active plan under
        # deployment B. Nothing but a rollover produces that, and no tick
        # has to survive for it to be true.
        summaries = tick_summaries(build_id)
        deployment_b, _ = _deployment_ids(APP_NAME)[code_b]
        active_under = str(registry.build_get_frontier(build_id).deployment_id)
        assert active_under == deployment_b, (
            f"The build's active plan did not move to deployment B "
            f"({deployment_b}): {active_under}. The first tick to run on the "
            f"new deployment should have re-planned it under its own "
            f"deployment.\n" + _rollover_reports(summaries) + "\n" + describe(build_id)
        )

        # The one report this still reads, and the direction matters: a
        # tick *saying* the rollover failed is a real event, so requiring
        # its absence can only fail on real evidence. Requiring a good
        # report's presence is the opposite -- it fails when the reporter
        # dies, which is STA-87.
        assert not any(s.get("outcome") == "rollover_failed" for s in summaries), (
            "A tick reported that the rollover failed.\n" + describe(build_id)
        )

        # The new plan registered the parent in its own scope: an expanded
        # instance (its upstreams declared) under deployment B, beside the
        # one under A, which nothing retracts. The edges themselves have no
        # read route yet (the graph over instance edges is I4); the instance
        # is where the scope a build planned under is visible.
        instances = registry.task_get(str(parent.id)).instances
        by_deployment = {str(i.deployment_id): i for i in instances}
        assert deployment_b in by_deployment, (
            f"The parent has no instance under deployment B: "
            f"{sorted(by_deployment)}\n"
            + _rollover_reports(summaries)
            + "\n"
            + describe(build_id)
        )
        assert by_deployment[deployment_b].expanded_at is not None, (
            "The parent's instance under B was never expanded, so the new "
            "plan did not discover its upstreams.\n" + describe(build_id)
        )
        assert deployment_a in by_deployment, (
            "The parent's instance under A is gone; nothing retracts an "
            "instance.\n" + describe(build_id)
        )

        # Both deployments are on record, B current.
        recorded = _deployment_ids(APP_NAME)
        assert set(recorded) >= {code_a, code_b}, recorded
        assert recorded[code_b][1] and not recorded[code_a][1], recorded
    finally:
        # Leave nothing warm on an app only this scenario deploys.
        stop_existing_app(APP_NAME, modal_environment)
