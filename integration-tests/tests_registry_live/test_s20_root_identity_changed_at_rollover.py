"""S20: new code that changes what a build asked for fails the build.

A build is one request, recorded at completion-id level as
``build.root_task_ids``. Rollover re-plans the request under new code by
rehydrating the active plan's root bodies and recomputing their task ids
(design.md, "Rollover", step 2). If the new code computes different ids --
here, the root's class gained a significant field with a default, so the
old body rehydrates into a different completion -- the new code is not
serving the same request. The build is failed with "re-trigger it as a new
build" rather than silently re-planned onto roots nobody asked for.

The second deploy bakes ``REGISTRY_LIVE_ROOT_VARIANT=renamed`` into its
image, which is the harness's stand-in for that code change (see
``tasks.RolloverRoot``). It lands while the root's upstream is RUNNING;
the upstream's worker finishes on the old code and its completion wakes the
first tick of the new code, which is the one that must refuse.

The alternative this rules out is a rollover that trusts the stored root
bodies' ids, or re-plans whatever the new code builds from them: the build
would then complete a different output under the old build's name.
"""

from __future__ import annotations

import uuid

import pytest

from stardag_integration_tests.registry_live._events import _get
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

APP_NAME = ROLLOVER_APP_NAMES["S20"]

# The second deploy (30-60 s) must land while the root's upstream runs, so
# that the first tick after it is the new code's.
UPSTREAM_SECONDS = 150

STATUS_TIMEOUT_SECONDS = 300
BUILD_TIMEOUT_SECONDS = 900


def test_s20_a_root_whose_id_changes_under_new_code_fails_the_build(
    deployment: Deployment,
) -> None:
    from stardag.registry import registry_provider
    from stardag_integration_tests.registry_live.tasks import RolloverRoot

    env = deployment.modal_environment
    root = RolloverRoot(salt=uuid.uuid4().hex, seconds=UPSTREAM_SECONDS)
    upstream = root.requires()
    registry = registry_provider.get()

    stop_existing_app(APP_NAME, env)
    deploy_rollover_app(APP_NAME, env, code_id=uuid.uuid4().hex)
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
            upstream.id,
            expected="running",
            build_id=build_id,
            timeout=STATUS_TIMEOUT_SECONDS,
        )
        deploy_rollover_app(
            APP_NAME, env, code_id=uuid.uuid4().hex, root_variant="renamed"
        )
        assert registry.task_get(str(upstream.id)).status == "running", (
            f"The second deploy landed after the upstream finished; raise "
            f"UPSTREAM_SECONDS ({UPSTREAM_SECONDS}s)."
        )

        status = wait_for_terminal(build_id, timeout=BUILD_TIMEOUT_SECONDS)
        assert status == "failed", (
            "A rollover onto code that computes a different root id must fail "
            f"the build, not re-plan it.\n{describe(build_id)}"
        )
        events = _get(deployment, f"builds/{build_id}/events").json()["events"]
        failures = [e for e in events if e["event_type"] == "build_failed"]
        assert failures, events
        message = str(failures[-1].get("error_message") or "")
        assert "Re-trigger it as a new build" in message, message
        assert "root" in message, message

        # The request is untouched: the build still names the old root.
        assert registry.build_get(build_id).root_task_ids == [str(root.id)]
    finally:
        stop_existing_app(APP_NAME, env)
