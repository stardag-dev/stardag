"""S26: a local driver with Modal workers plans under the app's deployment.

A hybrid ``sd.build()`` -- the driver in this test process, every task on a
deployed app's Modal workers -- is not itself a deployment. It plans under
**the app's current deployment**, read from the registry at start (D13,
design.md, "A driver that is not the deployment"), so the workers never
yield into a scope that is not their own: ``/yield`` carries the worker's
baked ``STARDAG_DEPLOYMENT_ID`` and is refused (409 ``deployment_mismatch``)
unless it is the plan's. That the laptop's code matches the deployment is
on the user; this scenario runs the same checkout on both sides.

The alternative this rules out is the driver planning under a ``local``
deployment of its own: then every worker's yield would be refused and a
generator task could never suspend -- the hybrid path would work only for
DAGs without dynamic dependencies.

``ConfiguredFanOut`` yields, so the scenario exercises the one route where
the two identities meet. Observables: the build's plan is under the dag
app's current deployment, the parent's yield was applied, and the build
completed.
"""

from __future__ import annotations

import uuid

import pytest

from stardag_integration_tests.registry_live._events import (
    describe_events,
    task_events,
)
from stardag_integration_tests.registry_live._guard import registry_live_guard
from stardag_integration_tests.registry_live._harness import Deployment
from stardag_integration_tests.registry_live._wait import describe

registry_live_guard()

pytestmark = [
    pytest.mark.registry_live,
    pytest.mark.timeout(900),
]

CHILDREN = 2
CHILD_SECONDS = 5
PRE_YIELD_SECONDS = 5


def test_s26_a_hybrid_driver_plans_under_the_apps_current_deployment(
    deployment: Deployment,
) -> None:
    import stardag as sd
    from stardag.build import BuildExitStatus
    from stardag.integration.modal import ModalTaskExecutor
    from stardag.registry import registry_provider
    from stardag_integration_tests.registry_live.dag_app import APP_NAME
    from stardag_integration_tests.registry_live.selectors import (
        ALT_WORKER,
        registry_live_worker,
    )
    from stardag_integration_tests.registry_live.tasks import (
        ConfiguredFanOut,
        get_sum,
    )

    salt = uuid.uuid4().hex
    parent = ConfiguredFanOut(
        salt=salt,
        children=CHILDREN,
        child_seconds=CHILD_SECONDS,
        pre_yield_seconds=PRE_YIELD_SECONDS,
    )
    registry = registry_provider.get()
    current = [
        d
        for d in registry.deployment_list(kind="modal", app_name=APP_NAME, current=True)
        if d.app_name == APP_NAME
    ]
    assert len(current) == 1, current

    summary = sd.build(
        get_sum(integers=parent),
        task_executor=ModalTaskExecutor(
            modal_app_name=APP_NAME,
            worker_selector=registry_live_worker,
            worker_timeouts={"default": 600, ALT_WORKER: 600},
        ),
        description="S26 hybrid driver",
    )
    build_id = summary.build_id
    assert build_id is not None
    assert summary.status == BuildExitStatus.SUCCESS, f"{summary}\n{describe(build_id)}"

    frontier = registry.build_get_frontier(build_id)
    assert frontier.deployment_id == current[0].id, (
        f"The hybrid driver planned under {frontier.deployment_id}, not the "
        f"app's current deployment {current[0].id}."
    )

    events = task_events(deployment, parent.id)
    yields = [
        e
        for e in events
        if e.get("event_type") == "task_yielded"
        and e.get("report_applied")
        and str(e.get("build_id")) == str(build_id)
    ]
    assert yields, (
        "The parent's worker never had a yield applied, so its deployment and "
        "the plan's did not meet.\n" + describe_events(events, hybrid=build_id)
    )
    assert registry.build_get(build_id).status == "completed", describe(build_id)
