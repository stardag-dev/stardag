"""A plan-only build is registered, and nothing of it is ever started.

``sd.register`` registers a DAG under the deployed app's current deployment
-- the same scope a reactive build of that app plans under -- and ends the
build REGISTERED without running anything. What could act on it is the
reactive machinery around it, so the scenario runs that machinery against
it rather than asserting a flag:

- The registered DAG shares its leaf with a **reactive build** of the same
  app, triggered afterwards. That build claims, runs and completes the
  leaf, so every status change of the shared task flags the RUNNING
  reactive builds whose active plan holds it. The plan-only build holds it
  too, and is never RUNNING: it must never be flagged, handed out by
  ``wake-candidates``, reported by ``stalled-builds``, or ticked.
- The registered root is held by the plan-only build alone. Once the leaf
  is COMPLETED it is runnable by the runnable rule -- in any build that
  hands out work. It must still be PENDING, never started, at the end.

Observables, all read after the reactive build has completed (a state,
not a clock): the plan-only build is ``registered`` with a sealed plan
under the app's current deployment; its ledger holds no execution and no
tick reported for it; the registry reports no pending wake-up and no
stall for it; and the registered root was never started by anyone.
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
from stardag_integration_tests.registry_live._wait import (
    describe,
    wait_for_terminal,
)

registry_live_guard()

pytestmark = [
    pytest.mark.registry_live,
    pytest.mark.timeout(900),
]

BUILD_TIMEOUT_SECONDS = 600


@pytest.mark.budget(70)
def test_a_registered_dag_is_never_started(deployment: Deployment) -> None:
    import stardag as sd
    from stardag.registry import registry_provider
    from stardag_integration_tests.registry_live.dag_app import APP_NAME, app
    from stardag_integration_tests.registry_live.tasks import (
        get_range,
        get_sum,
        square,
    )

    salt = uuid.uuid4().hex
    leaf = get_range(limit=6, salt=salt)
    registered_root = get_sum(integers=leaf)
    registry = registry_provider.get()
    (current,) = [
        d
        for d in registry.deployment_list(kind="modal", app_name=APP_NAME, current=True)
        if d.app_name == APP_NAME
    ]

    (instance_id,) = sd.register(registered_root, app_name=APP_NAME)

    # The salted root is this scenario's alone, so its REGISTERED build is
    # the one ``sd.register`` just created.
    (registered,) = [
        b
        for b in registry.build_list_page(status="registered", limit=200).builds
        if str(registered_root.id) in b.root_task_ids
    ]
    plan_only = registered.id
    assert registered.plan_only, registered
    frontier = registry.build_get_frontier(plan_only)
    assert frontier.sealed and frontier.deployment_id == current.id, frontier
    assert (frontier.runnable, frontier.discovery_jobs) == ([], []), frontier
    assert frontier.plan_id is not None
    (root_member,) = registry.plan_roots(frontier.plan_id)
    assert root_member.instance_id == instance_id, root_member

    # The reactive build that runs the shared leaf, and so moves it through
    # every status a wake-up is flagged on.
    reactive = app.build_trigger(
        square(values=leaf, offset=3),
        reactive=True,
        tick_kwargs={"linger_seconds": 30, "poll_interval_seconds": 3},
    ).build_id
    status = wait_for_terminal(reactive, timeout=BUILD_TIMEOUT_SECONDS)
    assert status == "completed", describe(reactive)
    assert registry.task_get(str(leaf.id)).status == "completed"

    build = registry.build_get(plan_only)
    assert (build.status, build.plan_only, build.reactive_app_name) == (
        "registered",
        True,
        None,
    ), build
    executions = registry.build_list_executions(plan_only, include_ended=True)
    assert executions == [], executions
    assert registry.build_list_tick_summaries(plan_only) == []
    assert not registry.build_get_notify(plan_only).needs_tick
    stalled = registry.build_list_stalled(older_than_seconds=1)
    assert plan_only not in {s.build_id for s in stalled}, stalled

    # The decisive one: runnable by the runnable rule since the leaf
    # completed, and held by no build that hands out work.
    assert registry.task_get(str(registered_root.id)).status == "pending"
    events = task_events(deployment, registered_root.id)
    started = [e for e in events if e.get("event_type") == "task_started"]
    assert started == [], (
        "The registered root was started, though only a plan-only build "
        "holds it.\n" + describe_events(events)
    )
