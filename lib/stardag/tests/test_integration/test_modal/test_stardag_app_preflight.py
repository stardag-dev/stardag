"""StardagApp reactive trigger, continued: the rehydration pre-flight
and task-module coverage (roots, incomplete tasks only, AliasTask bodies,
dynamic deps), and re-triggering a build. Helpers as in
``test_stardag_app_reactive.py``."""

from __future__ import annotations

import typing
import weakref

import pytest

try:
    import modal  # noqa: F401
except ImportError:
    pytest.skip("Skipping modal tests (import not available)", allow_module_level=True)

import stardag as _sd
from stardag.build import TaskModulesError
from stardag.build._deployment import STARDAG_DEPLOYMENT_ID_ENV
from stardag.integration.modal import FunctionSettings, StardagApp
from stardag.registry import registry_provider
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


class TestReactiveTriggerRootCoverageAdvisory:
    """Additive early feedback at the trigger: roots only, advisory, and
    never the check itself (that runs over the whole walked DAG, wherever
    the bootstrap runs)."""

    def _trigger(self, app, root):
        with registry_provider.override(_deployed_registry(app)):
            app.build_trigger(root, reactive=True)

    def test_uncovered_root_is_reported_before_anything_is_spawned(
        self, caplog, modal_function_stub, default_in_memory_fs_target
    ):
        app = _app_with_task_modules(
            "test-advisory-app", task_modules=[_UNCOVERING_PATTERN]
        )
        with caplog.at_level("WARNING"):
            self._trigger(app, SyncOnlyTask(name="advisory-root"))
        assert "not covered by this app's task_modules" in caplog.text
        assert "ROOT-TASKS-ONLY" in caplog.text

    def test_covered_root_says_nothing(
        self, caplog, modal_function_stub, default_in_memory_fs_target
    ):
        app = _app_with_task_modules(
            "test-advisory-app", task_modules=[SyncOnlyTask.__module__]
        )
        with caplog.at_level("WARNING"):
            self._trigger(app, SyncOnlyTask(name="advisory-ok-root"))
        assert "not covered" not in caplog.text

    def test_it_never_walks_the_dag(
        self, monkeypatch, modal_function_stub, default_in_memory_fs_target
    ):
        """Roots only, by construction: an advisory that traversed
        ``requires()`` would reintroduce the local walk, and could disagree
        with the real check."""
        app = _app_with_task_modules(
            "test-advisory-app", task_modules=[_UNCOVERING_PATTERN]
        )
        dep = SyncOnlyTask(name="advisory-dep")
        root = SyncOnlyTask(name="advisory-walk-root", deps=(dep,))
        requires_calls: list = []
        original = SyncOnlyTask.requires

        def spy(self):
            requires_calls.append(self.id)
            return original(self)

        monkeypatch.setattr(SyncOnlyTask, "requires", spy)
        self._trigger(app, root)
        assert requires_calls == []


class _PreflightUncoveredDep(_sd.Task[dict]):
    """A dep whose defining module the pre-flight tests leave undeclared."""

    name: str

    def run(self) -> None:
        self._save({"name": self.name})


class TestOnlyIncompleteTasksArePreflighted:
    """The check runs over the *incomplete* walked set: the walk stops at
    complete tasks and a tick only rebuilds ones it might schedule, so a
    completed dependency's class is irrelevant."""

    APP_MODULES = ["stardag.utils.testing.*"]

    def test_a_completed_dep_of_an_uncovered_class_does_not_refuse_the_build(
        self, modal_function_stub, default_in_memory_fs_target
    ):
        dep = _PreflightUncoveredDep(name="preflight-complete-dep")
        dep.run()  # writes its target -> the walk treats it as complete
        root = SyncOnlyTask(name="preflight-root", deps=(dep,))
        app = _app_with_task_modules(
            "tm-preflight-incomplete", task_modules=self.APP_MODULES
        )

        result, registry, _ = _trigger_reactive(app, root, stub=modal_function_stub)

        assert _armed(registry, result.build_id)

    def test_the_same_dep_incomplete_does_refuse_it(
        self, modal_function_stub, default_in_memory_fs_target
    ):
        """The control: the only difference is whether the dep is done."""
        dep = _PreflightUncoveredDep(name="preflight-incomplete-dep")
        root = SyncOnlyTask(name="preflight-root-2", deps=(dep,))
        app = _app_with_task_modules(
            "tm-preflight-incomplete-2", task_modules=self.APP_MODULES
        )

        with pytest.raises(TaskModulesError) as exc:
            _trigger_reactive(app, root, stub=modal_function_stub)

        assert str(dep.id) in str(exc.value)
        assert str(root.id) not in str(exc.value)


class _ElisionAliasedSource(_sd.Task[int]):
    """Module-level (pickle-able) source for the AliasTask test."""

    def run(self) -> None:
        self._save(7)


class _ElisionIntAlias(_sd.AliasTask[int]):
    """Concrete alias class (module-level so it pickles)."""


class _ElisionAliasConsumer(_sd.Task[int]):
    """Consumes an aliased upstream — its body therefore embeds the
    ``__aliased`` marker that rehydration refuses."""

    loads_int: _sd.TaskLoads[int]

    def run(self) -> None:
        self._save(self.loads_int.load() + 1)


class TestReactiveRehydrationPreflight:
    """The bootstrap refuses a build a scheduler tick could not drive: an
    instance body plus the deployment's importable code is the only way a
    tick gets a task object. The check runs against the module list baked in
    at deploy time, so these drive the trigger *and* the bootstrap."""

    def test_covered_round_tripping_tasks_pass(
        self, caplog, modal_function_stub, default_in_memory_fs_target
    ):
        app = _app_with_task_modules(
            "tm-preflight", task_modules=[SyncOnlyTask.__module__]
        )
        dep = SyncOnlyTask(name="preflight-dep")
        root = SyncOnlyTask(name="preflight-root", deps=(dep,))

        with caplog.at_level("INFO"):
            result, registry, _ = _trigger_reactive(app, root, stub=modal_function_stub)

        assert "2 task(s) reconstructable, 0 not" in caplog.text
        assert _armed(registry, result.build_id)

    def test_uncovered_tasks_refuse_the_build_naming_every_one(
        self, modal_function_stub, default_in_memory_fs_target
    ):
        """Loud, and from the bootstrap: the ``TaskModulesError`` propagates
        AND fails the build, before anything is registered."""
        app = _app_with_task_modules(
            "tm-preflight-uncovered", task_modules=[_UNCOVERING_PATTERN]
        )
        dep = SyncOnlyTask(name="preflight-uncovered-dep")
        root = SyncOnlyTask(name="preflight-uncovered-root", deps=(dep,))
        registry = _deployed_registry(app)

        with pytest.raises(TaskModulesError) as exc:
            _trigger_reactive(app, root, stub=modal_function_stub, registry=registry)

        message = str(exc.value)
        assert "2 task(s)" in message
        assert "(and 1 more task(s), same reason)" in message
        assert str(root.id) in message or str(dep.id) in message
        assert "not covered by task_modules" in message
        assert "stardag.utils.testing.*" in message
        (build,) = registry.builds.values()
        assert build.status == "failed"
        assert build.reactive_app_name is None
        # Refused before the static phase: no plan was registered.
        assert registry.plans == {}

    def test_inferred_task_modules_count(
        self, monkeypatch, modal_function_stub, default_in_memory_fs_target
    ):
        """An app whose tasks live under its own root package is exactly the
        app inference serves: inferred patterns cover like declared ones."""
        from stardag.integration.modal import _app as app_module

        monkeypatch.setattr(
            app_module, "_infer_task_module_patterns", lambda *a, **k: ("stardag.*",)
        )
        app = _app_with_task_modules("tm-preflight-inferred")
        assert app.task_modules == ("stardag.*",)

        result, registry, _ = _trigger_reactive(
            app, SyncOnlyTask(name="preflight-inferred-root"), stub=modal_function_stub
        )
        assert _armed(registry, result.build_id)

    def test_an_app_with_no_task_modules_is_refused_before_a_build_exists(
        self, modal_function_stub, default_in_memory_fs_target
    ):
        """With no task modules a tick imports nothing and could rebuild no
        task, so the refusal is synchronous, before a build is minted."""
        app = _app_with_task_modules("tm-preflight-optout", task_modules=[])
        registry = _deployed_registry(app)

        with registry_provider.override(registry):
            with pytest.raises(TaskModulesError, match="needs task_modules"):
                app.build_trigger(
                    SyncOnlyTask(name="preflight-optout-root"), reactive=True
                )

        assert not registry.called("build_create")

    def test_a_resident_build_on_the_same_app_is_unaffected(
        self, modal_function_stub, default_in_memory_fs_target
    ):
        """The refusal is about reactive scheduling only: a resident
        orchestrator holds the real task objects."""
        app = _app_with_task_modules("tm-preflight-resident", task_modules=[])
        _finalize_capturing_functions(app)

        with registry_provider.override(InMemoryRegistry()):
            result = app.build_trigger(SyncOnlyTask(name="preflight-resident-root"))

        assert result.build_id is not None
        assert modal_function_stub["from_name"]["name"] == "build"

    def test_an_alias_task_dag_is_refused(
        self, modal_function_stub, default_in_memory_fs_target
    ):
        """AliasTask is pickle-bound by design: its ``loads_type`` is pickled
        bytes a tick must never unpickle from registry data. The walk's
        once-per-instance stability check refuses the body before the
        pre-flight is reached, and the build is failed. An ``AliasTask`` has
        no ``run()``, so refusing loses nothing."""
        from stardag.exceptions import UnstableSerializationError

        source = _ElisionAliasedSource()
        source.run()
        alias = _ElisionIntAlias(aliased=_sd.AliasedMetadata.from_task(source))
        root = _ElisionAliasConsumer(loads_int=alias)
        app = _app_with_task_modules("tm-preflight-alias", task_modules=[__name__])
        registry = _deployed_registry(app)

        with pytest.raises(UnstableSerializationError, match="AliasTask"):
            _trigger_reactive(app, root, stub=modal_function_stub, registry=registry)

        (build,) = registry.builds.values()
        assert build.status == "failed"
        assert build.reactive_app_name is None


class TestDynamicDepCoverage:
    """Dynamic deps yielded inside a worker get the same coverage check —
    as a warning, because the parent has already run."""

    def test_covered_dynamic_deps_warn_nothing(
        self, caplog, default_in_memory_fs_target
    ):
        from stardag.build import set_declared_task_module_patterns
        from stardag.integration.modal._reporter import _WorkerLifecycleReporter

        set_declared_task_module_patterns([SyncOnlyTask.__module__])
        try:
            with caplog.at_level("WARNING"):
                _WorkerLifecycleReporter._warn_uncovered(
                    [SyncOnlyTask(name="dyn-dep-covered")]
                )
        finally:
            set_declared_task_module_patterns([])

        assert "not covered by this app's task_modules" not in caplog.text

    def test_uncovered_dynamic_deps_warn_once_per_class(
        self, caplog, default_in_memory_fs_target
    ):
        """A warning, not a raise: failing the parent's bookkeeping would
        throw its work away and still leave the dependency unschedulable."""
        from stardag.build import set_declared_task_module_patterns
        from stardag.build._task_modules import _warned_classes
        from stardag.integration.modal._reporter import _WorkerLifecycleReporter
        from stardag.utils.testing.helper_tasks import AsyncOnlyTask

        _warned_classes.discard(
            f"{AsyncOnlyTask.__module__}.{AsyncOnlyTask.__qualname__}"
        )
        set_declared_task_module_patterns(["acme_pipelines.*"])
        try:
            with caplog.at_level("WARNING"):
                _WorkerLifecycleReporter._warn_uncovered(
                    [AsyncOnlyTask(name="dyn-dep-uncovered-1")]
                )
                _WorkerLifecycleReporter._warn_uncovered(
                    [AsyncOnlyTask(name="dyn-dep-uncovered-2")]
                )
        finally:
            set_declared_task_module_patterns([])

        assert caplog.text.count("not covered by this app's task_modules") == 1
        assert "a scheduler tick excludes each one it reaches" in caplog.text


class TestReactiveRetrigger:
    """Re-triggering a reactive build: the build is resumed from the
    trigger, the bootstrap re-plans under the same scope — the plan is
    reused and its members' observations re-sent — and the reactive
    metadata is updated (a bare re-trigger keeps the stored tick config)."""

    def test_retrigger_resumes_and_reuses_the_plan(
        self, modal_function_stub, default_in_memory_fs_target
    ):
        app = _make_app("test-retrigger-app")
        registry = _deployed_registry(app)
        root = SyncOnlyTask(name="rt-root")

        result, _, _ = _trigger_reactive(
            app,
            root,
            stub=modal_function_stub,
            registry=registry,
            tick_kwargs={"fail_mode": "continue"},
        )
        build_id = result.build_id
        (plan,) = registry.plans.values()
        assert registry.builds[build_id].reactive_tick_kwargs == {
            "fail_mode": "continue"
        }

        registry.build_fail(build_id, "gave up")
        _trigger_reactive(
            app,
            root,
            stub=modal_function_stub,
            registry=registry,
            build_id=build_id,
            tick_kwargs={"linger_seconds": 5},
        )

        build = registry.builds[build_id]
        assert build.status == "running"
        # The trigger's resume carries the reactive trigger's metadata; the
        # bootstrap's names the scope it plans under.
        trigger_resume, bootstrap_resume = registry.calls_to("build_resume")[-2:]
        assert trigger_resume["deployment_id"] is None
        assert bootstrap_resume["deployment_id"] == plan.deployment_id
        assert build.executor_metadata is not None
        assert build.executor_metadata["function_name"] == "bootstrap"
        # Same scope, same plan.
        assert list(registry.plans) == [plan.id]
        assert build.reactive_tick_kwargs == {"linger_seconds": 5}

        # A BARE re-trigger passes tick_kwargs=None, which leaves the stored
        # config untouched (not reset to {}).
        _trigger_reactive(
            app, root, stub=modal_function_stub, registry=registry, build_id=build_id
        )
        assert registry.calls_to("build_set_reactive_meta")[-1]["tick_kwargs"] is None
        assert registry.builds[build_id].reactive_tick_kwargs == {"linger_seconds": 5}

    def test_a_retrigger_with_other_settings_plans_a_new_scope(
        self, modal_function_stub, default_in_memory_fs_target
    ):
        """Settings are half the scope: other settings are another plan of
        the same build, activated in place of the old one."""
        app = _make_app("test-retrigger-app")
        registry = _deployed_registry(app)
        root = SyncOnlyTask(name="rt-settings-root")
        result, _, _ = _trigger_reactive(
            app, root, stub=modal_function_stub, registry=registry, settings={"A": "1"}
        )
        _trigger_reactive(
            app,
            root,
            stub=modal_function_stub,
            registry=registry,
            build_id=result.build_id,
            settings={"A": "2"},
        )
        hashes = sorted(p.settings_hash for p in registry.plans.values())
        assert hashes == sorted([settings_hash({"A": "1"}), settings_hash({"A": "2"})])
        active = registry.active_plan(result.build_id)
        assert active is not None and active.settings_hash == settings_hash({"A": "2"})
