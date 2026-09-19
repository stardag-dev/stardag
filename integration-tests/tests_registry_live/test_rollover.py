"""A running build follows the live deployment.

The rule the whole structure-scope design turned on at the end: a build is
not bound to the code that planned it. Modal keeps one live deployment per
app name, and a redeploy sends every *new* spawn to the new code — so the
first tick that runs on the new deployment must **re-plan** the build under
its own scope and drive on, not refuse it and leave it stalled.

The scenario deploys its own app twice under two code ids, ``A`` and
``B`` (``STARDAG_CODE_ID`` in the deploy's environment, the same override a
CI image uses), triggers a reactive build on ``A`` whose fan-out parent
has a long pre-yield, and redeploys as ``B`` while the parent is still
running. What it then asserts is the rollover as the design states it:

- the build completes;
- its scope now names ``B``, and a tick summary says it rolled over;
- the parent's edges under ``A`` are still there — nothing retracts an
  edge, the rolled-over build simply reads a different scope — and the
  build's gating read ``B``;
- the registry lists both deployments, ``B`` current.

An old worker's late yield landing in scope ``A`` is the other half of the
rule (``_runner.worker_scope_key``). It is not forced here: it needs the
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

import httpx
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


def _edges_into(deployment: Deployment, task_id: str) -> list[dict]:
    """Every recorded edge into ``task_id``, with the scope each carries."""
    with httpx.Client(timeout=60.0) as client:
        response = client.post(
            f"{deployment.api_url.rstrip('/')}/api/v1/tasks/graph",
            headers={"X-API-Key": deployment.api_key},
            json={"task_ids": [task_id], "upstream_depth": 1},
        )
        response.raise_for_status()
        return [e for e in response.json()["edges"] if e["target"] == task_id]


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
        scope_a = registry.build_get(build_id).scope_key
        assert scope_a is not None and scope_a.startswith(code_a), (
            f"The bootstrap did not plan the build under code A: {scope_a!r}\n"
            + describe(build_id)
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

        scope_b = registry.build_get(build_id).scope_key
        assert scope_b is not None and scope_b.startswith(code_b), (
            f"The build's scope did not move to code B: {scope_b!r}\n"
            + describe(build_id)
        )
        summaries = tick_summaries(build_id)
        assert any(s.get("rolled_over") for s in summaries), (
            "No tick reported rolling the build over, so it completed by some "
            "other route than the one under test.\n" + describe(build_id)
        )
        assert not any(s.get("outcome") == "rollover_failed" for s in summaries)

        # Nothing retracts an edge: the parent's static edge under A is still
        # recorded, and the build's plan under B recorded its own.
        scopes_into_parent = {
            e["scope_key"] for e in _edges_into(deployment, str(parent.id))
        }
        assert scope_a in scopes_into_parent, (
            f"The edges recorded under code A are gone: {scopes_into_parent}"
        )
        assert scope_b in scopes_into_parent, (
            f"The rollover recorded no edges under code B: {scopes_into_parent}"
        )

        # Both deployments are on record, B current.
        recorded = registry.deployment_list(app_name=APP_NAME)
        by_code = {d.code_id: d for d in recorded}
        assert set(by_code) >= {code_a, code_b}, recorded
        assert by_code[code_b].current and not by_code[code_a].current, recorded
    finally:
        # Leave nothing warm on an app only this scenario deploys.
        stop_existing_app(APP_NAME, modal_environment)
