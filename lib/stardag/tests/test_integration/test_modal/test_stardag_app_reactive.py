"""``StardagApp.build_trigger(reactive=True)`` and the ``bootstrap`` it
spawns, on the v2 registry.

The trigger mints the build (its root task ids are the build's request) and
hands the roots to the deployed ``bootstrap`` by value; the bootstrap plans
the build under its deployment — its own ``STARDAG_DEPLOYMENT_ID``, or run
in the triggering process (``reactive_discovery="local"``) the app's current
deployment (D13) — applying the build's settings, refuses a DAG a tick could
not rehydrate, writes the reactive marker last and spawns the first tick.

The registry is :class:`stardag.testing.InMemoryRegistry`, which follows the
server's seams; the deployed wrappers are captured at ``finalize()`` and run
in-process.
"""

from __future__ import annotations

import os
import typing
import weakref
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

try:
    import modal
except ImportError:
    pytest.skip("Skipping modal tests (import not available)", allow_module_level=True)

from stardag import BaseTask
from stardag.build import SettingsError
from stardag.build._deployment import STARDAG_DEPLOYMENT_ID_ENV
from stardag.integration.modal import FunctionSettings, StardagApp
from stardag.registry import NoOpRegistry, registry_provider
from stardag.testing import InMemoryRegistry
from stardag.testing._registry_state import settings_hash
from stardag.utils.testing.helper_tasks import SyncOnlyTask
from tests.test_integration.test_modal._app_helpers import (  # noqa: F401
    _UNCOVERING_PATTERN,
    _finalize_capturing_functions,
    _make_image,
    _mock_secret_hydrate,
    _stub_modal_concurrent,
)

_REACTIVE_TEST_TASK_MODULES = ["stardag.utils.testing.*", __name__]
"""What a reactive app in this module must declare: a tick rebuilds every
task from its instance body, so the bootstrap refuses a build whose classes
the deployment could not import. This module is not part of a package, so
inference opts out; the DAGs draw from ``helper_tasks`` plus classes here."""


@pytest.fixture(autouse=True)
def _not_in_a_deployment(monkeypatch):
    """The in-process bootstrap is not a deployed container unless a test
    says so: it then plans under the app's current deployment."""
    monkeypatch.delenv(STARDAG_DEPLOYMENT_ID_ENV, raising=False)


def _make_app(name: str = "test-reactive-app", **kwargs) -> StardagApp:
    kwargs.setdefault("task_modules", _REACTIVE_TEST_TASK_MODULES)
    return StardagApp(
        name,
        builder_settings=FunctionSettings(image=_make_image()),
        worker_settings={"default": FunctionSettings(image=_make_image())},
        **kwargs,
    )


def _app_with_task_modules(name: str, **kwargs) -> StardagApp:
    # Defined here, not in _app_helpers: task_modules inference reads the
    # module that constructs the app.
    return StardagApp(
        name,
        builder_settings=FunctionSettings(image=_make_image()),
        worker_settings={"default": FunctionSettings(image=_make_image())},
        **kwargs,
    )


def _deployed_registry(app: StardagApp, cls=InMemoryRegistry) -> InMemoryRegistry:
    """A registry in which ``app`` has a current (activated) deployment."""
    registry = cls()
    registry.add_deployment(app_name=app.name)
    return registry


_BOOTSTRAPS: "weakref.WeakKeyDictionary[StardagApp, typing.Any]" = (
    weakref.WeakKeyDictionary()
)


def _trigger_reactive(
    app: StardagApp,
    tasks,
    *,
    stub: dict,
    registry: InMemoryRegistry | None = None,
    build_id=None,
    tick_kwargs=None,
    settings=None,
    run_bootstrap: bool = True,
):
    """Trigger reactively, then run the spawned ``bootstrap`` in-process
    with exactly the kwargs the trigger passed. ``run_bootstrap=False``
    stops at the spawn. Returns ``(result, registry, bootstrap_kwargs)``."""
    if registry is None:
        registry = _deployed_registry(app)
    # finalize() once per app; a re-trigger reuses the captured bootstrap.
    if app not in _BOOTSTRAPS:
        _BOOTSTRAPS[app] = _finalize_capturing_functions(app)["bootstrap"]
    bootstrap = _BOOTSTRAPS[app]
    with registry_provider.override(registry):
        result = app.build_trigger(
            tasks,
            reactive=True,
            build_id=build_id,
            tick_kwargs=tick_kwargs,
            settings=settings,
        )
        bootstrap_kwargs = dict(stub["kwargs"])
        if run_bootstrap:
            bootstrap(**bootstrap_kwargs)
    return result, registry, bootstrap_kwargs


def _armed(registry: InMemoryRegistry, build_id) -> bool:
    return registry.builds[build_id].reactive_app_name is not None


def _members(registry: InMemoryRegistry, build_id) -> set[str]:
    plan = registry.active_plan(build_id)
    assert plan is not None
    return set(registry.members[plan.id])


class TestStardagAppReactiveTrigger:
    def test_trigger_spawns_bootstrap_with_the_roots_by_value(
        self, modal_function_stub, default_in_memory_fs_target
    ):
        """The trigger's whole job: mint the build with its roots, hand the
        root tasks to the deployed ``bootstrap`` by value."""
        app = _make_app()
        dep = SyncOnlyTask(name="reactive-dep")
        root = SyncOnlyTask(name="reactive-root", deps=(dep,))

        result, registry, bootstrap_kwargs = _trigger_reactive(
            app,
            root,
            stub=modal_function_stub,
            tick_kwargs={"linger_seconds": 30},
            settings={"MY_FLAG": "1"},
            run_bootstrap=False,
        )

        assert registry.builds[result.build_id].root_task_ids == [str(root.id)]
        assert modal_function_stub["from_name"] == {
            "app_name": app.name,
            "name": "bootstrap",
        }
        assert modal_function_stub["op"] == "spawn"
        # Roots ride along BY VALUE (cloudpickled into the call).
        assert bootstrap_kwargs == {
            "build_id": str(result.build_id),
            "tasks": [root],
            "tick_kwargs": {"linger_seconds": 30},
            "settings": {"MY_FLAG": "1"},
        }
        assert result.function_call == "spawn-handle"
        # Nothing planned, nothing armed: that is the bootstrap's.
        assert registry.plans == {}
        assert not _armed(registry, result.build_id)

    def test_trigger_does_no_discovery_locally(
        self, monkeypatch, modal_function_stub, default_in_memory_fs_target
    ):
        """Discovery is one ``complete_aio()`` — a target existence check —
        per task; against a ``modalvol://`` root that is a rate-limited
        volume API call from the triggering machine, so the trigger performs
        none and the bootstrap performs them all."""
        from stardag._core.base_task import TargetTask

        checked: list = []
        original = TargetTask.complete_aio

        async def spy(self):
            checked.append(self.id)
            return await original(self)

        monkeypatch.setattr(TargetTask, "complete_aio", spy)

        app = _make_app()
        dep = SyncOnlyTask(name="no-local-io-dep")
        root = SyncOnlyTask(name="no-local-io-root", deps=(dep,))

        _, registry, bootstrap_kwargs = _trigger_reactive(
            app, root, stub=modal_function_stub, run_bootstrap=False
        )
        assert checked == []

        bootstrap = _finalize_capturing_functions(_make_app())["bootstrap"]
        with registry_provider.override(registry):
            bootstrap(**bootstrap_kwargs)
        assert set(checked) == {root.id, dep.id}

    @pytest.mark.parametrize("retrigger", [False, True])
    def test_the_build_is_created_or_resumed_before_the_bootstrap_is_spawned(
        self, monkeypatch, default_in_memory_fs_target, retrigger
    ):
        """The build — its roots, and on a re-trigger its un-terminaled
        status — is in the registry before anything is airborne."""
        order: list[str] = []

        class Ordered(InMemoryRegistry):
            def build_create(self, **kwargs):
                order.append("build_create")
                return super().build_create(**kwargs)

            def build_resume(self, build_id, **kwargs):
                order.append("build_resume")
                return super().build_resume(build_id, **kwargs)

        class _Stub:
            def spawn(self, **kwargs):
                order.append("spawn")
                return "spawn-handle"

        monkeypatch.setattr(
            modal.Function, "from_name", staticmethod(lambda **kw: _Stub())
        )
        app = _make_app()
        registry = _deployed_registry(app, Ordered)
        root = SyncOnlyTask(name="order-root")
        build_id = None
        if retrigger:
            build_id = registry.build_create(root_task_ids=[str(root.id)]).id
            registry.build_fail(build_id, "earlier")
            order.clear()

        with registry_provider.override(registry):
            app.build_trigger(root, reactive=True, build_id=build_id)

        assert order == (
            ["build_resume", "spawn"] if retrigger else ["build_create", "spawn"]
        )

    def test_bootstrap_plans_seals_arms_and_spawns_the_first_tick(
        self, modal_function_stub, default_in_memory_fs_target
    ):
        app = _make_app()
        dep = SyncOnlyTask(name="bootstrap-dep")
        root = SyncOnlyTask(name="bootstrap-root", deps=(dep,))

        result, registry, _ = _trigger_reactive(
            app,
            root,
            stub=modal_function_stub,
            tick_kwargs={"linger_seconds": 30},
        )

        build = registry.builds[result.build_id]
        plan = registry.active_plan(result.build_id)
        assert plan is not None and plan.sealed_at is not None
        # Planned under the app's current deployment (the bootstrap ran
        # outside a deployment here; see the deployed-container test).
        (deployment,) = registry.deployment_list(app_name=app.name, current=True)
        assert plan.deployment_id == deployment.id
        # The whole walked DAG is a member of the plan: that is what every
        # tick rebuilds tasks from.
        assert _members(registry, result.build_id) == {str(root.id), str(dep.id)}
        # Armed in the REGISTRY, with the trigger's tick config.
        assert build.reactive_app_name == app.name
        assert build.reactive_tick_kwargs == {"linger_seconds": 30}
        # The first tick gets only the build id: its config comes from the
        # registry, so every tick of the build shares it.
        assert modal_function_stub["from_name"] == {
            "app_name": app.name,
            "name": "tick",
        }
        assert modal_function_stub["kwargs"] == {"build_id": str(result.build_id)}

    def test_a_deployed_bootstrap_plans_under_its_own_deployment(
        self, monkeypatch, modal_function_stub, default_in_memory_fs_target
    ):
        """In its container the bootstrap has ``STARDAG_DEPLOYMENT_ID`` baked
        in, and plans under exactly that."""
        app = _make_app()
        registry = InMemoryRegistry()
        own = registry.add_deployment(app_name=app.name)
        monkeypatch.setenv(STARDAG_DEPLOYMENT_ID_ENV, str(own))

        result, _, _ = _trigger_reactive(
            app,
            SyncOnlyTask(name="own-deployment-root"),
            stub=modal_function_stub,
            registry=registry,
        )

        plan = registry.active_plan(result.build_id)
        assert plan is not None and plan.deployment_id == own
        assert not registry.called("deployment_list")

    def test_the_settings_are_the_plans_and_are_applied_while_it_walks(
        self, monkeypatch, modal_function_stub, default_in_memory_fs_target
    ):
        """Settings are the second half of the scope: the plan is keyed by
        their hash, and they are in the environment while ``requires()`` and
        the completion checks run — and only then."""
        seen: list[str | None] = []

        class ReadsASetting(SyncOnlyTask):
            def requires(self):
                seen.append(os.environ.get("MY_SETTING"))
                return super().requires()

        app = _make_app()
        root = ReadsASetting(name="settings-root")
        result, registry, _ = _trigger_reactive(
            app, root, stub=modal_function_stub, settings={"MY_SETTING": "on"}
        )

        plan = registry.active_plan(result.build_id)
        assert plan is not None
        assert plan.settings_hash == settings_hash({"MY_SETTING": "on"})
        assert seen and set(seen) == {"on"}
        assert "MY_SETTING" not in os.environ

    @pytest.mark.parametrize("settings", [{"STARDAG_X": "1"}, {"MODAL_X": "1"}])
    def test_reserved_settings_are_refused_before_a_build_exists(
        self, modal_function_stub, settings
    ):
        app = _make_app()
        registry = _deployed_registry(app)
        with registry_provider.override(registry):
            with pytest.raises(SettingsError):
                app.build_trigger(
                    SyncOnlyTask(name="reserved"), reactive=True, settings=settings
                )
        assert not registry.called("build_create")
        assert "op" not in modal_function_stub

    def test_marker_is_written_only_after_the_plan_is_sealed(
        self, modal_function_stub, default_in_memory_fs_target
    ):
        """The ordering guarantee: ``reactive_app_name`` is the "reactively
        scheduled" marker and a tick no-ops without it, so it is written
        once the plan is complete and sealed."""
        observed: dict = {}

        class Observing(InMemoryRegistry):
            def build_set_reactive_meta(self, build_id, **kwargs):
                plan = self.active_plan(build_id)
                observed["sealed"] = plan is not None and plan.sealed_at is not None
                observed["members"] = set(self.members[plan.id]) if plan else set()
                return super().build_set_reactive_meta(build_id, **kwargs)

        app = _make_app()
        dep = SyncOnlyTask(name="marker-order-dep")
        root = SyncOnlyTask(name="marker-order-root", deps=(dep,))
        _trigger_reactive(
            app,
            root,
            stub=modal_function_stub,
            registry=_deployed_registry(app, Observing),
        )

        assert observed == {"sealed": True, "members": {str(root.id), str(dep.id)}}

    def test_reactive_rejects_build_kwargs(self, modal_function_stub):
        app = _make_app()
        with pytest.raises(TypeError, match="not supported with reactive"):
            app.build_trigger(
                MagicMock(spec=BaseTask),
                reactive=True,
                build_kwargs={"fail_mode": "CONTINUE"},
            )

    def test_reactive_rejects_worker_selector_override(self, modal_function_stub):
        """Later ticks always use the app's deployed selector — a
        per-trigger override would change routing mid-build."""
        app = _make_app()
        with pytest.raises(TypeError, match="worker_selector overrides"):
            app.build_trigger(
                MagicMock(spec=BaseTask),
                worker_selector=lambda t: "gpu",
                reactive=True,
            )

    def test_reactive_rejects_non_persistable_tick_kwargs(self, modal_function_stub):
        """tick_kwargs are persisted as JSON meta shared by all ticks —
        callables (e.g. a limit key selector) belong on the deployed app."""
        app = _make_app()
        with pytest.raises(TypeError, match="Unsupported tick_kwargs"):
            app.build_trigger(
                MagicMock(spec=BaseTask),
                reactive=True,
                tick_kwargs={"limit_key_selector": lambda t: []},
            )

    def test_reactive_requires_registry(self, modal_function_stub):
        app = _make_app()
        with registry_provider.override(NoOpRegistry()):
            with pytest.raises(RuntimeError, match="requires a configured registry"):
                app.build_trigger(
                    MagicMock(spec=BaseTask),
                    build_id=uuid4(),  # explicit id is NOT enough in reactive
                    reactive=True,
                )


class TestReactiveTriggerFailureLeavesNoOrphanBuild:
    """A reactive trigger mints a RUNNING build and walks away: every way the
    work can die before the first tick records a terminal BUILD_FAILED — on
    both sides of the spawn."""

    def test_spawn_failure_at_the_trigger_fails_the_build(
        self, monkeypatch, default_in_memory_fs_target
    ):
        app = _make_app("test-orphan-app")
        registry = _deployed_registry(app)
        monkeypatch.setattr(
            modal.Function,
            "from_name",
            staticmethod(MagicMock(side_effect=RuntimeError("no such app"))),
        )

        with registry_provider.override(registry):
            with pytest.raises(RuntimeError, match="no such app"):
                app.build_trigger(SyncOnlyTask(name="orphan-root"), reactive=True)

        (build,) = registry.builds.values()
        assert build.status == "failed"
        assert "no such app" in (build.error_message or "")

    def test_a_failed_resume_does_not_fail_the_build(
        self, modal_function_stub, default_in_memory_fs_target
    ):
        """On a re-trigger the build may still be *terminal* until
        ``build_resume`` lands, so a resume that failed must not stamp
        BUILD_FAILED over somebody else's outcome."""

        class RefusesResume(InMemoryRegistry):
            def build_resume(self, build_id, **kwargs):
                raise RuntimeError("resume rejected")

        app = _make_app("test-orphan-app")
        registry = _deployed_registry(app, RefusesResume)
        root = SyncOnlyTask(name="resume-fail-root")
        build_id = registry.build_create(root_task_ids=[str(root.id)]).id
        registry.build_cancel(build_id)

        with registry_provider.override(registry):
            with pytest.raises(RuntimeError, match="resume rejected"):
                app.build_trigger(root, build_id=build_id, reactive=True)

        assert registry.builds[build_id].status == "cancelled"
        assert not registry.called("build_fail")

    def test_bootstrap_failure_fails_the_build_and_propagates(
        self, modal_function_stub, default_in_memory_fs_target
    ):
        """Failures on the far side of the spawn are the bootstrap's to
        report: nobody else is watching that container."""

        class Down(InMemoryRegistry):
            def plan_create(self, build_id, **kwargs):
                raise RuntimeError("registry down")

        app = _make_app("test-orphan-app")
        registry = _deployed_registry(app, Down)
        with pytest.raises(RuntimeError, match="registry down"):
            _trigger_reactive(
                app,
                SyncOnlyTask(name="bootstrap-fail-root"),
                stub=modal_function_stub,
                registry=registry,
            )

        (build,) = registry.builds.values()
        assert build.status == "failed"
        assert "registry down" in (build.error_message or "")
        # Never armed: no tick acts on what the failure left behind.
        assert build.reactive_app_name is None

    def test_an_app_with_no_deployment_on_record_fails_the_build(
        self, modal_function_stub, default_in_memory_fs_target
    ):
        """Outside a deployment the bootstrap plans under the app's current
        deployment; with none on record there is no scope to plan under, and
        the build is failed with the remedy rather than left RUNNING."""
        from stardag.build import DeploymentResolutionError

        app = _make_app("test-undeployed-app")
        registry = InMemoryRegistry()
        with pytest.raises(DeploymentResolutionError, match="stardag modal deploy"):
            _trigger_reactive(
                app,
                SyncOnlyTask(name="undeployed-root"),
                stub=modal_function_stub,
                registry=registry,
            )
        (build,) = registry.builds.values()
        assert build.status == "failed"

    def test_a_failed_first_tick_spawn_fails_the_build(
        self, monkeypatch, default_in_memory_fs_target
    ):
        """An un-spawned first tick is not a partial success — without a
        watchdog nothing would ever move the build."""
        app = _make_app("test-orphan-app")
        registry = _deployed_registry(app)
        root = SyncOnlyTask(name="tick-spawn-fail-root")
        build_id = registry.build_create(root_task_ids=[str(root.id)]).id
        bootstrap = _finalize_capturing_functions(app)["bootstrap"]
        monkeypatch.setattr(
            modal.Function,
            "from_name",
            staticmethod(MagicMock(side_effect=RuntimeError("tick gone"))),
        )

        with registry_provider.override(registry):
            with pytest.raises(RuntimeError, match="tick gone"):
                bootstrap(build_id=str(build_id), tasks=[root])

        assert registry.builds[build_id].status == "failed"

    def test_a_registry_that_cannot_record_the_failure_never_masks_it(
        self, monkeypatch, default_in_memory_fs_target, caplog
    ):
        class CannotFail(InMemoryRegistry):
            def build_fail(self, build_id, error_message=None):
                raise RuntimeError("also down")

        app = _make_app("test-orphan-app")
        registry = _deployed_registry(app, CannotFail)
        monkeypatch.setattr(
            modal.Function,
            "from_name",
            staticmethod(MagicMock(side_effect=RuntimeError("no such app"))),
        )

        with registry_provider.override(registry):
            with caplog.at_level("ERROR"):
                # The ORIGINAL error propagates, not the bookkeeping one.
                with pytest.raises(RuntimeError, match="no such app"):
                    app.build_trigger(
                        SyncOnlyTask(name="double-fault-root"), reactive=True
                    )
        assert "Could not record BUILD_FAILED" in caplog.text


class TestReactiveDiscoveryPlacement:
    """``reactive_discovery`` decides *where* the identical bootstrap
    runs; ``"modal"`` is the default and ``"local"`` is the opt-out."""

    def test_default_is_modal(self):
        assert _make_app("test-placement-app").reactive_discovery == "modal"

    def test_unknown_placement_is_rejected_eagerly(self):
        with pytest.raises(ValueError, match="reactive_discovery"):
            _make_app("test-placement-app", reactive_discovery="remote")  # type: ignore[arg-type]

    def test_local_plans_here_under_the_apps_current_deployment(
        self, modal_function_stub, default_in_memory_fs_target
    ):
        """D13: a driver that is not the deployment, but whose tasks run on
        it, plans under the app's current deployment."""
        app = _make_app("test-placement-app", reactive_discovery="local")
        registry = InMemoryRegistry()
        registry.add_deployment(app_name=app.name, code_id="old")
        current = registry.add_deployment(app_name=app.name, code_id="new")
        root = SyncOnlyTask(name="local-discovery-root")

        with registry_provider.override(registry):
            result = app.build_trigger(root, reactive=True)

        plan = registry.active_plan(result.build_id)
        assert plan is not None and plan.sealed_at is not None
        assert plan.deployment_id == current
        assert str(root.id) in _members(registry, result.build_id)
        assert registry.builds[result.build_id].reactive_app_name == app.name
        # The handle is the first tick, since no bootstrap was spawned.
        assert modal_function_stub["from_name"] == {
            "app_name": app.name,
            "name": "tick",
        }
        assert result.function_call == "spawn-handle"

    def test_local_bootstrap_does_not_leak_the_settings_into_the_caller(
        self, modal_function_stub, default_in_memory_fs_target
    ):
        app = _make_app("test-placement-app", reactive_discovery="local")
        registry = _deployed_registry(app)
        assert "MY_LOCAL_SETTING" not in os.environ
        with registry_provider.override(registry):
            result = app.build_trigger(
                SyncOnlyTask(name="local-settings-root"),
                reactive=True,
                settings={"MY_LOCAL_SETTING": "x"},
            )
        assert "MY_LOCAL_SETTING" not in os.environ
        plan = registry.active_plan(result.build_id)
        assert plan is not None
        assert plan.settings_hash == settings_hash({"MY_LOCAL_SETTING": "x"})

    def test_the_bootstrap_imports_the_task_modules_before_it_walks(
        self, modal_function_stub, default_in_memory_fs_target
    ):
        """A tick rehydrates from the declared modules; the bootstrap imports
        them first too, so the walk and the pre-flight see the same classes."""
        order: list[str] = []

        async def fake_walk(roots, **kwargs):
            order.append("walk")
            raise RuntimeError("stop here")

        app = _make_app(
            "test-placement-app", reactive_discovery="local", task_modules=["my_pkg.*"]
        )
        with (
            registry_provider.override(_deployed_registry(app)),
            patch(
                "stardag.integration.modal._bootstrap.expand_task_module_patterns",
                return_value=["my_pkg.tasks"],
            ),
            patch(
                "stardag.integration.modal._bootstrap.import_task_modules",
                side_effect=lambda modules: order.append(f"import:{','.join(modules)}"),
            ),
            patch("stardag.integration.modal._bootstrap.walk_aio", fake_walk),
        ):
            with pytest.raises(RuntimeError, match="stop here"):
                app.build_trigger(SyncOnlyTask(name="import-order-root"), reactive=True)

        assert order == ["import:my_pkg.tasks", "walk"]

    def test_local_failure_also_leaves_no_orphan_build(
        self, modal_function_stub, default_in_memory_fs_target
    ):
        class Down(InMemoryRegistry):
            def plan_create(self, build_id, **kwargs):
                raise RuntimeError("registry down")

        app = _make_app("test-placement-app", reactive_discovery="local")
        registry = _deployed_registry(app, Down)
        with registry_provider.override(registry):
            with pytest.raises(RuntimeError, match="registry down"):
                app.build_trigger(SyncOnlyTask(name="local-fail-root"), reactive=True)

        (build,) = registry.builds.values()
        assert build.status == "failed"
