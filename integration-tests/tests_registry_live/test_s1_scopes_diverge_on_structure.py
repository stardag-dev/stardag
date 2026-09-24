"""S1: two scopes, overlapping DAGs, diverging upstreams, one completion.

Two builds want one completion (``ScopedUpstreams``) under two scopes --
different ``settings`` -- and the setting shapes its structure: one upstream
under A, three under B. Each build also constructs it with its own body
(a non-significant ``label``). The design's outcome (design.md, scenario
table, S1): both plans admit the shared completion with **their own
instances**; whichever claims it first runs *its* instance body; the other
waits on the global status. The upstream sets differ, and the duplicated
upstream work (B's extra upstreams) is accepted, not prevented.

The alternatives this rules out: structure keyed by task id (one edge set
for the completion, so one build gates on the other's upstreams, or edges
are "retracted" to agree), and completion keyed by scope (B would run the
completion a second time under its own structure).

Observables, all durable: the one submitted execution of the shared task is
A's and names A's instance; the task holds one instance per scope with
distinct hashes; B's ledger holds its own executions of the upstreams only
its scope declared.
"""

from __future__ import annotations

import uuid

import pytest

from stardag_integration_tests.registry_live._events import (
    describe_ledger,
    spawned_of,
    wait_until_registered,
)
from stardag_integration_tests.registry_live._guard import registry_live_guard
from stardag_integration_tests.registry_live._harness import Deployment
from stardag_integration_tests.registry_live._scenario_app import MAX_LINGER_SECONDS
from stardag_integration_tests.registry_live._wait import (
    describe,
    task_status,
    wait_for_task_status,
    wait_for_terminal,
)

registry_live_guard()

pytestmark = [
    pytest.mark.registry_live,
    pytest.mark.timeout(900),
]

# B must register while A's execution of the shared task is RUNNING: B's
# bootstrap is one container start after A's claim.
SHARED_SECONDS = 60
A_UPSTREAMS = 1
B_UPSTREAMS = 3

STATUS_TIMEOUT_SECONDS = 300
BUILD_TIMEOUT_SECONDS = 600


def test_s1_diverging_scopes_run_one_completion_once(deployment: Deployment) -> None:
    from stardag.registry import registry_provider
    from stardag.registry import TaskInstanceInfo
    from stardag_integration_tests.registry_live.dag_app import app
    from stardag_integration_tests.registry_live.tasks import (
        SCOPED_UPSTREAMS_SETTING,
        ScopedUpstreams,
        get_range,
        square,
    )

    salt = uuid.uuid4().hex
    shared_a = ScopedUpstreams(salt=salt, seconds=SHARED_SECONDS, label="a")
    shared_b = ScopedUpstreams(salt=salt, seconds=SHARED_SECONDS, label="b")
    assert shared_a.id == shared_b.id, "label must not be significant"
    tick_kwargs = {"linger_seconds": MAX_LINGER_SECONDS, "poll_interval_seconds": 3}

    build_a = app.build_trigger(
        square(values=shared_a, offset=1),
        reactive=True,
        tick_kwargs=tick_kwargs,
        settings={SCOPED_UPSTREAMS_SETTING: str(A_UPSTREAMS)},
    ).build_id
    wait_for_task_status(
        shared_a.id,
        expected="running",
        build_id=build_a,
        timeout=STATUS_TIMEOUT_SECONDS,
    )

    build_b = app.build_trigger(
        square(values=shared_b, offset=2),
        reactive=True,
        tick_kwargs=tick_kwargs,
        settings={SCOPED_UPSTREAMS_SETTING: str(B_UPSTREAMS)},
    ).build_id
    wait_until_registered(deployment, task_id=shared_a.id, build_id=build_b)
    assert task_status(shared_a.id) == "running", (
        "B registered after A's execution finished; raise SHARED_SECONDS "
        f"({SHARED_SECONDS}s).\n" + describe(build_b)
    )

    registry = registry_provider.get()
    scope_a = registry.build_get_frontier(build_a)
    scope_b = registry.build_get_frontier(build_b)
    assert scope_a.settings_hash != scope_b.settings_hash

    for build_id in (build_a, build_b):
        status = wait_for_terminal(build_id, timeout=BUILD_TIMEOUT_SECONDS)
        assert status == "completed", (
            f"--- A ---\n{describe(build_a)}\n--- B ---\n{describe(build_b)}"
        )

    # Each plan admitted the completion with its own instance.
    by_scope: dict[tuple[object, object], object] = {
        (i.deployment_id, i.settings_hash): i
        for i in registry.task_get(str(shared_a.id)).instances
    }
    instance_a = by_scope.get((scope_a.deployment_id, scope_a.settings_hash))
    instance_b = by_scope.get((scope_b.deployment_id, scope_b.settings_hash))
    assert isinstance(instance_a, TaskInstanceInfo), by_scope
    assert isinstance(instance_b, TaskInstanceInfo), by_scope
    assert instance_a.instance_hash != instance_b.instance_hash
    assert instance_a.body.get("label") == "a" and instance_b.body.get("label") == "b"

    # The claim decided: A ran it, once, from A's instance body.
    spawned = spawned_of(deployment, shared_a.id, build_a, build_b)
    ledger_text = describe_ledger(spawned, A=build_a, B=build_b)
    assert len(spawned) == 1, (
        "The shared completion ran other than once.\n" + ledger_text
    )
    assert spawned[0]["build_id"] == str(build_a), ledger_text
    assert str(spawned[0]["instance_id"]) == str(instance_a.id), (
        "The execution did not run the claiming build's own instance.\n" + ledger_text
    )

    # The upstreams only B's scope declared were B's to run: the structure
    # diverged, and the duplicated upstream work is B's alone.
    for limit in range(A_UPSTREAMS, B_UPSTREAMS):
        upstream = get_range(limit=limit, salt=salt)
        rows = spawned_of(deployment, upstream.id, build_a, build_b)
        assert [r["build_id"] for r in rows] == [str(build_b)], (
            f"Range(limit={limit}) is only in B's structure, so exactly one "
            "execution of it, in B, was expected.\n"
            + describe_ledger(rows, A=build_a, B=build_b)
        )
