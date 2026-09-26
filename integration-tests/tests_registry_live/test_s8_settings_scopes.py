"""S8: two builds under different settings share one completion.

Settings are the second half of the scope (design.md, "The deterministic
scope"), so two builds of one DAG under different settings plan it under
two scopes: two plans, two instance rows for every task, **one** ``task``
row per completion. The claim is on that one row, so the completion is run
once, by whichever build claims it first, and the other build waits on the
global status and reuses the result.

This replaces v1's ``test_structure_scope_static``, which asked the same
question of ``build_config``'s ``dependencies_only`` part -- a mechanism v2
deleted. The alternative this rules out is "settings are per-build state"
(v1's ``build_config``): then the second build would either be refused
(``scope_mismatch``) or run the shared task a second time.

Durable observables only: the two frontiers' scopes, the shared task's two
instances, and the execution ledger's count of submitted containers.
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

# Long enough that B registers while the shared task is still RUNNING
# under A's claim: B's bootstrap is one container start after A's claim.
SHARED_SECONDS = 60

STATUS_TIMEOUT_SECONDS = 300
BUILD_TIMEOUT_SECONDS = 600

FLAG = "REGISTRY_LIVE_FLAG"


@pytest.mark.budget(125)
def test_s8_two_settings_scopes_share_one_completion(deployment: Deployment) -> None:
    from stardag.registry import registry_provider
    from stardag_integration_tests.registry_live.dag_app import app
    from stardag_integration_tests.registry_live.tasks import (
        get_range,
        get_sum,
        slow,
        square,
    )

    salt = uuid.uuid4().hex
    shared = slow(values=get_range(limit=3, salt=salt), seconds=SHARED_SECONDS)
    tick_kwargs = {"linger_seconds": MAX_LINGER_SECONDS, "poll_interval_seconds": 3}

    build_a = app.build_trigger(
        get_sum(integers=shared),
        reactive=True,
        tick_kwargs=tick_kwargs,
        settings={FLAG: f"a-{salt[:8]}"},
    ).build_id
    wait_for_task_status(
        shared.id, expected="running", build_id=build_a, timeout=STATUS_TIMEOUT_SECONDS
    )

    build_b = app.build_trigger(
        square(values=shared, offset=3),
        reactive=True,
        tick_kwargs=tick_kwargs,
        settings={FLAG: f"b-{salt[:8]}"},
    ).build_id
    wait_until_registered(deployment, task_id=shared.id, build_id=build_b)
    assert task_status(shared.id) == "running", (
        "B registered after the shared task left RUNNING, so the claim was "
        f"never contended; raise SHARED_SECONDS ({SHARED_SECONDS}s).\n"
        + describe(build_b)
    )

    registry = registry_provider.get()
    frontier_a = registry.build_get_frontier(build_a)
    frontier_b = registry.build_get_frontier(build_b)
    assert frontier_a.deployment_id == frontier_b.deployment_id, (
        frontier_a,
        frontier_b,
    )
    assert frontier_a.settings_hash != frontier_b.settings_hash, (
        "Different settings planned under one scope, so settings are not "
        f"part of the scope: {frontier_a.settings_hash}"
    )

    for build_id in (build_a, build_b):
        status = wait_for_terminal(build_id, timeout=BUILD_TIMEOUT_SECONDS)
        assert status == "completed", (
            f"--- A ---\n{describe(build_a)}\n--- B ---\n{describe(build_b)}"
        )

    # One completion, two constructions: an instance per scope, with the
    # same body (settings are not a hash input) and so the same hash.
    instances = registry.task_get(str(shared.id)).instances
    scopes = {(i.deployment_id, i.settings_hash) for i in instances}
    assert scopes >= {
        (frontier_a.deployment_id, frontier_a.settings_hash),
        (frontier_b.deployment_id, frontier_b.settings_hash),
    }, instances
    assert len({i.instance_hash for i in instances}) == 1, (
        "One body produced two instance hashes across scopes; the scope must "
        f"be a storage key, not a hash input: {instances}"
    )

    # The claim decided who ran it, once; the other reused the result.
    spawned = spawned_of(deployment, shared.id, build_a, build_b)
    assert len(spawned) == 1 and spawned[0]["build_id"] == str(build_a), (
        "The shared completion was not run exactly once, by the build that "
        "claimed it first.\n" + describe_ledger(spawned, A=build_a, B=build_b)
    )
