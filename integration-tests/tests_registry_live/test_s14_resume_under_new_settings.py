"""S14: a resume under different settings is a new plan in the same build.

A build is one request, and its scope is not fixed at creation: settings are
chosen per trigger, so re-triggering a running build with other settings
registers a second plan in the same build, under the new scope, alongside
the active one; the new plan is activated at its seal and the old one
superseded (design.md, ``plan``: unique on ``(build, scope)``,
``activated_at``/``superseded_at``). Discovery runs under the new scope, and
the members already done are reused through the global status -- nothing is
re-run because the scope moved.

The alternative this rules out is v1's 409 ``scope_mismatch`` -- a resume
under another configuration refused outright -- and its opposite, a new
scope that resets what the old one already completed.

The re-trigger lands while the chain's middle task is RUNNING under the
first plan's claim. A plan being superseded changes nothing about a claim
it holds (design.md, "Claim × plan invariants"), so that execution runs to
completion and the new plan sees the task COMPLETED: the observable is one
submitted execution of every task across the build's whole ledger, and the
active plan's scope at the end.
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
from stardag_integration_tests.registry_live._harness import Deployment
from stardag_integration_tests.registry_live._scenario_app import MAX_LINGER_SECONDS
from stardag_integration_tests.registry_live._wait import (
    describe,
    wait_for_task_status,
    wait_for_terminal,
    wait_until,
)

registry_live_guard()

pytestmark = [
    pytest.mark.registry_live,
    pytest.mark.timeout(900),
]

# The middle task must still be RUNNING when the second plan is sealed, so
# the new plan meets an execution the old plan's claim holds: the resume's
# bootstrap and static phase are one container start.
SLOW_SECONDS = 90

STATUS_TIMEOUT_SECONDS = 300
BUILD_TIMEOUT_SECONDS = 600

FLAG = "REGISTRY_LIVE_FLAG"


@pytest.mark.budget(150)
def test_s14_resume_under_new_settings_plans_anew_and_reuses_completions(
    deployment: Deployment,
) -> None:
    from stardag.registry import registry_provider
    from stardag_integration_tests.registry_live.dag_app import app
    from stardag_integration_tests.registry_live.tasks import (
        get_range,
        get_sum,
        slow,
    )

    salt = uuid.uuid4().hex
    leaf = get_range(limit=3, salt=salt)
    middle = slow(values=leaf, seconds=SLOW_SECONDS)
    root = get_sum(integers=middle)
    tick_kwargs = {"linger_seconds": MAX_LINGER_SECONDS, "poll_interval_seconds": 3}
    registry = registry_provider.get()

    build_id = app.build_trigger(
        root, reactive=True, tick_kwargs=tick_kwargs, settings={FLAG: "first"}
    ).build_id
    wait_for_task_status(
        middle.id, expected="running", build_id=build_id, timeout=STATUS_TIMEOUT_SECONDS
    )
    first = registry.build_get_frontier(build_id)
    assert first.plan_id is not None and first.sealed, describe(build_id)

    resumed = app.build_trigger(
        root,
        build_id=build_id,
        reactive=True,
        tick_kwargs=tick_kwargs,
        settings={FLAG: "second"},
    )
    assert resumed.build_id == build_id

    # The replacement is registered alongside the active plan and becomes
    # the active one at its seal.
    second = wait_until(
        lambda: (
            f
            if (f := registry.build_get_frontier(build_id)).plan_id != first.plan_id
            and f.sealed
            else None
        ),
        build_id=build_id,
        timeout=STATUS_TIMEOUT_SECONDS,
        what="a second, sealed plan to become the build's active plan",
    )
    assert second.settings_hash != first.settings_hash
    assert second.deployment_id == first.deployment_id

    status = wait_for_terminal(build_id, timeout=BUILD_TIMEOUT_SECONDS)
    assert status == "completed", describe(build_id)
    final = registry.build_get_frontier(build_id)
    assert final.plan_id == second.plan_id, (
        "The build did not finish on the plan of its latest request."
    )

    # Completed members were reused through the global status: every task
    # ran once, whichever plan claimed it.
    rows = ledger(deployment, build_id)
    counts = spawned_executions(deployment, build_id)
    for task in (leaf, middle, root):
        assert counts.get(str(task.id)) == 1, (
            f"{task.id} ran {counts.get(str(task.id), 0)} times across the "
            "build's plans; a new scope must reuse what the old one completed.\n"
            + describe_ledger(rows)
        )
    middle_plan = next(
        r["plan_id"]
        for r in rows
        if r["task_id"] == str(middle.id) and r.get("executor_ref")
    )
    assert str(middle_plan) == str(first.plan_id), (
        "The middle task's one execution should be the first plan's, run to "
        "completion across the supersession.\n" + describe_ledger(rows)
    )
