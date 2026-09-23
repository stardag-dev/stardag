"""An abandoned dynamic generation is not inherited by a narrower build.

The second incident behind the structure-scope work, and the more common
one, because internal fan-out is usually dynamic. A parent yielded ten
children and its build was cancelled mid-flight, leaving the children
cancelled or pending. The fan-out width was changed — an internal concern,
so the parent kept its id — and the next build was gated on the old ten:
plan closure admitted them, the tick reset them, and ten partitions of the
previous scheme were crunched for nothing before the parent could re-yield.

Here the width is a ``dependencies_only`` field read from the build config,
so two builds with different widths have different structure scopes and the
narrower build never sees the wider generation's edges. The child ids
overlap by construction (index 0 and 1 exist at both widths), which is what
makes the assertion precise: the shared children *are* the second build's
to reset and run — they are in its own plan — while the children only the
wide generation had must never carry an event from the second build.
"""

from __future__ import annotations

import uuid

import pytest

from stardag_integration_tests.registry_live._events import (
    describe_events,
    events_by,
    task_events,
)
from stardag_integration_tests.registry_live._guard import registry_live_guard
from stardag_integration_tests.registry_live._harness import Deployment
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
    pytest.mark.skip(
        reason="v2: dies with build_config; replaced by S8/S22/S24 in I10"
    ),
]

WIDE = 4
NARROW = 2
# Children run long enough that the first build is cancelled while its
# generation is still in flight, so the abandoned children are left in the
# statuses a cascade leaves (cancelled, or pending if not yet started).
CHILD_SECONDS = 60
PRE_YIELD_SECONDS = 10

STATUS_TIMEOUT_SECONDS = 300
BUILD_TIMEOUT_SECONDS = 600


def _config_key(cls) -> str:
    from stardag.build_config import task_config_key

    return task_config_key(cls.get_namespace(), cls.get_name())


def test_a_narrower_build_never_runs_the_abandoned_wide_generation(
    deployment: Deployment,
) -> None:
    from stardag.build_config import build_config_scope
    from stardag.registry import registry_provider
    from stardag_integration_tests.registry_live.tasks import (
        ConfiguredFanOut,
        get_sum,
        square,
    )
    from stardag_integration_tests.registry_live.dag_app import app

    salt = uuid.uuid4().hex
    key = _config_key(ConfiguredFanOut)
    parent = ConfiguredFanOut(
        salt=salt, child_seconds=CHILD_SECONDS, pre_yield_seconds=PRE_YIELD_SECONDS
    )
    # The children the wide generation yields, by construction of the task;
    # the first NARROW of them are exactly the narrow generation's.
    with build_config_scope({key: {"children": WIDE}}):
        wide_children = ConfiguredFanOut(
            salt=salt, child_seconds=CHILD_SECONDS, pre_yield_seconds=PRE_YIELD_SECONDS
        ).child_tasks()
    with build_config_scope({key: {"children": NARROW}}):
        narrow_children = ConfiguredFanOut(
            salt=salt, child_seconds=CHILD_SECONDS, pre_yield_seconds=PRE_YIELD_SECONDS
        ).child_tasks()
    assert len(wide_children) == WIDE and len(narrow_children) == NARROW
    assert [c.id for c in wide_children[:NARROW]] == [c.id for c in narrow_children]
    only_wide = wide_children[NARROW:]

    build_1 = app.build_trigger(
        get_sum(integers=square(values=parent, offset=1)),
        reactive=True,
        tick_kwargs={"linger_seconds": 90, "poll_interval_seconds": 3},
        build_config={key: {"children": WIDE}},
    ).build_id
    # Suspended: the generation is registered and in flight.
    wait_for_task_status(
        parent.id,
        expected="suspended",
        build_id=build_1,
        timeout=STATUS_TIMEOUT_SECONDS,
    )
    registry = registry_provider.get()
    cancelled = registry.build_cancel(build_1, cascade=True)
    assert cancelled is not None, describe(build_1)

    build_2 = app.build_trigger(
        get_sum(integers=square(values=parent, offset=1)),
        reactive=True,
        tick_kwargs={"linger_seconds": 180, "poll_interval_seconds": 3},
        build_config={key: {"children": NARROW}},
    ).build_id

    status_2 = wait_for_terminal(build_2, timeout=BUILD_TIMEOUT_SECONDS)
    assert status_2 == "completed", (
        "The narrower build did not complete.\n"
        f"--- build 1 (cancelled) ---\n{describe(build_1)}\n"
        f"--- build 2 ---\n{describe(build_2)}"
    )

    assert (
        registry.build_get(build_1).scope_key != registry.build_get(build_2).scope_key
    )

    # The children only the wide generation had were never build 2's.
    for child in only_wide:
        events = task_events(deployment, child.id, missing_ok=True)
        assert not events_by(events, build_2), (
            f"Build 2 has events on wide-only child {child.id}, so the "
            "abandoned generation's edges reached its plan.\n"
            f"--- events ---\n{describe_events(events, one=build_1, two=build_2)}"
        )

    # And the narrow generation is what build 2 ran: both shared children
    # completed (they are in its own plan), and nothing else was needed.
    for child in wide_children[:NARROW]:
        assert task_status(child.id) == "completed", describe(build_2)
