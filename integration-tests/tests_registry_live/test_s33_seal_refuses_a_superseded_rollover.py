"""S33: two new deployments both roll a build over; only the latest wins.

A tick under D2 decides to roll a build over (D2 is the app's current
deployment), registers a plan under D2 -- and before it seals, D3 is
deployed and activated. The seal re-checks, under the app's advisory lock,
that the plan's deployment is still the app's current one (design.md,
"Rollover", step 3), so D2's seal is refused and D2's tick exits
``superseded``; D3's tick rolls over, seals, and supersedes the old plan.
Rollover only moves forward, and "forward" is decided on the registry's
record when the seal lands, not when the tick started.

The alternative this rules out is a currency check made only at the start
of the rollover: then D2's seal would activate a plan under code that is no
longer live, and the build would run on the older of two new deployments.

**How the race is produced.** Two ticks under two live deployments at once
cannot be timed from outside Modal -- a deploy replaces the live code, so a
D2 container exists only until D3 lands. So D2's tick is run *here*: the
SDK's own rollover step (``roll_over_aio``) with D2's deployment id, the
same code D2's container runs, against the real registry. The one thing
arranged is *when* D3 appears: the registry handle given to it deploys D3
for real, activation included, immediately before forwarding the seal.
Everything after that -- the refusal, D3's real tick on Modal, the
supersession -- is the system's own.
"""

from __future__ import annotations

import asyncio
import typing
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

APP_NAME = ROLLOVER_APP_NAMES["S33"]

# Two deploys (30-60 s each) and D2's local walk must fit while the middle
# task is RUNNING: its completion is what wakes D3's tick, and nothing may
# wake a tick on Modal before D3 is live.
SLOW_SECONDS = 240

STATUS_TIMEOUT_SECONDS = 300
BUILD_TIMEOUT_SECONDS = 900


class _DeployBeforeSeal:
    """A registry handle that deploys D3 just before forwarding ``/seal``.

    Delegates everything else unchanged. Records what the seal it
    forwarded answered, so the scenario asserts on the registry's refusal
    rather than on the rollover's interpretation of it.
    """

    def __init__(self, inner, deploy_d3: typing.Callable[[], None]):
        self._inner = inner
        self._deploy_d3 = deploy_d3
        self.sealed_plan_id = None
        self.seal_refusal: str | None = None

    def __getattr__(self, name: str):
        return getattr(self._inner, name)

    async def plan_seal_aio(self, plan_id, *args, **kwargs):
        from stardag.exceptions import APIError

        self.sealed_plan_id = plan_id
        await asyncio.to_thread(self._deploy_d3)
        try:
            return await self._inner.plan_seal_aio(plan_id, *args, **kwargs)
        except APIError as e:
            self.seal_refusal = e.code
            raise


def test_s33_the_older_of_two_rollovers_is_refused_at_seal(
    deployment: Deployment,
) -> None:
    from stardag.build._reactive._rollover import roll_over_aio
    from stardag.registry import RegistryABC, registry_provider
    from stardag_integration_tests.registry_live.tasks import (
        get_range,
        get_sum,
        slow,
    )

    env = deployment.modal_environment
    code_1, code_2, code_3 = (uuid.uuid4().hex for _ in range(3))
    salt = uuid.uuid4().hex
    middle = slow(values=get_range(limit=3, salt=salt), seconds=SLOW_SECONDS)
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
        original = registry.build_get_frontier(build_id)

        deploy_rollover_app(APP_NAME, env, code_id=code_2)
        deployment_2 = deployment_of(APP_NAME, code_2)

        # D2's tick, from its rollover step on; D3 lands before its seal.
        handle = _DeployBeforeSeal(
            registry,
            lambda: deploy_rollover_app(APP_NAME, env, code_id=code_3),
        )
        outcome = asyncio.run(
            roll_over_aio(
                typing.cast(RegistryABC, handle),
                registry.build_get_frontier(build_id),
                own_deployment_id=deployment_2,
            )
        )
        deployment_3 = deployment_of(APP_NAME, code_3)
        assert handle.seal_refusal in ("deployment_not_current", "plan_superseded"), (
            f"D2's seal was not refused (answer: {handle.seal_refusal!r}) after "
            "D3 became current.\n" + describe(build_id)
        )
        assert outcome == "superseded", outcome
        assert registry.task_get(str(middle.id)).status == "running", (
            f"The middle task finished before the race was staged; raise "
            f"SLOW_SECONDS ({SLOW_SECONDS}s)."
        )

        status = wait_for_terminal(build_id, timeout=BUILD_TIMEOUT_SECONDS)
        assert status == "completed", describe(build_id)
        final = registry.build_get_frontier(build_id)
        assert final.deployment_id == deployment_3, (
            f"The build ended under {final.deployment_id}; the latest deployment "
            f"({deployment_3}) must win.\n{describe(build_id)}"
        )
        assert final.plan_id not in (original.plan_id, handle.sealed_plan_id)

        counts = spawned_executions(deployment, build_id)
        assert counts.get(str(middle.id)) == 1 and counts.get(str(root.id)) == 1, (
            describe_ledger(ledger(deployment, build_id))
        )
    finally:
        stop_existing_app(APP_NAME, env)
