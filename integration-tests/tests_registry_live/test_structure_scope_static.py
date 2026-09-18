"""A build whose static structure changed is not gated by the old upstream.

The incident that started the whole structure-scope work: a root ``R`` whose
``requires()`` returned ``U1`` was registered by a build that was then
cancelled, leaving ``U1`` cancelled. The code changed so that ``R`` required
``U2`` instead, ``R`` kept its id, and the next build was gated on ``U1``
forever — the registry kept every edge ever recorded for a task id,
environment-wide, and gated on all of them. The escape was bumping ``R``'s
version.

Dependency edges now live in a *structure scope*: the code that evaluated
``requires()`` plus the ``dependencies_only`` config it read. From one
deployment the code cannot change, so the scenario changes the structure the
other way, through the config: ``ConfiguredChain.upstream_seconds`` is a
``dependencies_only`` field that picks a different ``Slow`` upstream without
changing the root's id. Two builds, two configs, one root id, two scopes.

What must hold: the second build completes, its plan never contains the
first build's upstream, and it writes no event on it — it neither reset it
nor ran it. Against the old registry the second build would have reset the
cancelled ``U1`` (a revocation in its plan) and run a 90-second task nobody
asked for before it could finish.
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
]

# Long enough for the first build to be cancelled while its upstream is
# still RUNNING — and long enough that, were the second build to re-run it,
# the run would be visibly slower than the assertions below allow.
OLD_UPSTREAM_SECONDS = 90
NEW_UPSTREAM_SECONDS = 5

STATUS_TIMEOUT_SECONDS = 300
BUILD_TIMEOUT_SECONDS = 600


def _config_key(cls) -> str:
    from stardag.build_config import task_config_key

    return task_config_key(cls.get_namespace(), cls.get_name())


def test_a_changed_static_upstream_does_not_gate_the_next_build(
    deployment: Deployment,
) -> None:
    from stardag.registry import registry_provider
    from stardag_integration_tests.registry_live.dag_app import app
    from stardag_integration_tests.registry_live.tasks import (
        ConfiguredChain,
        get_range,
        slow,
    )

    salt = uuid.uuid4().hex
    key = _config_key(ConfiguredChain)
    root = ConfiguredChain(salt=salt)
    leaf = get_range(limit=3, salt=salt)
    old_upstream = slow(values=leaf, seconds=OLD_UPSTREAM_SECONDS)
    new_upstream = slow(values=leaf, seconds=NEW_UPSTREAM_SECONDS)

    # Build 1: R -> U1 (the default width of the config field).
    build_1 = app.build_trigger(
        root,
        reactive=True,
        tick_kwargs={"linger_seconds": 60, "poll_interval_seconds": 3},
        build_config={key: {"upstream_seconds": OLD_UPSTREAM_SECONDS}},
    ).build_id
    wait_for_task_status(
        old_upstream.id,
        expected="running",
        build_id=build_1,
        timeout=STATUS_TIMEOUT_SECONDS,
    )
    registry = registry_provider.get()
    cancelled = registry.build_cancel(build_1, cascade=True)
    assert cancelled is not None and str(old_upstream.id) in cancelled.cascaded_task_ids
    wait_for_task_status(
        old_upstream.id,
        expected="cancelled",
        build_id=build_1,
        timeout=STATUS_TIMEOUT_SECONDS,
    )

    # Build 2: the same root id, a different upstream, a different scope.
    build_2 = app.build_trigger(
        root,
        reactive=True,
        tick_kwargs={"linger_seconds": 120, "poll_interval_seconds": 3},
        build_config={key: {"upstream_seconds": NEW_UPSTREAM_SECONDS}},
    ).build_id

    status_2 = wait_for_terminal(build_2, timeout=BUILD_TIMEOUT_SECONDS)
    assert status_2 == "completed", (
        "Build 2 did not complete. Its root's structure changed through the "
        "build config, so the first build's upstream must not gate it.\n"
        f"--- build 1 (cancelled) ---\n{describe(build_1)}\n"
        f"--- build 2 ---\n{describe(build_2)}"
    )

    info_1 = registry.build_get(build_1)
    info_2 = registry.build_get(build_2)
    assert info_1.scope_key and info_2.scope_key, (info_1, info_2)
    assert info_1.scope_key != info_2.scope_key, (
        "Two builds with different dependencies_only config share a "
        f"structure scope ({info_2.scope_key!r}); the scope hash is not "
        "reading the config."
    )
    assert not info_1.scope_key.startswith("build:"), (
        "The bootstrap never fixed a real scope on build 1 — it is still on "
        f"the server's synthetic one ({info_1.scope_key!r})."
    )

    # The decisive observables. The old upstream stayed cancelled: nothing
    # reset it, nothing ran it, and in particular build 2 never touched it.
    assert task_status(old_upstream.id) == "cancelled", describe(build_2)
    old_events = task_events(deployment, old_upstream.id)
    assert not events_by(old_events, build_2), (
        "Build 2 has events on the first build's upstream, so the old edge "
        "reached its plan — the structure scope did not isolate it.\n"
        f"--- events on U1 ---\n"
        f"{describe_events(old_events, one=build_1, two=build_2)}"
    )
    assert task_status(new_upstream.id) == "completed", describe(build_2)
