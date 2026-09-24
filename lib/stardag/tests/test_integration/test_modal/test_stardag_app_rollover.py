"""The deployed ``tick`` and rollover: a tick compares the active plan's
deployment with its own ``STARDAG_DEPLOYMENT_ID``. On a difference it rolls
the build over when its deployment is the app's current one — re-planning
under its own code, with the app's task-module pre-flight — and exits
``superseded`` otherwise. The rollover mechanics are the engine's
(``tests/test_build/test_reactive_tick.py::TestRollover``); these pin the
deployed tick's wiring of them, end to end on an ``InMemoryRegistry``."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

try:
    import modal  # noqa: F401
except ImportError:
    pytest.skip("Skipping modal tests (import not available)", allow_module_level=True)

from stardag.build import TickSummary
from stardag.build._deployment import STARDAG_DEPLOYMENT_ID_ENV
from stardag.build._registration import new_id, register_plan_aio, walk_aio
from stardag.integration.modal import FunctionSettings, StardagApp
from stardag.registry import registry_provider
from stardag.testing import InMemoryRegistry
from stardag.utils.testing.helper_tasks import SyncOnlyTask
from tests.test_integration.test_modal._app_helpers import (  # noqa: F401
    _UNCOVERING_PATTERN,
    _finalize_capturing_functions,
    _invoke,
    _make_image,
    _mock_secret_hydrate,
    _stub_modal_concurrent,
)

APP = "rollover-app"


def _tick(task_modules=("stardag.utils.testing.*",)):
    app = StardagApp(
        APP,
        builder_settings=FunctionSettings(image=_make_image()),
        worker_settings={"default": FunctionSettings(image=_make_image())},
        task_modules=list(task_modules),
    )
    return _finalize_capturing_functions(app)["tick"]


def _planned(root, *, code_id: str = "v1"):
    """A reactive build of ``root`` planned under a first deployment of the
    app, the way the bootstrap plans it. Returns ``(registry, build_id,
    old_deployment_id)``."""
    registry = InMemoryRegistry()
    old = registry.add_deployment(app_name=APP, code_id=code_id)
    build_id = registry.build_create(root_task_ids=[str(root.id)]).id
    walk = asyncio.run(walk_aio([root]))
    asyncio.run(
        register_plan_aio(registry, build_id, walk, deployment_id=old, settings={})
    )
    registry.build_set_reactive_meta(build_id, app_name=APP)
    return registry, build_id, old


def _run_tick(tick, registry, build_id):
    with registry_provider.override(registry):
        return _invoke(tick, str(build_id), {"linger_seconds": 0})


class TestTheTickNamesItsDeployment:
    def _captured_kwargs(self, monkeypatch, deployment_id):
        if deployment_id is None:
            monkeypatch.delenv(STARDAG_DEPLOYMENT_ID_ENV, raising=False)
        else:
            monkeypatch.setenv(STARDAG_DEPLOYMENT_ID_ENV, str(deployment_id))
        registry = InMemoryRegistry()
        build_id = registry.build_create(root_task_ids=["root"]).id
        registry.build_set_reactive_meta(build_id, app_name=APP)
        captured: dict = {}

        async def stub_tick_aio(build_uuid, **kwargs):
            captured.update(kwargs)
            return TickSummary(outcome="lingered_out")

        with patch("stardag.integration.modal._tick.run_tick_aio", stub_tick_aio):
            _run_tick(_tick(), registry, build_id)
        return captured

    def test_a_deployed_tick_passes_its_deployment_and_a_rollover_hook(
        self, monkeypatch, default_in_memory_fs_target
    ):
        own = new_id()
        kwargs = self._captured_kwargs(monkeypatch, own)
        assert kwargs["deployment_id"] == own
        assert callable(kwargs["roll_over"])

    def test_outside_a_deployment_there_is_no_rollover(
        self, monkeypatch, default_in_memory_fs_target
    ):
        """No ``STARDAG_DEPLOYMENT_ID``, no own code to roll a build over to."""
        kwargs = self._captured_kwargs(monkeypatch, None)
        assert kwargs["deployment_id"] is None
        assert kwargs["roll_over"] is None


class TestRolloverThroughTheDeployedTick:
    def test_the_current_deployments_tick_rolls_the_build_over(
        self, monkeypatch, default_in_memory_fs_target
    ):
        """The root is complete, so the new plan is complete at its seal and
        the build finishes under the new deployment, nothing spawned."""
        root = SyncOnlyTask(name=f"rolled-{new_id()}")
        root.run()
        registry, build_id, old = _planned(root)
        new = registry.add_deployment(app_name=APP, code_id="v2")
        monkeypatch.setenv(STARDAG_DEPLOYMENT_ID_ENV, str(new))

        result = _run_tick(_tick(), registry, build_id)

        assert result["rolled_over"] == 1
        active = registry.active_plan(build_id)
        assert active is not None and active.deployment_id == new
        assert (
            registry.plans[
                next(p.id for p in registry.plans.values() if p.deployment_id == old)
            ].superseded_at
            is not None
        )
        assert registry.builds[build_id].status == "completed"

    def test_a_tick_that_is_not_the_current_deployment_is_superseded(
        self, monkeypatch, default_in_memory_fs_target
    ):
        """Rollover only moves forward, on the registry's record: a tick of a
        deployment that is not the app's current one leaves the build alone."""
        registry, build_id, old = _planned(SyncOnlyTask(name=f"stale-{new_id()}"))
        stale = registry.add_deployment(app_name=APP, code_id="v0", activated=False)
        monkeypatch.setenv(STARDAG_DEPLOYMENT_ID_ENV, str(stale))

        result = _run_tick(_tick(), registry, build_id)

        assert result["outcome"] == "superseded"
        assert len(registry.plans) == 1
        active = registry.active_plan(build_id)
        assert active is not None and active.deployment_id == old
        assert registry.builds[build_id].status == "running"

    def test_a_task_the_new_code_cannot_rebuild_fails_the_rollover(
        self, monkeypatch, default_in_memory_fs_target
    ):
        """The deployed tick hands the rollover the app's task-module
        pre-flight: a walked task the new deployment could not rehydrate
        fails the build (re-trigger it as a new build), and the tick's
        result carries the reason."""
        registry, build_id, _ = _planned(SyncOnlyTask(name=f"uncovered-{new_id()}"))
        new = registry.add_deployment(app_name=APP, code_id="v2")
        monkeypatch.setenv(STARDAG_DEPLOYMENT_ID_ENV, str(new))

        result = _run_tick(
            _tick(task_modules=[_UNCOVERING_PATTERN]), registry, build_id
        )

        assert result["outcome"] == "rollover_failed"
        assert "not covered by task_modules" in result["error"]
        build = registry.builds[build_id]
        assert build.status == "failed"
        assert "new build" in (build.error_message or "")
        assert len(registry.plans) == 1

    def test_a_root_the_new_code_cannot_rehydrate_fails_the_build(
        self, monkeypatch, default_in_memory_fs_target
    ):
        root = SyncOnlyTask(name=f"gone-{new_id()}")
        registry, build_id, _ = _planned(root)
        (instance,) = [
            i for i in registry.instances.values() if i.task_id == str(root.id)
        ]
        instance.body["__name"] = "NoSuchClass"
        new = registry.add_deployment(app_name=APP, code_id="v2")
        monkeypatch.setenv(STARDAG_DEPLOYMENT_ID_ENV, str(new))

        result = _run_tick(_tick(), registry, build_id)

        assert result["outcome"] == "rollover_failed"
        assert registry.builds[build_id].status == "failed"
