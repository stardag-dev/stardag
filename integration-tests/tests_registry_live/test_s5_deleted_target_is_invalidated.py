"""S5: the registry follows the world when a completed target is deleted.

COMPLETED is a fact about the world -- the target exists -- and the only
path out of it is a driver observing the target missing (design.md,
"Invalidation: the registry follows the world"): discovery's chunk item
carries ``observed_complete: false``, and the task goes COMPLETED -> PENDING
(``TASK_INVALIDATED``) in the chunk's own transaction, after which it is
runnable again like any other member. There is no operator route that
declares a task incomplete; an operator deletes the target and triggers a
build.

Two halves, both on one salted chain ``Range -> Square -> Sum``:

1. **Stickiness.** After a first build completes the chain, ``Square``'s
   target is deleted and a second build of the same root is triggered. The
   root's target exists, so the walk stops there and never looks at
   ``Square``: it stays COMPLETED in the registry, as the design says
   ("without discovery seeing it, it stays COMPLETED").
2. **Invalidation.** ``Sum``'s target is deleted too and a third build is
   triggered. Its walk finds both missing, invalidates both, and re-runs
   them -- and not ``Range``, whose target still exists. The order is
   deliberate: the second build leaves a warm bootstrap container whose
   Modal Volume view holds ``Sum``'s file, and a walk answered from that
   view would see a deleted target as present. The SDK refreshes a mounted
   volume once per walk for exactly this (``stardag.target._freshness``);
   CI found the stale hit before it did.

The alternative this rules out is v1's: a COMPLETED task could not be reset
at all, so a vanished output could never be rebuilt; and its opposite, a
registry that invalidates on anything but an observation.
"""

from __future__ import annotations

import uuid

import pytest

from stardag_integration_tests.registry_live._events import (
    describe_events,
    describe_ledger,
    spawned_of,
    task_events,
)
from stardag_integration_tests.registry_live._guard import registry_live_guard
from stardag_integration_tests.registry_live._harness import Deployment
from stardag_integration_tests.registry_live._targets import delete_target
from stardag_integration_tests.registry_live._wait import (
    describe,
    task_status,
    wait_for_terminal,
)

registry_live_guard()

pytestmark = [
    pytest.mark.registry_live,
    pytest.mark.timeout(900),
]

BUILD_TIMEOUT_SECONDS = 300
INVALIDATED = "task_invalidated"


def _invalidations(deployment: Deployment, task_id, build_id) -> list[dict]:
    return [
        e
        for e in task_events(deployment, task_id)
        if e.get("event_type") == INVALIDATED
        and str(e.get("build_id")) == str(build_id)
    ]


def test_s5_a_deleted_target_is_invalidated_and_rerun(deployment: Deployment) -> None:
    from stardag.registry import registry_provider
    from stardag_integration_tests.registry_live.dag_app import app
    from stardag_integration_tests.registry_live.tasks import (
        get_range,
        get_sum,
        square,
    )

    salt = uuid.uuid4().hex
    leaf = get_range(limit=3, salt=salt)
    middle = square(values=leaf, offset=1)
    root = get_sum(integers=middle)
    registry = registry_provider.get()
    env = deployment.modal_environment
    tick_kwargs = {"linger_seconds": 30, "poll_interval_seconds": 3}

    def _trigger():
        return app.build_trigger(root, reactive=True, tick_kwargs=tick_kwargs).build_id

    def _uri(task) -> str:
        uri = registry.task_get(str(task.id)).output_uri
        assert uri, f"the registry recorded no output_uri for {task.id}"
        return uri

    first = _trigger()
    assert wait_for_terminal(first, timeout=BUILD_TIMEOUT_SECONDS) == "completed", (
        describe(first)
    )

    # 1. Stickiness: a deletion discovery never looks at is not observed.
    # The root's target exists, so the walk stops there. This build also
    # leaves a warm bootstrap container whose volume view holds the root's
    # file -- which is what makes the next half a test of freshness.
    delete_target(_uri(middle), modal_environment=env)
    second = _trigger()
    assert wait_for_terminal(second, timeout=BUILD_TIMEOUT_SECONDS) == "completed", (
        describe(second)
    )
    assert task_status(middle.id) == "completed", (
        "The registry withdrew a completion no driver observed missing."
    )
    assert not _invalidations(deployment, middle.id, second), describe_events(
        task_events(deployment, middle.id), first=first, second=second
    )
    assert len(spawned_of(deployment, middle.id, second)) == 0

    # 2. Invalidation: the root's output goes too, and a build observes both
    # missing -- from a view at least as fresh as its walk, even in a warm
    # container that saw the root's file (CI found exactly that stale hit).
    delete_target(_uri(root), modal_environment=env)
    third = _trigger()
    assert wait_for_terminal(third, timeout=BUILD_TIMEOUT_SECONDS) == "completed", (
        describe(third)
    )
    for task in (middle, root):
        events = task_events(deployment, task.id)
        assert _invalidations(deployment, task.id, third), (
            f"{task.id}'s missing target was not invalidated by the build that "
            f"observed it.\n{describe_events(events, first=first, third=third)}"
        )
        rows = spawned_of(deployment, task.id, first, second, third)
        assert [r["build_id"] for r in rows] == [str(first), str(third)], (
            f"{task.id} should have run once to produce it and once to "
            "re-produce it.\n"
            + describe_ledger(rows, first=first, second=second, third=third)
        )
        assert task.complete(), f"{task.id} was not re-produced"
    leaf_rows = spawned_of(deployment, leaf.id, first, second, third)
    assert [r["build_id"] for r in leaf_rows] == [str(first)], (
        "The leaf's target never went missing, so nothing should have re-run it.\n"
        + describe_ledger(leaf_rows, first=first, third=third)
    )
