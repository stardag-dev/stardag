"""Tests for StardagApp build_function and run_function customization."""

import contextlib
import importlib.util
import io
import pickletools
import sys
import threading
import time
import typing

import pytest

try:
    import modal  # noqa: F401
except ImportError:
    pytest.skip("Skipping modal tests (import not available)", allow_module_level=True)

from unittest.mock import MagicMock, patch
from uuid import uuid4

import stardag as _sd
from stardag import BaseTask
from stardag.build._base import BuildSummary
from stardag.exceptions import StardagError
from stardag.integration.modal import (
    Builder,
    BuildFunction,
    FunctionSettings,
    RunFunction,
    Runner,
    SerializedCallablePlacementError,
    StardagApp,
)
from stardag.integration.modal._builder import _default_build
from stardag.integration.modal import _container_setup as _container_setup_module
from stardag.integration.modal._container_setup import (
    _loading_deploy_entrypoint,
    _reset_container_setup_for_testing,
)
from stardag.integration.modal._runner import _default_run
from stardag.registry import NoOpRegistry, RegistryABC, registry_provider


def _make_image() -> modal.Image:
    return modal.Image.debian_slim()


def _finalize_capturing_functions(app: StardagApp) -> dict:
    """finalize() ``app`` with ``modal.App.function`` stubbed out.

    Returns the registered callables by function name, so a test can
    invoke the deployed ``bootstrap`` / ``tick`` bodies in-process.
    """
    captured: dict = {}

    def capture_function(**kwargs):
        def decorator(fn):
            captured[kwargs.get("name", "unknown")] = fn
            return fn

        return decorator

    app.modal_app.function = capture_function  # type: ignore[assignment]
    with patch("stardag.integration.modal._app.get_target_roots_volumes") as mv:
        mv.return_value = MagicMock(by_volume_name={}, by_root_key={})
        app.finalize()
    return captured


def _trigger_reactive(
    app: StardagApp,
    tasks,
    *,
    stub: dict,
    registry=None,
    build_id=None,
    tick_kwargs=None,
    run_bootstrap: bool = True,
):
    """Trigger reactively, then run the spawned ``bootstrap`` in-process.

    The trigger only spawns now, so a test that wants the DAG registered
    and the task store written has to run the bootstrap the trigger
    spawned — which is what this does, with exactly the kwargs the
    trigger passed. ``run_bootstrap=False`` stops at the spawn, which is
    how tests assert that the trigger itself does no discovery.

    Returns ``(result, registry, bootstrap_kwargs)``.
    """
    if registry is None:
        registry = MagicMock(spec=RegistryABC)
        registry.build_start.return_value = build_id or uuid4()
        registry.task_register_bulk_aio.return_value = None
    bootstrap = _finalize_capturing_functions(app)["bootstrap"]
    with registry_provider.override(registry):
        result = app.build_trigger(
            tasks, reactive=True, build_id=build_id, tick_kwargs=tick_kwargs
        )
        bootstrap_kwargs = dict(stub["kwargs"])
        if run_bootstrap:
            bootstrap(**bootstrap_kwargs)
    return result, registry, bootstrap_kwargs


class TestStardagAppCustomFunctions:
    def test_defaults_to_builder_and_runner_instances(self):
        """When no custom functions given, defaults are Builder() and Runner()."""
        app = StardagApp(
            "test-app",
            builder_settings=FunctionSettings(image=_make_image()),
            worker_settings={"default": FunctionSettings(image=_make_image())},
        )
        assert app._build_function is _default_build
        assert app._run_function is _default_run

    def test_custom_build_function(self):
        """Custom build_function is stored."""

        def my_build(
            tasks, worker_selector, app_name, build_kwargs=None
        ) -> BuildSummary:  # type: ignore[empty-body]
            ...

        app = StardagApp(
            "test-app",
            build_function=my_build,
            builder_settings=FunctionSettings(image=_make_image()),
            worker_settings={"default": FunctionSettings(image=_make_image())},
        )
        assert app._build_function is my_build

    def test_custom_run_function(self):
        """Custom run_function is stored."""

        def my_run(task):
            pass

        app = StardagApp(
            "test-app",
            run_function=my_run,
            builder_settings=FunctionSettings(image=_make_image()),
            worker_settings={"default": FunctionSettings(image=_make_image())},
        )
        assert app._run_function is my_run

    def test_custom_builder_subclass(self):
        """A Builder subclass can be passed as build_function."""

        class MyBuilder(Builder):
            def setup(self, tasks):
                pass  # custom setup

        my_builder = MyBuilder()
        app = StardagApp(
            "test-app",
            build_function=my_builder,
            builder_settings=FunctionSettings(image=_make_image()),
            worker_settings={"default": FunctionSettings(image=_make_image())},
        )
        assert app._build_function is my_builder

    def test_custom_runner_subclass(self):
        """A Runner subclass can be passed as run_function."""

        class MyRunner(Runner):
            def setup(self, task):
                pass  # GPU init etc.

        my_runner = MyRunner()
        app = StardagApp(
            "test-app",
            run_function=my_runner,
            builder_settings=FunctionSettings(image=_make_image()),
            worker_settings={"default": FunctionSettings(image=_make_image())},
        )
        assert app._run_function is my_runner

    @patch("stardag.integration.modal._app.get_target_roots_volumes")
    def test_finalize_registers_build_wrapper_that_delegates(self, mock_volumes):
        """finalize() registers a wrapper that delegates to the build function."""
        mock_volumes.return_value = MagicMock(by_volume_name={}, by_root_key={})

        calls = []

        def my_build(
            tasks, worker_selector, app_name, build_kwargs=None
        ) -> BuildSummary:  # type: ignore[empty-body]
            calls.append(("build", tasks, app_name))

        app = StardagApp(
            "test-app",
            build_function=my_build,
            builder_settings=FunctionSettings(image=_make_image()),
            worker_settings={"default": FunctionSettings(image=_make_image())},
        )

        registered_fns: dict = {}

        def capture_function(**kwargs):
            name = kwargs.get("name", "unknown")

            def decorator(fn):
                registered_fns[name] = fn
                return fn

            return decorator

        app.modal_app.function = capture_function  # type: ignore[assignment]
        app.finalize()

        # The registered function is a wrapper (real function for Modal compat)
        import inspect

        assert inspect.isfunction(registered_fns["build"])
        # Calling it delegates to my_build
        registered_fns["build"]("task", "selector", "app", None)
        assert calls == [("build", "task", "app")]

    @patch("stardag.integration.modal._app.get_target_roots_volumes")
    def test_finalize_wrapper_forwards_build_kwargs_as_keyword(self, mock_volumes):
        """The Modal wrapper forwards ``build_kwargs`` to the user's build_fn
        as a keyword arg, so custom functions with keyword-only build_kwargs
        are also supported."""
        mock_volumes.return_value = MagicMock(by_volume_name={}, by_root_key={})

        captured: dict = {}

        # build_kwargs is keyword-only — would TypeError if forwarded
        # positionally. (This deliberately diverges from BuildFunction's
        # exact protocol signature, which has build_kwargs positional-or-
        # keyword; the test verifies the wrapper supports either shape.)
        def my_build(tasks, worker_selector, app_name, *, build_kwargs=None):
            captured["build_kwargs"] = build_kwargs

        app = StardagApp(
            "test-app",
            build_function=my_build,  # type: ignore[arg-type]
            builder_settings=FunctionSettings(image=_make_image()),
            worker_settings={"default": FunctionSettings(image=_make_image())},
        )

        registered_fns: dict = {}

        def capture_function(**kwargs):
            name = kwargs.get("name", "unknown")

            def decorator(fn):
                registered_fns[name] = fn
                return fn

            return decorator

        app.modal_app.function = capture_function  # type: ignore[assignment]
        app.finalize()

        registered_fns["build"]("task", "selector", "app", {"fail_mode": "x"})
        assert captured["build_kwargs"] == {"fail_mode": "x"}

    @patch("stardag.integration.modal._app.get_target_roots_volumes")
    def test_finalize_registers_run_wrapper_for_all_workers(self, mock_volumes):
        """finalize() registers run wrappers for all workers."""
        mock_volumes.return_value = MagicMock(by_volume_name={}, by_root_key={})

        calls = []

        def my_run(task):
            calls.append(task)

        app = StardagApp(
            "test-app",
            run_function=my_run,
            builder_settings=FunctionSettings(image=_make_image()),
            worker_settings={
                "default": FunctionSettings(image=_make_image()),
                "gpu": FunctionSettings(image=_make_image()),
            },
        )

        registered_fns: dict = {}

        def capture_function(**kwargs):
            name = kwargs.get("name", "unknown")

            def decorator(fn):
                registered_fns[name] = fn
                return fn

            return decorator

        app.modal_app.function = capture_function  # type: ignore[assignment]
        app.finalize()

        import inspect

        assert inspect.isfunction(registered_fns["worker_default"])
        assert inspect.isfunction(registered_fns["worker_gpu"])
        # Both wrappers delegate to my_run
        registered_fns["worker_default"]("task1")
        registered_fns["worker_gpu"]("task2")
        assert calls == ["task1", "task2"]

    @patch("stardag.integration.modal._app.get_target_roots_volumes")
    def test_finalize_wrappers_are_real_functions(self, mock_volumes):
        """Registered wrappers are real functions (Modal compatibility)."""
        mock_volumes.return_value = MagicMock(by_volume_name={}, by_root_key={})

        app = StardagApp(
            "test-app",
            builder_settings=FunctionSettings(image=_make_image()),
            worker_settings={"default": FunctionSettings(image=_make_image())},
        )

        registered_fns: dict = {}

        def capture_function(**kwargs):
            name = kwargs.get("name", "unknown")

            def decorator(fn):
                registered_fns[name] = fn
                return fn

            return decorator

        app.modal_app.function = capture_function  # type: ignore[assignment]
        app.finalize()

        import inspect

        # All registered functions must be real functions for Modal's is_async()
        for name, fn in registered_fns.items():
            assert inspect.isfunction(fn), f"{name} is {type(fn)}, not a function"


class TestBuilderAndRunnerProtocols:
    def test_builder_satisfies_build_function(self):
        """Builder instance satisfies BuildFunction protocol."""
        fn: BuildFunction = Builder()
        assert callable(fn)

    def test_runner_satisfies_run_function(self):
        """Runner instance satisfies RunFunction protocol."""
        fn: RunFunction = Runner()
        assert callable(fn)

    def test_plain_function_satisfies_build_function(self):
        """A plain function with matching signature satisfies BuildFunction."""

        def my_build(
            tasks, worker_selector, app_name, build_kwargs=None
        ) -> BuildSummary:  # type: ignore[empty-body]
            ...

        fn: BuildFunction = my_build
        assert callable(fn)

    def test_plain_function_satisfies_run_function(self):
        """A plain function with matching signature satisfies RunFunction."""

        def my_run(task):
            pass

        fn: RunFunction = my_run
        assert callable(fn)


# ---------------------------------------------------------------------------
# Builder: setup/build/teardown orchestration
# ---------------------------------------------------------------------------


class TestBuilderOrchestration:
    def test_calls_setup_build_teardown_in_order(self):
        calls = []
        mock_summary = MagicMock()
        mock_summary.status = MagicMock(value="SUCCESS")

        class TracingBuilder(Builder):
            def setup(self, tasks):
                calls.append("setup")

            def build(self, tasks, task_executor, build_kwargs=None):
                calls.append("build")
                return mock_summary

            def teardown(self, tasks, summary_or_exception):
                calls.append("teardown")

        builder = TracingBuilder()
        mock_task = MagicMock()
        builder(mock_task, MagicMock(), "app")

        assert calls == ["setup", "build", "teardown"]

    def test_teardown_called_on_build_exception(self):
        calls = []

        class FailingBuilder(Builder):
            def setup(self, tasks):
                calls.append("setup")

            def build(self, tasks, task_executor, build_kwargs=None):
                calls.append("build")
                raise RuntimeError("boom")

            def teardown(self, tasks, summary_or_exception):
                calls.append(("teardown", type(summary_or_exception).__name__))

        builder = FailingBuilder()
        with pytest.raises(RuntimeError, match="boom"):
            builder(MagicMock(), MagicMock(), "app")

        assert calls == ["setup", "build", ("teardown", "RuntimeError")]

    def test_teardown_receives_summary_on_success(self):
        received = {}

        mock_summary = MagicMock()
        mock_summary.status = MagicMock()
        mock_summary.status.value = "SUCCESS"

        class InspectingBuilder(Builder):
            def setup(self, tasks):
                pass

            def build(self, tasks, task_executor, build_kwargs=None):
                return mock_summary

            def teardown(self, tasks, summary_or_exception):
                received["arg"] = summary_or_exception

        builder = InspectingBuilder()
        result = builder(MagicMock(), MagicMock(), "app")

        assert received["arg"] is mock_summary
        assert result is mock_summary


# ---------------------------------------------------------------------------
# Runner: setup/run/teardown orchestration
# ---------------------------------------------------------------------------


class TestRunnerOrchestration:
    def test_calls_setup_run_teardown_in_order(self):
        calls = []

        class TracingRunner(Runner):
            def setup(self, task):
                calls.append("setup")

            def run(self, task):
                calls.append("run")

            def teardown(self, task, exception):
                calls.append("teardown")

        runner = TracingRunner()
        mock_task = MagicMock()
        runner(mock_task)

        assert calls == ["setup", "run", "teardown"]

    def test_teardown_called_on_run_exception(self):
        calls = []

        class FailingRunner(Runner):
            def setup(self, task):
                calls.append("setup")

            def run(self, task):
                raise ValueError("task failed")

            def teardown(self, task, exception):
                calls.append(("teardown", type(exception).__name__))

        runner = FailingRunner()
        with pytest.raises(ValueError, match="task failed"):
            runner(MagicMock())

        assert calls == ["setup", ("teardown", "ValueError")]

    def test_teardown_receives_none_on_success(self):
        received = {}

        class InspectingRunner(Runner):
            def setup(self, task):
                pass

            def run(self, task):
                pass

            def teardown(self, task, exception):
                received["exception"] = exception

        runner = InspectingRunner()
        runner(MagicMock())

        assert received["exception"] is None


# ---------------------------------------------------------------------------
# Builder.build_kwargs forwarding
# ---------------------------------------------------------------------------


class TestBuilderBuildKwargs:
    """Builder forwards ``build_kwargs`` to ``stardag.build``."""

    def test_default_build_forwards_build_kwargs(self, monkeypatch):
        from stardag.build import FailMode
        from stardag.integration.modal import _builder as builder_module

        captured: dict = {}

        def fake_build(tasks, **kwargs):
            captured["tasks"] = tasks
            captured["kwargs"] = kwargs
            return None

        monkeypatch.setattr(builder_module, "build", fake_build)

        builder = builder_module.Builder()
        executor = MagicMock()
        root = MagicMock()
        builder.build(
            root,
            executor,
            build_kwargs={
                "fail_mode": FailMode.CONTINUE,
                "register_all": True,
            },
        )
        assert captured["tasks"] is root
        assert captured["kwargs"]["task_executor"] is executor
        assert captured["kwargs"]["fail_mode"] == FailMode.CONTINUE
        assert captured["kwargs"]["register_all"] is True

    def test_default_build_no_build_kwargs(self, monkeypatch):
        """Backwards-compat: omitting build_kwargs still works."""
        from stardag.integration.modal import _builder as builder_module

        captured: dict = {}

        def fake_build(tasks, **kwargs):
            captured["kwargs"] = kwargs
            return None

        monkeypatch.setattr(builder_module, "build", fake_build)

        builder = builder_module.Builder()
        builder.build(MagicMock(), MagicMock())
        # Only task_executor — no leaked kwargs from a None build_kwargs.
        assert set(captured["kwargs"].keys()) == {"task_executor"}

    @pytest.mark.parametrize("reserved_key", ["tasks", "task_executor"])
    def test_default_build_rejects_reserved_keys(self, reserved_key):
        builder = Builder()
        with pytest.raises(TypeError, match=reserved_key):
            builder.build(MagicMock(), MagicMock(), build_kwargs={reserved_key: "x"})

    def test_call_forwards_build_kwargs_to_build(self):
        """Builder.__call__ passes build_kwargs through to Builder.build()."""
        captured: dict = {}

        class CapturingBuilder(Builder):
            def build(self, tasks, task_executor, build_kwargs=None):
                captured["tasks"] = tasks
                captured["build_kwargs"] = build_kwargs
                return None

        builder = CapturingBuilder()
        root = MagicMock()
        builder(
            root,
            lambda t: "default",
            "test-app",
            build_kwargs={"fail_mode": "FAIL_FAST"},
        )
        assert captured["tasks"] is root
        assert captured["build_kwargs"] == {"fail_mode": "FAIL_FAST"}

    def test_call_default_build_kwargs_is_none(self):
        """When build_kwargs is omitted, Builder.build receives None."""
        captured: dict = {}

        class CapturingBuilder(Builder):
            def build(self, tasks, task_executor, build_kwargs=None):
                captured["build_kwargs"] = build_kwargs
                return None

        builder = CapturingBuilder()
        builder(MagicMock(), lambda t: "default", "test-app")
        assert captured["build_kwargs"] is None


# ---------------------------------------------------------------------------
# StardagApp.build_spawn / build_remote dispatch
# ---------------------------------------------------------------------------


class TestStardagAppBuildSpawnRemote:
    """build_spawn / build_remote forward tasks (single or sequence) and
    build_kwargs to the remote Modal function."""

    def _make_app(self):
        return StardagApp(
            "test-spawn-remote-app",
            builder_settings=FunctionSettings(image=_make_image()),
            worker_settings={"default": FunctionSettings(image=_make_image())},
        )

    def test_build_remote_single_task(self, modal_function_stub):
        captured = modal_function_stub
        app = self._make_app()
        root = MagicMock()

        result = app.build_remote(root)

        assert result == "remote-result"
        assert captured["op"] == "remote"
        assert captured["kwargs"]["tasks"] is root
        assert captured["kwargs"]["app_name"] == app.name
        assert captured["kwargs"]["build_kwargs"] is None

    def test_build_remote_sequence_of_tasks(self, modal_function_stub):
        captured = modal_function_stub
        app = self._make_app()
        roots = [MagicMock(), MagicMock()]

        app.build_remote(roots)

        assert captured["kwargs"]["tasks"] is roots

    def test_build_remote_forwards_build_kwargs(self, modal_function_stub):
        captured = modal_function_stub
        app = self._make_app()

        app.build_remote(MagicMock(), build_kwargs={"fail_mode": "CONTINUE"})

        assert captured["kwargs"]["build_kwargs"] == {"fail_mode": "CONTINUE"}

    def test_build_spawn_sequence_and_build_kwargs(self, modal_function_stub):
        captured = modal_function_stub
        app = self._make_app()
        roots = [MagicMock(), MagicMock()]

        result = app.build_spawn(roots, build_kwargs={"register_all": True})

        assert result == "spawn-handle"
        assert captured["op"] == "spawn"
        assert captured["kwargs"]["tasks"] is roots
        assert captured["kwargs"]["build_kwargs"] == {"register_all": True}


# ---------------------------------------------------------------------------
# StardagApp.build_trigger
# ---------------------------------------------------------------------------


class TestStardagAppBuildTrigger:
    """build_trigger mints the build id at the trigger point and passes it
    to the remote build function as ``resume_build_id``, so restarts of the
    build function resume the same build."""

    def _make_app(self):
        return StardagApp(
            "test-trigger-app",
            builder_settings=FunctionSettings(image=_make_image()),
            worker_settings={"default": FunctionSettings(image=_make_image())},
        )

    def test_mints_build_id_and_injects_resume_build_id(self, modal_function_stub):
        captured = modal_function_stub
        app = self._make_app()
        root = MagicMock(spec=BaseTask)
        build_id = uuid4()
        registry = MagicMock(spec=RegistryABC)
        registry.build_start.return_value = build_id

        with registry_provider.override(registry):
            result = app.build_trigger(root, description="a build")

        registry.build_start.assert_called_once_with(
            root_tasks=[root],
            description="a build",
            executor_metadata={
                "kind": "modal",
                "app_name": app.name,
                "function_name": "build",
                "reactive": False,
                "workspace": "test-workspace",
                "environment": "test-env",
            },
        )
        assert result.build_id == build_id
        assert result.function_call == "spawn-handle"
        assert captured["op"] == "spawn"
        assert captured["kwargs"]["tasks"] is root
        assert captured["kwargs"]["build_kwargs"] == {"resume_build_id": build_id}

    def test_sequence_of_roots_passed_as_list_to_registry(self, modal_function_stub):
        app = self._make_app()
        roots = [MagicMock(spec=BaseTask), MagicMock(spec=BaseTask)]
        registry = MagicMock(spec=RegistryABC)
        registry.build_start.return_value = uuid4()

        with registry_provider.override(registry):
            app.build_trigger(roots)

        assert registry.build_start.call_count == 1
        call_kwargs = registry.build_start.call_args.kwargs
        assert call_kwargs["root_tasks"] is not None
        assert list(call_kwargs["root_tasks"]) == roots
        assert call_kwargs["description"] is None

    def test_explicit_build_id_skips_registry(self, modal_function_stub):
        captured = modal_function_stub
        app = self._make_app()
        build_id = uuid4()

        # NoOpRegistry active — must not be consulted (and must not raise)
        with registry_provider.override(NoOpRegistry()):
            result = app.build_trigger(MagicMock(spec=BaseTask), build_id=build_id)

        assert result.build_id == build_id
        assert captured["kwargs"]["build_kwargs"] == {"resume_build_id": build_id}

    def test_merges_build_kwargs(self, modal_function_stub):
        captured = modal_function_stub
        app = self._make_app()
        build_id = uuid4()

        with registry_provider.override(NoOpRegistry()):
            app.build_trigger(
                MagicMock(spec=BaseTask),
                build_id=build_id,
                build_kwargs={"register_all": True},
            )

        assert captured["kwargs"]["build_kwargs"] == {
            "register_all": True,
            "resume_build_id": build_id,
        }

    def test_raises_without_registry(self, modal_function_stub):
        app = self._make_app()

        with registry_provider.override(NoOpRegistry()):
            with pytest.raises(RuntimeError, match="requires a configured registry"):
                app.build_trigger(MagicMock(spec=BaseTask))

    def test_rejects_resume_build_id_in_build_kwargs(self, modal_function_stub):
        app = self._make_app()

        with pytest.raises(TypeError, match="resume_build_id"):
            app.build_trigger(
                MagicMock(spec=BaseTask),
                build_id=uuid4(),
                build_kwargs={"resume_build_id": uuid4()},
            )

    def test_does_not_mutate_caller_build_kwargs(self, modal_function_stub):
        app = self._make_app()
        caller_kwargs: dict = {"register_all": True}

        with registry_provider.override(NoOpRegistry()):
            app.build_trigger(
                MagicMock(spec=BaseTask),
                build_id=uuid4(),
                build_kwargs=caller_kwargs,
            )

        assert caller_kwargs == {"register_all": True}


# ---------------------------------------------------------------------------
# StardagApp.build_trigger(reactive=True) + tick/watchdog registration
# ---------------------------------------------------------------------------


class TestStardagAppReactiveTrigger:
    def _make_app(self):
        return StardagApp(
            "test-reactive-app",
            builder_settings=FunctionSettings(image=_make_image()),
            worker_settings={"default": FunctionSettings(image=_make_image())},
        )

    def test_trigger_spawns_bootstrap_with_the_roots_by_value(
        self, modal_function_stub, default_in_memory_fs_target
    ):
        """The trigger's whole job: mint the build, register the roots,
        hand the root tasks to the deployed ``bootstrap`` by value."""
        from uuid import uuid4 as _uuid4

        from stardag.utils.testing.helper_tasks import SyncOnlyTask

        app = self._make_app()
        build_id = _uuid4()
        registry = MagicMock(spec=RegistryABC)
        registry.build_start.return_value = build_id
        dep = SyncOnlyTask(name="reactive-dep")
        root = SyncOnlyTask(name="reactive-root", deps=(dep,))

        result, _, bootstrap_kwargs = _trigger_reactive(
            app,
            root,
            stub=modal_function_stub,
            registry=registry,
            tick_kwargs={"linger_seconds": 30},
            run_bootstrap=False,
        )

        assert result.build_id == build_id
        assert modal_function_stub["from_name"] == {
            "app_name": app.name,
            "name": "bootstrap",
        }
        assert modal_function_stub["op"] == "spawn"
        # Roots ride along BY VALUE (cloudpickled into the call), exactly
        # as build_spawn passes ``tasks=`` to the builder.
        assert bootstrap_kwargs["tasks"] == [root]
        assert bootstrap_kwargs["build_id"] == str(build_id)
        assert bootstrap_kwargs["tick_kwargs"] == {"linger_seconds": 30}
        # The returned handle is the bootstrap call — the one thing this
        # trigger actually spawned.
        assert result.function_call == "spawn-handle"

    def test_trigger_does_no_discovery_locally(
        self, monkeypatch, modal_function_stub, default_in_memory_fs_target
    ):
        """The regression this whole change exists for.

        Discovery is one ``complete_aio()`` — a target existence check —
        per task. Against a ``modalvol://`` root that is a rate-limited
        volume API call from the triggering machine, so the trigger must
        not perform a single one; the bootstrap performs them all, next
        to the mounted volume.
        """
        from stardag._core.base_task import TargetTask
        from stardag.utils.testing.helper_tasks import SyncOnlyTask

        checked: list = []
        original = TargetTask.complete_aio

        async def spy(self):
            checked.append(self.id)
            return await original(self)

        monkeypatch.setattr(TargetTask, "complete_aio", spy)

        app = self._make_app()
        dep = SyncOnlyTask(name="no-local-io-dep")
        root = SyncOnlyTask(name="no-local-io-root", deps=(dep,))

        _, _, bootstrap_kwargs = _trigger_reactive(
            app, root, stub=modal_function_stub, run_bootstrap=False
        )
        assert checked == []

        # …and the very same walk does happen once the bootstrap runs.
        bootstrap = _finalize_capturing_functions(self._make_app())["bootstrap"]
        registry = MagicMock(spec=RegistryABC)
        registry.task_register_bulk_aio.return_value = None
        with registry_provider.override(registry):
            bootstrap(**bootstrap_kwargs)
        assert set(checked) == {root.id, dep.id}

    @pytest.mark.parametrize("retrigger", [False, True])
    def test_roots_are_registered_before_the_bootstrap_is_spawned(
        self, monkeypatch, default_in_memory_fs_target, retrigger
    ):
        """The roots must be in the registry before anything is spawned.

        Fresh builds carry them on ``build_start``; a re-trigger appends
        them with ``build_add_roots``. Either way the bootstrap must not
        be airborne first — a concurrent tick would otherwise be free to
        complete-and-terminal the build on a root set the new subtree is
        not part of.
        """
        from stardag.utils.testing.helper_tasks import SyncOnlyTask

        app = self._make_app()
        build_id = uuid4()
        order: list[str] = []
        registry = MagicMock(spec=RegistryABC)
        registry.build_start.side_effect = lambda **kw: (
            order.append("build_start"),
            build_id,
        )[1]
        registry.build_add_roots.side_effect = lambda *a, **kw: order.append(
            "build_add_roots"
        )

        class _Stub:
            def spawn(self, **kwargs):
                order.append("spawn")
                return "spawn-handle"

        monkeypatch.setattr(
            modal.Function, "from_name", staticmethod(lambda **kw: _Stub())
        )

        root = SyncOnlyTask(name="order-root")
        with registry_provider.override(registry):
            app.build_trigger(
                root, reactive=True, build_id=build_id if retrigger else None
            )

        assert order == (
            ["build_add_roots", "spawn"] if retrigger else ["build_start", "spawn"]
        )

    def test_bootstrap_discovers_persists_sets_marker_and_spawns_tick(
        self, modal_function_stub, default_in_memory_fs_target
    ):
        from uuid import uuid4 as _uuid4

        from stardag.build import BuildTaskStore
        from stardag.utils.testing.helper_tasks import SyncOnlyTask

        app = self._make_app()
        build_id = _uuid4()
        registry = MagicMock(spec=RegistryABC)
        registry.build_start.return_value = build_id
        registry.task_register_bulk_aio.return_value = None
        dep = SyncOnlyTask(name="reactive-dep")
        root = SyncOnlyTask(name="reactive-root", deps=(dep,))

        _trigger_reactive(
            app,
            root,
            stub=modal_function_stub,
            registry=registry,
            tick_kwargs={"linger_seconds": 30},
        )

        # Discovery registered the DAG…
        registry.task_register_bulk_aio.assert_called()
        # …the reactive marker/owner/config were written to the REGISTRY
        # (not the target root, which may be immutable).
        registry.build_set_reactive_meta.assert_called_once_with(
            build_id, app_name=app.name, tick_kwargs={"linger_seconds": 30}
        )
        # …the task store holds the rehydratable pickles (objects only)…
        store = BuildTaskStore(build_id)
        loaded_root = store.load_task(root.id)
        assert loaded_root is not None and loaded_root.id == root.id
        assert store.load_task(dep.id) is not None
        # …and the first tick was spawned with only the build id (config
        # comes from the registry reactive_tick_kwargs so ALL ticks —
        # worker wake-ups, watchdog — share it).
        assert modal_function_stub["from_name"] == {
            "app_name": app.name,
            "name": "tick",
        }
        assert modal_function_stub["op"] == "spawn"
        assert modal_function_stub["kwargs"] == {"build_id": str(build_id)}

    def test_marker_is_written_only_after_discovery_and_persistence(
        self, modal_function_stub, default_in_memory_fs_target
    ):
        """The ordering guarantee: ``reactive_app_name`` is the "this
        build is reactively scheduled" marker, and a tick no-ops without
        it. Written before the DAG is fully registered, it would expose a
        window in which a tick sees "nothing actionable, roots not
        complete" — exactly the shape terminal detection fails a build on
        (registration is chunked post-order, so the roots land last).
        """
        from stardag.build import BuildTaskStore
        from stardag.utils.testing.helper_tasks import SyncOnlyTask

        app = self._make_app()
        build_id = uuid4()
        registry = MagicMock(spec=RegistryABC)
        registry.build_start.return_value = build_id
        registry.task_register_bulk_aio.return_value = None
        dep = SyncOnlyTask(name="marker-order-dep")
        root = SyncOnlyTask(name="marker-order-root", deps=(dep,))

        observed: dict = {}

        def observe_marker(*args, **kwargs):
            store = BuildTaskStore(build_id)
            observed["registered"] = registry.task_register_bulk_aio.call_count
            observed["persisted"] = [
                store.load_task(t.id) is not None for t in (root, dep)
            ]

        registry.build_set_reactive_meta.side_effect = observe_marker

        _trigger_reactive(app, root, stub=modal_function_stub, registry=registry)

        assert observed["registered"] > 0
        assert observed["persisted"] == [True, True]

    def test_reactive_rejects_build_kwargs(self, modal_function_stub):
        app = self._make_app()
        with pytest.raises(TypeError, match="not supported with reactive"):
            app.build_trigger(
                MagicMock(spec=BaseTask),
                reactive=True,
                build_kwargs={"fail_mode": "CONTINUE"},
            )

    def test_reactive_rejects_worker_selector_override(self, modal_function_stub):
        """Later ticks always use the app's deployed selector — a
        per-trigger override would change routing mid-build."""
        app = self._make_app()
        with pytest.raises(TypeError, match="worker_selector overrides"):
            app.build_trigger(
                MagicMock(spec=BaseTask),
                worker_selector=lambda t: "gpu",
                reactive=True,
            )

    def test_reactive_rejects_non_persistable_tick_kwargs(self, modal_function_stub):
        """tick_kwargs are persisted as JSON meta shared by all ticks —
        callables (e.g. a limit key selector) must be configured on the
        deployed app instead."""
        app = self._make_app()
        with pytest.raises(TypeError, match="Unsupported tick_kwargs"):
            app.build_trigger(
                MagicMock(spec=BaseTask),
                reactive=True,
                tick_kwargs={"limit_key_selector": lambda t: []},
            )

    def test_reactive_requires_registry(self, modal_function_stub):
        app = self._make_app()
        with registry_provider.override(NoOpRegistry()):
            with pytest.raises(RuntimeError, match="requires a configured registry"):
                app.build_trigger(
                    MagicMock(spec=BaseTask),
                    build_id=uuid4(),  # explicit id is NOT enough in reactive
                    reactive=True,
                )


class TestReactiveTriggerFailureLeavesNoOrphanBuild:
    """A reactive trigger mints a RUNNING build and then walks away. Every
    way the work can die before the first tick must therefore record a
    terminal BUILD_FAILED — on both sides of the spawn."""

    def _make_app(self, **kwargs):
        return StardagApp(
            "test-orphan-app",
            builder_settings=FunctionSettings(image=_make_image()),
            worker_settings={"default": FunctionSettings(image=_make_image())},
            **kwargs,
        )

    def test_spawn_failure_at_the_trigger_fails_the_build(
        self, monkeypatch, default_in_memory_fs_target
    ):
        from stardag.utils.testing.helper_tasks import SyncOnlyTask

        app = self._make_app()
        build_id = uuid4()
        registry = MagicMock(spec=RegistryABC)
        registry.build_start.return_value = build_id
        monkeypatch.setattr(
            modal.Function,
            "from_name",
            staticmethod(MagicMock(side_effect=RuntimeError("no such app"))),
        )

        with registry_provider.override(registry):
            with pytest.raises(RuntimeError, match="no such app"):
                app.build_trigger(SyncOnlyTask(name="orphan-root"), reactive=True)

        registry.build_fail.assert_called_once()
        assert registry.build_fail.call_args.args[0] == build_id
        assert "no such app" in registry.build_fail.call_args.kwargs["error_message"]

    def test_a_failed_resume_does_not_fail_the_build(
        self, modal_function_stub, default_in_memory_fs_target
    ):
        """The arming point matters. On a re-trigger the build may still
        be *terminal* until ``build_resume`` lands, so a resume that
        failed must not have this trigger stamp BUILD_FAILED over
        somebody else's outcome."""
        from stardag.utils.testing.helper_tasks import SyncOnlyTask

        app = self._make_app()
        build_id = uuid4()
        registry = MagicMock(spec=RegistryABC)
        registry.build_resume.side_effect = RuntimeError("resume rejected")

        with registry_provider.override(registry):
            with pytest.raises(RuntimeError, match="resume rejected"):
                app.build_trigger(
                    SyncOnlyTask(name="resume-fail-root"),
                    build_id=build_id,
                    reactive=True,
                )

        registry.build_fail.assert_not_called()

    def test_bootstrap_failure_fails_the_build_and_propagates(
        self, modal_function_stub, default_in_memory_fs_target
    ):
        """Failures on the far side of the spawn are the bootstrap's to
        report: nobody else is watching that container."""
        from stardag.utils.testing.helper_tasks import SyncOnlyTask

        app = self._make_app()
        build_id = uuid4()
        registry = MagicMock(spec=RegistryABC)
        registry.build_start.return_value = build_id
        registry.task_register_bulk_aio.side_effect = RuntimeError("registry down")

        with pytest.raises(RuntimeError, match="registry down"):
            _trigger_reactive(
                app,
                SyncOnlyTask(name="bootstrap-fail-root"),
                stub=modal_function_stub,
                registry=registry,
            )

        registry.build_fail.assert_called_once()
        assert registry.build_fail.call_args.args[0] == build_id
        assert "registry down" in registry.build_fail.call_args.kwargs["error_message"]
        # The build was never armed: no marker, so no tick can act on the
        # half-registered DAG this failure left behind.
        registry.build_set_reactive_meta.assert_not_called()

    def test_a_failed_first_tick_spawn_fails_the_build(
        self, monkeypatch, default_in_memory_fs_target
    ):
        """An un-spawned first tick is not a partial success — without a
        watchdog nothing would ever move the build."""
        from stardag.utils.testing.helper_tasks import SyncOnlyTask

        app = self._make_app()
        build_id = uuid4()
        registry = MagicMock(spec=RegistryABC)
        registry.task_register_bulk_aio.return_value = None
        bootstrap = _finalize_capturing_functions(app)["bootstrap"]
        monkeypatch.setattr(
            modal.Function,
            "from_name",
            staticmethod(MagicMock(side_effect=RuntimeError("tick gone"))),
        )

        with registry_provider.override(registry):
            with pytest.raises(RuntimeError, match="tick gone"):
                bootstrap(
                    build_id=str(build_id),
                    tasks=[SyncOnlyTask(name="tick-spawn-fail-root")],
                )

        registry.build_fail.assert_called_once()

    def test_a_registry_that_cannot_record_the_failure_never_masks_it(
        self, monkeypatch, default_in_memory_fs_target, caplog
    ):
        from stardag.utils.testing.helper_tasks import SyncOnlyTask

        app = self._make_app()
        registry = MagicMock(spec=RegistryABC)
        registry.build_start.return_value = uuid4()
        registry.build_fail.side_effect = RuntimeError("also down")
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

    def _make_app(self, **kwargs):
        return StardagApp(
            "test-placement-app",
            builder_settings=FunctionSettings(image=_make_image()),
            worker_settings={"default": FunctionSettings(image=_make_image())},
            **kwargs,
        )

    def test_default_is_modal(self):
        assert self._make_app().reactive_discovery == "modal"

    def test_unknown_placement_is_rejected_eagerly(self):
        with pytest.raises(ValueError, match="reactive_discovery"):
            self._make_app(reactive_discovery="remote")  # type: ignore[arg-type]

    def test_local_runs_the_same_bootstrap_in_process(
        self, modal_function_stub, default_in_memory_fs_target
    ):
        from stardag.build import BuildTaskStore
        from stardag.utils.testing.helper_tasks import SyncOnlyTask

        app = self._make_app(reactive_discovery="local")
        build_id = uuid4()
        registry = MagicMock(spec=RegistryABC)
        registry.build_start.return_value = build_id
        registry.task_register_bulk_aio.return_value = None
        root = SyncOnlyTask(name="local-discovery-root")

        with registry_provider.override(registry):
            result = app.build_trigger(root, reactive=True)

        # Discovery, persistence and the marker all happened right here…
        registry.task_register_bulk_aio.assert_called()
        registry.build_set_reactive_meta.assert_called_once_with(
            build_id, app_name=app.name, tick_kwargs=None
        )
        assert BuildTaskStore(build_id).load_task(root.id) is not None
        # …and the handle is the first tick, since no bootstrap was spawned.
        assert modal_function_stub["from_name"] == {
            "app_name": app.name,
            "name": "tick",
        }
        assert result.function_call == "spawn-handle"

    def test_local_failure_also_leaves_no_orphan_build(
        self, modal_function_stub, default_in_memory_fs_target
    ):
        from stardag.utils.testing.helper_tasks import SyncOnlyTask

        app = self._make_app(reactive_discovery="local")
        build_id = uuid4()
        registry = MagicMock(spec=RegistryABC)
        registry.build_start.return_value = build_id
        registry.task_register_bulk_aio.side_effect = RuntimeError("registry down")

        with registry_provider.override(registry):
            with pytest.raises(RuntimeError, match="registry down"):
                app.build_trigger(SyncOnlyTask(name="local-fail-root"), reactive=True)

        registry.build_fail.assert_called_once()


class TestReactiveTriggerRootCoverageAdvisory:
    """Additive early feedback at the trigger: roots only, advisory, and
    never the check itself (that one runs over the whole discovered DAG,
    wherever discovery runs)."""

    def _app(self, **kwargs):
        return StardagApp(
            "test-advisory-app",
            builder_settings=FunctionSettings(image=_make_image()),
            worker_settings={"default": FunctionSettings(image=_make_image())},
            **kwargs,
        )

    def test_uncovered_root_is_reported_before_anything_is_spawned(
        self, caplog, modal_function_stub, default_in_memory_fs_target
    ):
        from stardag.utils.testing.helper_tasks import SyncOnlyTask

        app = self._app(task_modules=[_UNCOVERING_PATTERN])
        registry = MagicMock(spec=RegistryABC)
        registry.build_start.return_value = uuid4()

        with caplog.at_level("WARNING"):
            with registry_provider.override(registry):
                app.build_trigger(SyncOnlyTask(name="advisory-root"), reactive=True)

        assert "not covered by this app's task_modules" in caplog.text
        assert "ROOT-TASKS-ONLY" in caplog.text

    def test_covered_root_says_nothing(
        self, caplog, modal_function_stub, default_in_memory_fs_target
    ):
        from stardag.utils.testing.helper_tasks import SyncOnlyTask

        app = self._app(task_modules=[SyncOnlyTask.__module__])
        registry = MagicMock(spec=RegistryABC)
        registry.build_start.return_value = uuid4()

        with caplog.at_level("WARNING"):
            with registry_provider.override(registry):
                app.build_trigger(SyncOnlyTask(name="advisory-ok-root"), reactive=True)

        assert "not covered" not in caplog.text

    def test_it_never_walks_the_dag(
        self, monkeypatch, modal_function_stub, default_in_memory_fs_target
    ):
        """Roots only, by construction: an advisory that traversed
        ``requires()`` would reintroduce the local walk this whole change
        removes, and could disagree with the real check."""
        from stardag.utils.testing.helper_tasks import SyncOnlyTask

        app = self._app(task_modules=[_UNCOVERING_PATTERN])
        registry = MagicMock(spec=RegistryABC)
        registry.build_start.return_value = uuid4()
        dep = SyncOnlyTask(name="advisory-dep")
        root = SyncOnlyTask(name="advisory-walk-root", deps=(dep,))

        requires_calls: list = []
        original = SyncOnlyTask.requires

        def spy(self):
            requires_calls.append(self.id)
            return original(self)

        monkeypatch.setattr(SyncOnlyTask, "requires", spy)

        with registry_provider.override(registry):
            app.build_trigger(root, reactive=True)

        assert requires_calls == []


# ---------------------------------------------------------------------------
# task_modules: declaration, deploy-time expansion, pre-flight, pickle elision
# ---------------------------------------------------------------------------


_UNCOVERING_PATTERN = "stardag.registry.*"
"""A pattern that covers none of the task classes used in these tests.

Real and importable on purpose: ``finalize()`` expands the declared
patterns (that is how the deployed module list is frozen), so a fictional
package cannot be used by any test that goes through a deploy — which,
now that the coverage check runs from the *deployed* list, is all of them.
"""


def _app_with_task_modules(name: str, **kwargs) -> StardagApp:
    return StardagApp(
        name,
        builder_settings=FunctionSettings(image=_make_image()),
        worker_settings={"default": FunctionSettings(image=_make_image())},
        **kwargs,
    )


class TestTaskModulesDeclaration:
    """`StardagApp(task_modules=...)` validation and inference."""

    def test_patterns_are_validated_eagerly(self):
        from stardag.build import TaskModulesError

        with pytest.raises(TaskModulesError, match="only allowed as the final"):
            _app_with_task_modules("tm-bad", task_modules=["my_pkg.*.tasks"])

    def test_empty_list_opts_out_silently(self, caplog):
        with caplog.at_level("WARNING"):
            app = _app_with_task_modules("tm-optout", task_modules=[])
        assert app.task_modules == ()
        assert "task_modules" not in caplog.text

    def test_declared_patterns_are_deduped_and_sorted(self):
        app = _app_with_task_modules(
            "tm-declared", task_modules=["b_pkg.*", "a_pkg.tasks", "b_pkg.*"]
        )
        assert app.task_modules == ("a_pkg.tasks", "b_pkg.*")

    def test_default_infers_from_the_defining_module(self):
        """The default is "the root package of the module defining the app,
        recursively" — resolved from the caller's frame, so it matches what
        inference sees from this very test function."""
        from stardag.integration.modal._app import _infer_task_module_patterns

        expected = _infer_task_module_patterns(_depth=1)
        app = _app_with_task_modules("tm-inferred")
        assert app.task_modules == expected

    def test_inference_uses_the_callers_root_package(self):
        from stardag.integration.modal._app import _infer_task_module_patterns

        namespace = {
            "__name__": "acme_pipelines.deploy.app",
            "__package__": "acme_pipelines.deploy",
            "_infer": _infer_task_module_patterns,
        }
        exec("result = _infer(_depth=1)", namespace)  # noqa: S102
        assert namespace["result"] == ("acme_pipelines.*",)

    @pytest.mark.parametrize(
        "module_name,package",
        [("__main__", None), ("loose_deploy_script", ""), ("__main__", "")],
    )
    def test_inference_opts_out_with_a_warning_for_unpackaged_modules(
        self, caplog, module_name, package
    ):
        """A module that isn't part of a package has no importable name in a
        container, so a pattern derived from it would be a lie: warn (naming
        the fallback and the fix) and opt out."""
        from stardag.integration.modal._app import _infer_task_module_patterns

        namespace = {
            "__name__": module_name,
            "__package__": package,
            "_infer": _infer_task_module_patterns,
        }
        with caplog.at_level("WARNING"):
            exec("result = _infer(_depth=1)", namespace)  # noqa: S102

        assert namespace["result"] == ()
        assert "Could not infer StardagApp(task_modules=...)" in caplog.text
        assert "fall back to the build task store's pickles" in caplog.text
        assert 'task_modules=["my_pkg.tasks.*"]' in caplog.text

    def test_require_pickle_free_without_task_modules_is_rejected(self):
        from stardag.build import TaskModulesError

        with pytest.raises(TaskModulesError, match="meaningless without"):
            _app_with_task_modules(
                "tm-contradiction", task_modules=[], require_pickle_free=True
            )


class TestFinalizeBakesTaskModules:
    """finalize() expands the patterns once and freezes the result into the
    deployed tick — so the deployed set is explicit, auditable, and only
    changes on redeploy."""

    def _finalize(self, **app_kwargs):
        app = _app_with_task_modules("tm-finalize", **app_kwargs)
        captured: dict = {}

        def capture_function(**kwargs):
            def decorator(fn):
                captured[kwargs.get("name", "unknown")] = fn
                return fn

            return decorator

        app.modal_app.function = capture_function  # type: ignore[assignment]
        with patch("stardag.integration.modal._app.get_target_roots_volumes") as mv:
            mv.return_value = MagicMock(by_volume_name={}, by_root_key={})
            result = app.finalize()
        return app, result, captured

    def test_expansion_is_surfaced_on_the_finalize_result(self):
        _, result, _ = self._finalize(task_modules=["stardag.utils.*"])

        assert "stardag.utils" in result.task_modules
        assert "stardag.utils.testing.helper_tasks" in result.task_modules
        assert result.task_modules == sorted(set(result.task_modules))

    def test_opted_out_app_bakes_nothing(self):
        _, result, _ = self._finalize(task_modules=[])
        assert result.task_modules == []

    def test_tick_imports_the_baked_list(self, default_in_memory_fs_target):
        from stardag.build import TickSummary
        from stardag.registry import BuildInfo

        _, result, captured = self._finalize(task_modules=["stardag.utils.*"])
        build_id = uuid4()
        registry = MagicMock(spec=RegistryABC)
        registry.build_get.return_value = BuildInfo(
            id=build_id, reactive_app_name="tm-finalize", reactive_tick_kwargs=None
        )

        async def stub_tick_aio(build_uuid, **kwargs):
            return TickSummary(outcome="noop")

        with (
            patch("stardag.integration.modal._tick.run_tick_aio", stub_tick_aio),
            patch("stardag.integration.modal._tick.registry_provider") as rp,
            patch(
                "stardag.integration.modal._tick.RegistryGlobalConcurrencyLockManager"
            ),
            patch(
                "stardag.integration.modal._tick.import_task_modules"
            ) as import_modules,
        ):
            rp.get.return_value = registry
            captured["tick"](str(build_id))

        # Exactly the list frozen at deploy time — not re-derived in the
        # container, where the filesystem walk would cost cold-start time.
        # (The deployment record holds it as a tuple; import_task_modules
        # keys its cache on ``tuple(modules)`` either way.)
        import_modules.assert_called_once()
        assert list(import_modules.call_args.args[0]) == result.task_modules

    def test_opted_out_tick_imports_nothing(self, default_in_memory_fs_target):
        from stardag.build import TickSummary
        from stardag.registry import BuildInfo

        _, _, captured = self._finalize(task_modules=[])
        build_id = uuid4()
        registry = MagicMock(spec=RegistryABC)
        registry.build_get.return_value = BuildInfo(
            id=build_id, reactive_app_name="tm-finalize", reactive_tick_kwargs=None
        )

        async def stub_tick_aio(build_uuid, **kwargs):
            return TickSummary(outcome="noop")

        with (
            patch("stardag.integration.modal._tick.run_tick_aio", stub_tick_aio),
            patch("stardag.integration.modal._tick.registry_provider") as rp,
            patch(
                "stardag.integration.modal._tick.RegistryGlobalConcurrencyLockManager"
            ),
            patch(
                "stardag.integration.modal._tick.import_task_modules"
            ) as import_modules,
        ):
            rp.get.return_value = registry
            captured["tick"](str(build_id))

        import_modules.assert_not_called()

    def test_worker_publishes_the_patterns_for_dynamic_dep_registration(self):
        """The worker doesn't import the modules (its task arrived by value,
        self-importing) but it does need the patterns: dynamic deps are
        persisted with the same elision as the trigger's discovered set."""
        from stardag.build import declared_task_module_patterns
        from stardag.utils.testing.helper_tasks import SyncOnlyTask

        app, _, captured = self._finalize(task_modules=["stardag.utils.*"])
        from stardag.build import set_declared_task_module_patterns

        set_declared_task_module_patterns([])
        try:
            with patch.object(Runner, "run", return_value=None):
                captured["worker_default"](SyncOnlyTask(name="publishes"))
            assert declared_task_module_patterns() == app.task_modules
        finally:
            set_declared_task_module_patterns([])


class TestReactiveTriggerCoveragePreflight:
    """The authoritative coverage check. It runs where discovery runs
    (the bootstrap container by default) over the real discovered set, and
    tells the user a scheduler tick won't be able to rebuild one of their
    task classes — against the module list the deployment actually baked
    in, not the caller's local app definition."""

    def _trigger(self, app, root, stub):
        return _trigger_reactive(app, root, stub=stub)[:2]

    def test_uncovered_class_warns_with_the_pattern_to_add(
        self, caplog, modal_function_stub, default_in_memory_fs_target
    ):
        from stardag.utils.testing.helper_tasks import SyncOnlyTask

        app = _app_with_task_modules("tm-preflight", task_modules=[_UNCOVERING_PATTERN])
        root = SyncOnlyTask(name="preflight-uncovered")

        with caplog.at_level("WARNING"):
            self._trigger(app, root, modal_function_stub)

        assert "not covered by this app's task_modules" in caplog.text
        assert f"['{_UNCOVERING_PATTERN}']" in caplog.text
        # The exact pattern that would fix it, and the redeploy requirement.
        assert f"{SyncOnlyTask.__module__.rsplit('.', 1)[0]}.*" in caplog.text
        assert "redeploy" in caplog.text

    def test_covered_class_does_not_warn(
        self, caplog, modal_function_stub, default_in_memory_fs_target
    ):
        from stardag.utils.testing.helper_tasks import SyncOnlyTask

        app = _app_with_task_modules(
            "tm-preflight-ok", task_modules=[SyncOnlyTask.__module__]
        )
        with caplog.at_level("WARNING"):
            self._trigger(
                app, SyncOnlyTask(name="preflight-covered"), modal_function_stub
            )

        assert "not covered" not in caplog.text

    def test_opted_out_app_never_warns(
        self, caplog, modal_function_stub, default_in_memory_fs_target
    ):
        """An app that declared nothing would otherwise warn about every
        class in every DAG, on every trigger."""
        from stardag.utils.testing.helper_tasks import SyncOnlyTask

        app = _app_with_task_modules("tm-preflight-optout", task_modules=[])
        with caplog.at_level("WARNING"):
            self._trigger(
                app, SyncOnlyTask(name="preflight-optout"), modal_function_stub
            )

        assert "not covered" not in caplog.text

    def test_only_incomplete_tasks_are_checked(
        self, caplog, modal_function_stub, default_in_memory_fs_target
    ):
        """Discovery stops at complete tasks and only incomplete ones are
        ever rehydrated by a tick — so a completed dep's class is
        irrelevant, and checking it would be a false alarm."""
        from stardag.utils.testing.helper_tasks import SyncOnlyTask

        dep = SyncOnlyTask(name="preflight-complete-dep")
        dep.run()  # writes its target -> discovery treats it as complete
        root = SyncOnlyTask(name="preflight-root", deps=(dep,))
        app = _app_with_task_modules(
            "tm-preflight-incomplete", task_modules=[_UNCOVERING_PATTERN]
        )

        with caplog.at_level("WARNING"):
            _, registry = self._trigger(app, root, modal_function_stub)

        # One warning from the authoritative check (the trigger's
        # roots-only advisory is a separate, clearly-labelled line —
        # match on the authoritative remedy to tell them apart). The
        # class is reported once regardless; the point is that the
        # completed dep never entered the checked set.
        assert caplog.text.count("build-task-store pickles") == 1


class _ElisionAliasedSource(_sd.Task[int]):
    """Module-level (pickle-able) source for the AliasTask elision test."""

    def run(self) -> None:
        self._save(7)


class _ElisionIntAlias(_sd.AliasTask[int]):
    """Concrete alias class (module-level so the store can pickle it)."""


class _ElisionAliasConsumer(_sd.Task[int]):
    """Consumes an aliased upstream — its payload therefore embeds the
    ``__aliased`` marker that rehydration refuses."""

    loads_int: _sd.TaskLoads[int]

    def run(self) -> None:
        self._save(self.loads_int.load() + 1)


class TestReactivePickleElision:
    """With task_modules covering the classes, a reactive build writes no
    pickles: a scheduler tick rebuilds the tasks from registry data.

    The store is written by the bootstrap — in-container by default,
    where a ``modalvol://`` target root is a mounted filesystem — so
    these drive the trigger *and* the bootstrap it spawned. The module
    list the elision is decided from is the one baked in at deploy time.
    """

    def _trigger(self, app, root, stub, build_id=None):
        result, registry, _ = _trigger_reactive(app, root, stub=stub, build_id=build_id)
        return result, registry

    def test_covered_round_tripping_tasks_get_no_pickle(
        self, caplog, modal_function_stub, default_in_memory_fs_target
    ):
        from stardag.build import BuildTaskStore
        from stardag.utils.testing.helper_tasks import SyncOnlyTask

        app = _app_with_task_modules("tm-elide", task_modules=[SyncOnlyTask.__module__])
        dep = SyncOnlyTask(name="elide-dep")
        root = SyncOnlyTask(name="elide-root", deps=(dep,))

        with caplog.at_level("INFO"):
            result, _ = self._trigger(app, root, modal_function_stub)

        store = BuildTaskStore(result.build_id)
        assert store.load_task(root.id) is None
        assert store.load_task(dep.id) is None
        assert "2 task(s) pickle-free, 0 pickled" in caplog.text

    def test_uncovered_tasks_still_get_their_pickle(
        self, modal_function_stub, default_in_memory_fs_target
    ):
        from stardag.build import BuildTaskStore
        from stardag.utils.testing.helper_tasks import SyncOnlyTask

        app = _app_with_task_modules(
            "tm-elide-uncovered", task_modules=[_UNCOVERING_PATTERN]
        )
        root = SyncOnlyTask(name="elide-uncovered-root")

        result, _ = self._trigger(app, root, modal_function_stub)

        store = BuildTaskStore(result.build_id)
        loaded = store.load_task(root.id)
        assert loaded is not None and loaded.id == root.id

    def test_inferred_task_modules_do_not_elide(
        self, monkeypatch, modal_function_stub, default_in_memory_fs_target
    ):
        """Inference is observation-only; only an explicit declaration elides.

        The trigger reads the LOCAL app definition while the tick runs the
        DEPLOYED one, and nothing lets the trigger see the deployed app's
        baked module list. If inference alone enabled elision, merely
        upgrading the SDK would start dropping pickles that an app deployed
        by an older SDK has no module list to compensate for — an upgrade
        that breaks builds.
        """
        from stardag.build import BuildTaskStore
        from stardag.integration.modal import _app as app_module
        from stardag.utils.testing.helper_tasks import SyncOnlyTask

        monkeypatch.setattr(
            app_module, "_infer_task_module_patterns", lambda *a, **k: ("stardag.*",)
        )
        app = _app_with_task_modules("tm-elide-inferred")
        # The inferred patterns DO cover the task class — coverage is not
        # what is being withheld here, the opt-in is.
        assert app.task_modules == ("stardag.*",)
        root = SyncOnlyTask(name="elide-inferred-root")

        result, _ = self._trigger(app, root, modal_function_stub)

        assert BuildTaskStore(result.build_id).load_task(root.id) is not None

    def test_the_same_patterns_declared_explicitly_do_elide(
        self, modal_function_stub, default_in_memory_fs_target
    ):
        """The opt-in is the user's act, not the patterns' content."""
        from stardag.build import BuildTaskStore
        from stardag.utils.testing.helper_tasks import SyncOnlyTask

        app = _app_with_task_modules("tm-elide-explicit", task_modules=["stardag.*"])
        root = SyncOnlyTask(name="elide-explicit-root")

        result, _ = self._trigger(app, root, modal_function_stub)

        assert BuildTaskStore(result.build_id).load_task(root.id) is None

    def test_opted_out_app_pickles_everything_exactly_as_before(
        self, modal_function_stub, default_in_memory_fs_target
    ):
        from stardag.build import BuildTaskStore
        from stardag.utils.testing.helper_tasks import SyncOnlyTask

        app = _app_with_task_modules("tm-elide-optout", task_modules=[])
        root = SyncOnlyTask(name="elide-optout-root")

        result, _ = self._trigger(app, root, modal_function_stub)

        assert BuildTaskStore(result.build_id).load_task(root.id) is not None

    def test_alias_task_dag_keeps_its_pickle(
        self, modal_function_stub, default_in_memory_fs_target
    ):
        """AliasTask is pickle-bound by design: its ``loads_type`` is pickled
        bytes that a scheduler tick must never auto-unpickle from registry
        data. The self-check fails, so the pickle is written."""
        from stardag.build import BuildTaskStore

        source = _ElisionAliasedSource()
        source.run()
        alias = _ElisionIntAlias(aliased=_sd.AliasedMetadata.from_task(source))
        root = _ElisionAliasConsumer(loads_int=alias)
        app = _app_with_task_modules("tm-elide-alias", task_modules=[__name__])

        result, _ = self._trigger(app, root, modal_function_stub)

        store = BuildTaskStore(result.build_id)
        # The consumer embeds the alias payload, so it too fails the
        # round-trip and keeps its pickle.
        assert store.load_task(root.id) is not None

    def test_require_pickle_free_raises_naming_every_task(
        self, modal_function_stub, default_in_memory_fs_target
    ):
        """Still enforced, and still loud — now from the bootstrap, where
        the store is written. It propagates on the bootstrap's Modal call
        AND records a terminal BUILD_FAILED, so the build never sits
        RUNNING behind a storage preference the operator asked for."""
        from stardag.build import TaskModulesError
        from stardag.utils.testing.helper_tasks import SyncOnlyTask

        app = _app_with_task_modules(
            "tm-require-pickle-free",
            task_modules=[_UNCOVERING_PATTERN],
            require_pickle_free=True,
        )
        dep = SyncOnlyTask(name="require-dep")
        root = SyncOnlyTask(name="require-root", deps=(dep,))
        build_id = uuid4()
        registry = MagicMock(spec=RegistryABC)
        registry.build_start.return_value = build_id
        registry.task_register_bulk_aio.return_value = None

        with pytest.raises(TaskModulesError) as exc:
            _trigger_reactive(app, root, stub=modal_function_stub, registry=registry)

        message = str(exc.value)
        assert "require_pickle_free=True" in message
        assert "2 task(s)" in message
        assert str(root.id) in message and str(dep.id) in message
        assert "not covered by task_modules" in message
        registry.build_fail.assert_called_once()
        assert registry.build_fail.call_args.args[0] == build_id
        # Never armed: no marker, so no tick acts on the half-built state.
        registry.build_set_reactive_meta.assert_not_called()

    def test_require_pickle_free_passes_when_everything_is_covered(
        self, modal_function_stub, default_in_memory_fs_target
    ):
        from stardag.build import BuildTaskStore
        from stardag.utils.testing.helper_tasks import SyncOnlyTask

        app = _app_with_task_modules(
            "tm-require-ok",
            task_modules=[SyncOnlyTask.__module__],
            require_pickle_free=True,
        )
        root = SyncOnlyTask(name="require-ok-root")

        result, _ = self._trigger(app, root, modal_function_stub)

        assert BuildTaskStore(result.build_id).load_task(root.id) is None


class TestDynamicDepPickleElision:
    """Dynamic deps registered from inside a worker get the same treatment —
    without it, ``require_pickle_free`` would hold only until a task yielded
    its first dynamic dependency."""

    def _reporter(self, build_id):
        from stardag.integration.modal._runner import _WorkerLifecycleReporter
        from stardag.utils.testing.helper_tasks import SyncOnlyTask

        registry = MagicMock(spec=RegistryABC)
        registry.task_register_bulk_aio.return_value = None
        return (
            _WorkerLifecycleReporter(
                registry,
                build_id,
                SyncOnlyTask(name="dyn-parent"),
                reactive=True,
                app_name="tm-dynamic",
            ),
            registry,
        )

    def test_covered_dynamic_deps_are_not_pickled(self, default_in_memory_fs_target):
        from stardag.build import (
            BuildTaskStore,
            set_declared_task_module_patterns,
        )
        from stardag.utils.testing.helper_tasks import SyncOnlyTask

        build_id = uuid4()
        reporter, _ = self._reporter(build_id)
        dyn = SyncOnlyTask(name="dyn-dep-covered")

        set_declared_task_module_patterns([SyncOnlyTask.__module__])
        try:
            reporter._register_dynamic_deps((dyn,))
        finally:
            set_declared_task_module_patterns([])

        assert BuildTaskStore(build_id).load_task(dyn.id) is None

    def test_uncovered_dynamic_deps_are_pickled_and_warned_once(
        self, caplog, default_in_memory_fs_target
    ):
        from stardag.build import (
            BuildTaskStore,
            set_declared_task_module_patterns,
        )
        from stardag.build._task_modules import _warned_classes
        from stardag.utils.testing.helper_tasks import AsyncOnlyTask

        build_id = uuid4()
        reporter, _ = self._reporter(build_id)
        first = AsyncOnlyTask(name="dyn-dep-uncovered-1")
        second = AsyncOnlyTask(name="dyn-dep-uncovered-2")

        _warned_classes.discard(
            f"{AsyncOnlyTask.__module__}.{AsyncOnlyTask.__qualname__}"
        )
        set_declared_task_module_patterns(["acme_pipelines.*"])
        try:
            with caplog.at_level("WARNING"):
                reporter._register_dynamic_deps((first,))
                reporter._register_dynamic_deps((second,))
        finally:
            set_declared_task_module_patterns([])

        store = BuildTaskStore(build_id)
        assert store.load_task(first.id) is not None
        assert store.load_task(second.id) is not None
        # Once per class per process — this runs on every suspending worker.
        assert caplog.text.count("not covered by this app's task_modules") == 1
        assert "the trigger's pre-flight could not see them" in caplog.text

    def test_without_declared_patterns_behaviour_is_unchanged(
        self, default_in_memory_fs_target
    ):
        from stardag.build import (
            BuildTaskStore,
            set_declared_task_module_patterns,
        )
        from stardag.utils.testing.helper_tasks import SyncOnlyTask

        build_id = uuid4()
        reporter, _ = self._reporter(build_id)
        dyn = SyncOnlyTask(name="dyn-dep-no-patterns")

        set_declared_task_module_patterns([])
        reporter._register_dynamic_deps((dyn,))

        assert BuildTaskStore(build_id).load_task(dyn.id) is not None


@pytest.fixture(autouse=True)
def _mock_secret_hydrate(monkeypatch):
    """StardagApp.finalize() validates a by-name api-key secret via
    Secret.hydrate(); stub it so tests neither hit the network nor depend
    on a secret actually existing. Tests exercising the missing-secret
    error override this explicitly."""
    monkeypatch.setattr(modal.Secret, "hydrate", lambda self, *a, **k: self)


class TestApiKeySecretPropagation:
    """`stardag_api_key_secret` is injected into EVERY function (all talk to
    the registry). It is the only secret shared across functions —
    per-function `secrets` stay function-local."""

    def _finalize_capturing(
        self, *, builder_secrets=None, worker_secrets=None, **app_kwargs
    ):
        builder = FunctionSettings(image=_make_image(), secrets=builder_secrets or [])
        worker = FunctionSettings(image=_make_image(), secrets=worker_secrets or [])
        app = StardagApp(
            "test-secret-propagation",
            builder_settings=builder,
            worker_settings={"default": worker},
            **app_kwargs,
        )
        registered: dict = {}

        def capture_function(**kwargs):
            def decorator(fn):
                registered[kwargs.get("name", "unknown")] = kwargs
                return fn

            return decorator

        app.modal_app.function = capture_function  # type: ignore[assignment]
        with patch(
            "stardag.integration.modal._app.get_target_roots_volumes"
        ) as mock_volumes:
            mock_volumes.return_value = MagicMock(by_volume_name={}, by_root_key={})
            app.finalize()
        return registered

    @staticmethod
    def _secret_names(kwargs) -> list[str | None]:
        return [getattr(s, "name", None) for s in (kwargs.get("secrets") or [])]

    def test_default_api_key_secret_reaches_all_functions(self):
        registered = self._finalize_capturing(watchdog_period_minutes=5)
        for fn in ("build", "worker_default", "tick", "tick_watchdog"):
            assert "stardag-api-key" in self._secret_names(registered[fn]), fn

    def test_explicit_secret_name_reaches_all_functions(self):
        registered = self._finalize_capturing(
            stardag_api_key_secret="my-registry-key", watchdog_period_minutes=5
        )
        for fn in ("build", "worker_default", "tick", "tick_watchdog"):
            assert "my-registry-key" in self._secret_names(registered[fn]), fn

    def test_none_injects_no_api_key_secret(self):
        registered = self._finalize_capturing(stardag_api_key_secret=None)
        for fn in ("build", "worker_default", "tick"):
            assert "stardag-api-key" not in self._secret_names(registered[fn]), fn

    def test_builder_secrets_do_not_propagate_to_workers(self):
        # A secret declared only on the builder stays builder-local — the
        # old "propagate all builder secrets" behavior is gone.
        registered = self._finalize_capturing(
            builder_secrets=[modal.Secret.from_name("build-only")]
        )
        assert "build-only" in self._secret_names(registered["build"])
        assert "build-only" not in self._secret_names(registered["worker_default"])

    def test_api_key_deduped_when_function_declares_it(self):
        registered = self._finalize_capturing(
            worker_secrets=[modal.Secret.from_name("stardag-api-key")]
        )
        assert (
            self._secret_names(registered["worker_default"]).count("stardag-api-key")
            == 1
        )

    def test_worker_keeps_its_own_extra_secret(self):
        registered = self._finalize_capturing(
            worker_secrets=[modal.Secret.from_name("gpu-creds")]
        )
        names = self._secret_names(registered["worker_default"])
        assert "stardag-api-key" in names  # injected
        assert "gpu-creds" in names  # own

    def test_missing_named_secret_raises_clear_error(self, monkeypatch):
        from stardag.exceptions import StardagError

        def _raise(self, *a, **k):
            raise modal.exception.NotFoundError("Secret 'x' not found")

        monkeypatch.setattr(modal.Secret, "hydrate", _raise)
        with pytest.raises(StardagError) as exc:
            self._finalize_capturing(stardag_api_key_secret="does-not-exist")
        msg = str(exc.value)
        # Guides toward the *requested* secret name, with the --secret-name
        # flag (the default name would omit the flag).
        assert "does-not-exist" in msg
        assert "--secret-name does-not-exist" in msg

    def test_workspace_baked_into_env_at_finalize(self, monkeypatch):
        # The Modal token exists only in the deploy process, not in
        # containers, so finalize resolves the workspace locally (mocked to
        # "test-workspace" by the hermetic fixture) and bakes it into the
        # function env so container-side executor metadata has it.
        from stardag.integration.modal._metadata import STARDAG_MODAL_WORKSPACE_ENV

        captured: list[dict] = []
        real_from_dict = modal.Secret.from_dict

        def _record(d, **kwargs):
            captured.append(d)
            return real_from_dict(d, **kwargs)

        monkeypatch.setattr(modal.Secret, "from_dict", staticmethod(_record))
        self._finalize_capturing()
        assert {STARDAG_MODAL_WORKSPACE_ENV: "test-workspace"} in captured


class TestFinalizeRegistersTick:
    def _capture_app(self, **app_kwargs):
        app = StardagApp(
            "test-tick-registration",
            builder_settings=FunctionSettings(image=_make_image()),
            worker_settings={"default": FunctionSettings(image=_make_image())},
            **app_kwargs,
        )
        registered: dict = {}

        def capture_function(**kwargs):
            def decorator(fn):
                registered[kwargs.get("name", "unknown")] = kwargs
                return fn

            return decorator

        app.modal_app.function = capture_function  # type: ignore[assignment]
        return app, registered

    @patch("stardag.integration.modal._app.get_target_roots_volumes")
    def test_watchdog_deployed_but_unscheduled_by_default(self, mock_volumes):
        """The sweep is always deployed — so a full sweep is one click away
        on an app that runs no cron — but without a period it carries no
        schedule and costs nothing while idle."""
        mock_volumes.return_value = MagicMock(by_volume_name={}, by_root_key={})
        app, registered = self._capture_app()

        result = app.finalize()

        assert "tick" in registered
        assert "tick_watchdog" in registered
        assert registered["tick_watchdog"].get("schedule") is None
        assert "tick" in result.functions
        assert "tick_watchdog" in result.functions

    @patch("stardag.integration.modal._app.get_target_roots_volumes")
    def test_watchdog_registered_with_period(self, mock_volumes):
        mock_volumes.return_value = MagicMock(by_volume_name={}, by_root_key={})
        app, registered = self._capture_app(watchdog_period_minutes=7)

        result = app.finalize()

        assert "tick_watchdog" in registered
        schedule = registered["tick_watchdog"]["schedule"]
        assert isinstance(schedule, modal.Period)
        assert "tick_watchdog" in result.functions

    @patch("stardag.integration.modal._app.get_target_roots_volumes")
    def test_bootstrap_registered(self, mock_volumes):
        mock_volumes.return_value = MagicMock(by_volume_name={}, by_root_key={})
        app, registered = self._capture_app()

        result = app.finalize()

        assert "bootstrap" in registered
        assert "bootstrap" in result.functions
        assert registered["bootstrap"]["serialized"] is True

    @patch("stardag.integration.modal._app.get_target_roots_volumes")
    def test_bootstrap_defaults_to_builder_settings_not_tick_settings(
        self, mock_volumes
    ):
        """Its timeout budget is independent of the tick's on purpose:
        one frontier pass and one whole-DAG discovery are different
        questions, and shortening the tick must not shorten discovery."""
        mock_volumes.return_value = MagicMock(by_volume_name={}, by_root_key={})
        app, registered = self._capture_app(
            tick_settings=FunctionSettings(image=_make_image(), timeout=60)
        )
        app._builder_settings = FunctionSettings(image=_make_image(), timeout=3600)

        app.finalize()

        assert registered["tick"]["timeout"] == 60
        assert registered["bootstrap"]["timeout"] == 3600

    @patch("stardag.integration.modal._app.get_target_roots_volumes")
    def test_bootstrap_settings_override(self, mock_volumes):
        mock_volumes.return_value = MagicMock(by_volume_name={}, by_root_key={})
        app, registered = self._capture_app(
            bootstrap_settings=FunctionSettings(image=_make_image(), timeout=900)
        )

        app.finalize()

        assert registered["bootstrap"]["timeout"] == 900


class TestDeployedFunctionsAreSerializable:
    """Every function finalize() registers is registered ``serialized=True``,
    i.e. Modal cloudpickles the closure at deploy time and reconstructs it in
    the container. Whatever those closures capture must therefore survive
    ``modal._serialization``: a capture that doesn't is a deploy-time failure
    with nothing in the unit tier to catch it.
    """

    def _finalize_capturing_functions(self, **app_kwargs):
        app = StardagApp(
            "test-serializable",
            builder_settings=FunctionSettings(image=_make_image(), timeout=1800),
            worker_settings={"default": FunctionSettings(image=_make_image())},
            **app_kwargs,
        )
        captured: dict = {}

        def capture_function(**kwargs):
            def decorator(fn):
                captured[kwargs["name"]] = fn
                return fn

            return decorator

        app.modal_app.function = capture_function  # type: ignore[assignment]
        with patch("stardag.integration.modal._app.get_target_roots_volumes") as mv:
            mv.return_value = MagicMock(by_volume_name={}, by_root_key={})
            app.finalize()
        return captured

    def test_every_registered_function_round_trips(self):
        # Modal's own serializer (its vendored cloudpickle), not the
        # standalone package — that is what a deploy actually runs.
        from modal._serialization import serialize

        captured = self._finalize_capturing_functions(
            watchdog_period_minutes=5,
            task_modules=["stardag.utils.*"],
            limit_key_selector=lambda task: ["some-limit"],
        )

        assert set(captured) == {
            "build",
            "worker_default",
            "tick",
            "bootstrap",
            "tick_watchdog",
        }
        for name, fn in captured.items():
            assert serialize(fn), f"{name} serialized to nothing"

    def test_round_tripped_tick_still_reads_its_deploy_time_config(self):
        """The tick's deploy-time captures (app name, selectors, worker
        timeouts, baked module list) have to arrive intact on the other side
        of serialization — the container never sees the StardagApp."""
        from uuid import uuid4

        from modal._serialization import deserialize, serialize
        from stardag.registry import BuildInfo

        captured = self._finalize_capturing_functions(watchdog_period_minutes=5)
        tick = deserialize(serialize(captured["tick"]), None)

        build_id = uuid4()
        registry = MagicMock(spec=RegistryABC)
        registry.build_get.return_value = BuildInfo(
            id=build_id, reactive_app_name="another-app", reactive_tick_kwargs=None
        )
        with (
            patch("stardag.integration.modal._tick.registry_provider") as rp,
            patch("modal.Function.from_name", side_effect=Exception("no such app")),
        ):
            rp.get.return_value = registry
            result = tick(str(build_id))

        # It compared the build's owner against the app name it was
        # deployed with, which means that capture survived the round trip.
        assert result == {
            "outcome": "foreign_app",
            "owner_app": "another-app",
            "forwarded": False,
        }


class TestTickAppOwnership:
    """Only the app recorded as the build's reactive_app_name (in the
    registry) may drive its ticks — read via the lighter build_get."""

    def _capture_tick(self, app_name: str):
        app = StardagApp(
            app_name,
            builder_settings=FunctionSettings(image=MagicMock()),
            worker_settings={"default": FunctionSettings(image=MagicMock())},
        )
        captured: dict = {}

        def capture_function(**kwargs):
            def decorator(fn):
                captured[kwargs.get("name", "unknown")] = fn
                return fn

            return decorator

        app.modal_app.function = capture_function  # type: ignore[assignment]
        with patch("stardag.integration.modal._app.get_target_roots_volumes") as mv:
            mv.return_value = MagicMock(by_volume_name={}, by_root_key={})
            app.finalize()
        return captured["tick"]

    @staticmethod
    def _registry_with_reactive_app(build_id, reactive_app_name, tick_kwargs=None):
        """A registry whose build_get returns the given reactive marker.

        ``reactive_app_name=None`` models a non-reactive build.
        """
        from stardag.registry import BuildInfo

        registry = MagicMock(spec=RegistryABC)
        registry.build_get.return_value = BuildInfo(
            id=build_id,
            reactive_app_name=reactive_app_name,
            reactive_tick_kwargs=tick_kwargs,
        )
        return registry

    def test_tick_is_given_a_successor_spawner_for_its_own_app(
        self, default_in_memory_fs_target, modal_function_stub
    ):
        """The deployed tick carries the other half of the conditional
        wake-up: a worker skips spawning while a scheduler is live, so the
        scheduler must be able to hand off to a successor when a wake-up
        lands as it releases the lease. Without this the skip loses that
        wake-up until the watchdog."""
        from uuid import uuid4

        from stardag.build import TickSummary

        tick = self._capture_tick("app-owner")
        build_id = uuid4()
        registry = self._registry_with_reactive_app(build_id, "app-owner")

        captured_config: dict = {}

        async def stub_tick_aio(build_uuid, **kwargs):
            captured_config["config"] = kwargs["config"]
            return TickSummary(outcome="lingered_out")

        with (
            patch("stardag.integration.modal._tick.registry_provider") as rp,
            patch("stardag.integration.modal._tick.run_tick_aio", stub_tick_aio),
            patch(
                "stardag.integration.modal._tick.RegistryGlobalConcurrencyLockManager"
            ),
        ):
            rp.get.return_value = registry
            tick(str(build_id))

        config = captured_config["config"]
        assert config.spawn_tick is not None

        # And it spawns the tick of whichever app it is told — its own for
        # the exit hand-off, a neighbour's for a cross-build wake-up.
        successor_build_id = uuid4()
        config.spawn_tick(successor_build_id, "app-owner")
        assert modal_function_stub["from_name"] == {
            "app_name": "app-owner",
            "name": "tick",
        }
        assert modal_function_stub["op"] == "spawn"
        assert modal_function_stub["kwargs"] == {"build_id": str(successor_build_id)}

    def test_foreign_app_tick_forwards_to_owner(
        self, default_in_memory_fs_target, modal_function_stub
    ):
        """A tick from an app that doesn't own the build (per the registry's
        reactive_app_name) must not drive it — a foreign app would schedule
        with its own commit and unpickle the owner's task store (pickle
        skew) — but it forwards the wake-up to the owner's tick, so e.g. a
        still-running worker of the previous owner completing after a
        takeover doesn't drop the wake-up."""
        from uuid import uuid4

        tick = self._capture_tick("app-b")
        build_id = uuid4()
        registry = self._registry_with_reactive_app(build_id, "app-a")

        # Patch the tick loop to assert it is never entered on a foreign app.
        with (
            patch("stardag.integration.modal._tick.registry_provider") as rp,
            patch("stardag.integration.modal._tick.run_tick_aio") as tick_aio,
        ):
            rp.get.return_value = registry
            result = tick(str(build_id))

        assert result == {
            "outcome": "foreign_app",
            "owner_app": "app-a",
            "forwarded": True,
        }
        assert modal_function_stub["from_name"] == {
            "app_name": "app-a",
            "name": "tick",
        }
        assert modal_function_stub["op"] == "spawn"
        assert modal_function_stub["kwargs"] == {"build_id": str(build_id)}
        tick_aio.assert_not_called()

    def test_foreign_app_forward_failure_tolerated(self, default_in_memory_fs_target):
        """Owner app deleted (orphaned build): the forward fails, the tick
        still no-ops cleanly — logged, never raised."""
        from uuid import uuid4

        tick = self._capture_tick("app-b")
        build_id = uuid4()
        registry = self._registry_with_reactive_app(build_id, "app-gone")

        with (
            patch("stardag.integration.modal._tick.registry_provider") as rp,
            patch("stardag.integration.modal._tick.run_tick_aio") as tick_aio,
            patch(
                "modal.Function.from_name",
                side_effect=Exception("app not found"),
            ),
        ):
            rp.get.return_value = registry
            result = tick(str(build_id))

        assert result == {
            "outcome": "foreign_app",
            "owner_app": "app-gone",
            "forwarded": False,
        }
        tick_aio.assert_not_called()

    def test_non_reactive_build_is_skipped(self, default_in_memory_fs_target):
        """A build with no reactive_app_name (e.g. a resident-orchestrator
        build swept by the watchdog) is skipped before the scheduler lease."""
        from uuid import uuid4

        tick = self._capture_tick("app-a")
        build_id = uuid4()
        registry = self._registry_with_reactive_app(build_id, None)

        with (
            patch("stardag.integration.modal._tick.registry_provider") as rp,
            patch("stardag.integration.modal._tick.run_tick_aio") as tick_aio,
        ):
            rp.get.return_value = registry
            result = tick(str(build_id))

        assert result == {"outcome": "not_reactive"}
        tick_aio.assert_not_called()

    def test_own_build_proceeds(self, default_in_memory_fs_target):
        """The owning app (reactive_app_name == this app) ticks its build."""
        from uuid import uuid4

        from stardag.build import TickSummary

        ticked: list[str] = []

        async def stub_tick_aio(build_uuid, **kwargs):
            ticked.append(str(build_uuid))
            return TickSummary(outcome="noop")

        tick = self._capture_tick("app-a")
        own = uuid4()
        own_registry = self._registry_with_reactive_app(own, "app-a")

        # Patch everything past the ownership guard: lock-manager
        # construction requires configured credentials (present on dev
        # machines, absent in CI — the guard itself must not need them).
        with (
            patch("stardag.integration.modal._tick.run_tick_aio", stub_tick_aio),
            patch("stardag.integration.modal._tick.registry_provider") as rp,
            patch(
                "stardag.integration.modal._tick.RegistryGlobalConcurrencyLockManager"
            ),
        ):
            rp.get.return_value = own_registry
            assert tick(str(own))["outcome"] == "noop"
        assert ticked == [str(own)]


class TestReactiveRetrigger:
    """Re-triggering an existing reactive build: resume (un-terminal),
    append roots server-side, retry failed tasks, update reactive metadata
    in the registry (bare re-trigger preserves stored tick_kwargs)."""

    def _make_app(self):
        return StardagApp(
            "test-retrigger-app",
            builder_settings=FunctionSettings(image=_make_image()),
            worker_settings={"default": FunctionSettings(image=_make_image())},
        )

    def test_retrigger_resumes_and_appends_roots_via_registry(
        self, modal_function_stub, default_in_memory_fs_target
    ):
        from stardag.utils.testing.helper_tasks import SyncOnlyTask

        app = self._make_app()
        build_id = uuid4()
        registry = MagicMock(spec=RegistryABC)
        registry.task_register_bulk_aio.return_value = None
        original_root = SyncOnlyTask(name="rt-orig")
        new_root = SyncOnlyTask(name="rt-new")
        bootstrap = _finalize_capturing_functions(app)["bootstrap"]

        def trigger(root, **kwargs):
            """Trigger, then run the bootstrap the trigger spawned.

            The reactive metadata is written by the bootstrap now (last,
            after discovery and persistence), so a re-trigger's effect on
            it is only observable once that runs.
            """
            with registry_provider.override(registry):
                app.build_trigger(root, build_id=build_id, reactive=True, **kwargs)
                bootstrap(**modal_function_stub["kwargs"])

        # Initial trigger persists the reactive marker/config with tick_kwargs.
        trigger(original_root, tick_kwargs={"fail_mode": "continue"})
        # Initial trigger with an explicit id is treated as re-trigger for
        # resume/add-roots (harmless no-ops server-side on a fresh build).
        # The resume carries the reactive trigger's executor metadata.
        registry.build_resume.assert_called_with(
            build_id,
            executor_metadata={
                "kind": "modal",
                "app_name": app.name,
                "function_name": "bootstrap",
                "reactive": True,
                "workspace": "test-workspace",
                "environment": "test-env",
            },
        )
        registry.build_set_reactive_meta.assert_called_once_with(
            build_id, app_name=app.name, tick_kwargs={"fail_mode": "continue"}
        )

        registry.reset_mock()
        registry.task_register_bulk_aio.return_value = None
        # Re-trigger with a NEW root and UPDATED tick_kwargs.
        trigger(new_root, tick_kwargs={"linger_seconds": 5})

        assert registry.build_resume.call_count == 1
        assert registry.build_resume.call_args.args == (build_id,)
        # The new root is appended in the REGISTRY (source of truth for the
        # scheduler frontier) — not by rewriting the store.
        registry.build_add_roots.assert_called_once_with(build_id, [str(new_root.id)])
        # The reactive metadata is UPDATED in the registry on re-trigger —
        # previously impossible (it was fixed at first trigger because the
        # target-root store may be immutable). Now that it lives in the
        # (mutable) registry, the new tick_kwargs take effect.
        registry.build_set_reactive_meta.assert_called_once_with(
            build_id, app_name=app.name, tick_kwargs={"linger_seconds": 5}
        )

        registry.reset_mock()
        registry.task_register_bulk_aio.return_value = None
        # A BARE re-trigger (no explicit tick_kwargs) must PRESERVE the
        # stored config, not wipe it: the SDK passes tick_kwargs=None, which
        # the server interprets as "leave the stored config untouched" (the
        # 0.10.1 merge-semantics guarantee). Regression: a bare re-trigger
        # used to reset tick_kwargs to {}.
        trigger(new_root)
        registry.build_set_reactive_meta.assert_called_once_with(
            build_id, app_name=app.name, tick_kwargs=None
        )


class TestWatchdogSweep:
    """The sweep dispatches: one spawned tick per build, then it returns.

    It used to run every build's tick body sequentially in its own container,
    which is why it had to force ``linger_seconds=0`` and hand each build a
    fraction of the container's timeout. Both overrides are gone with the
    inline run.
    """

    @staticmethod
    def _registry(build_ids: list) -> MagicMock:
        registry = MagicMock(spec=RegistryABC)
        registry.build_list_running.return_value = build_ids
        return registry

    def test_sweep_spawns_one_tick_per_running_build(self):
        from stardag.integration.modal._tick import _run_watchdog_sweep

        build_ids = [uuid4(), uuid4()]
        spawned: list = []

        _run_watchdog_sweep(
            self._registry(build_ids),
            "an-app",
            spawn=lambda build_id, app_name: spawned.append((build_id, app_name)),
        )

        assert spawned == [(build_ids[0], "an-app"), (build_ids[1], "an-app")]

    def test_sweep_passes_no_tick_overrides(self):
        """Each spawned tick runs on its own persisted config — its normal
        linger and the whole container timeout — because it gets a container
        to itself. The sweep has nothing to say about how a build ticks.

        The spawn signature is the guarantee: ``SpawnTick`` takes a build id
        and an app name, and there is nowhere to put an override.
        """
        import inspect

        from stardag.integration.modal._spawn import spawn_tick

        assert list(inspect.signature(spawn_tick).parameters) == [
            "build_id",
            "app_name",
        ]

    def test_sweep_survives_individual_spawn_failures(self):
        from stardag.integration.modal._tick import _run_watchdog_sweep

        build_ids = [uuid4(), uuid4()]
        spawned: list = []

        def spawn(build_id, app_name):
            if build_id == build_ids[0]:
                raise RuntimeError("boom")
            spawned.append(build_id)

        _run_watchdog_sweep(self._registry(build_ids), "an-app", spawn=spawn)

        assert spawned == [build_ids[1]]  # second build still reached

    def test_sweep_noop_without_registry(self):
        from stardag.integration.modal._tick import _run_watchdog_sweep

        def _never(build_id, app_name) -> None:
            raise AssertionError("nothing to sweep without a registry")

        _run_watchdog_sweep(NoOpRegistry(), "an-app", spawn=_never)  # no raise

    def test_sweep_scopes_listing_to_this_apps_reactive_builds(self):
        """The listing is where irrelevant builds must be dropped: a tick on
        a non-reactive build is a whole (wasted) container, and unrelated
        builds otherwise consume the sweep limit."""
        from stardag.integration.modal._tick import _run_watchdog_sweep

        registry = self._registry([])

        _run_watchdog_sweep(registry, "an-app", spawn=lambda *a, **k: None)

        registry.build_list_running.assert_called_once_with(
            limit=100, reactive_app_name="an-app"
        )

    def test_truncation_warning_names_the_scope_and_the_remedy(self, caplog):
        import logging

        from stardag.integration.modal._tick import _run_watchdog_sweep

        with caplog.at_level(logging.WARNING):
            _run_watchdog_sweep(
                self._registry([uuid4(), uuid4()]),
                "an-app",
                sweep_limit=2,
                spawn=lambda *a, **k: None,
            )

        # "2+ reactive builds owned by X", not "2+ running builds": the
        # operator needs to know the cap was hit on RELEVANT builds.
        assert "2+ reactive builds owned by 'an-app'" in caplog.text
        assert "reduce the number of concurrent reactive builds" in caplog.text


class TestTriggerExecutorMetadata:
    """`function_name` records what the trigger actually spawned.

    Operator and UI surfaces render it as "what was invoked", so naming a
    function that did not run sends a reader to the wrong logs for the
    failure that stopped the build from starting.
    """

    @staticmethod
    def _app(**kwargs) -> StardagApp:
        return StardagApp(
            "an-app",
            builder_settings=FunctionSettings(image=_make_image()),
            worker_settings={"default": FunctionSettings(image=_make_image())},
            **kwargs,
        )

    def test_reactive_trigger_records_bootstrap(self):
        metadata = self._app()._build_executor_metadata(reactive=True)
        assert metadata["function_name"] == "bootstrap"

    def test_local_discovery_records_tick(self):
        """Local discovery has no bootstrap to spawn — the trigger goes
        straight to the first tick."""
        app = self._app(reactive_discovery="local")
        assert app._build_executor_metadata(reactive=True)["function_name"] == "tick"

    def test_resident_trigger_records_build(self):
        metadata = self._app()._build_executor_metadata(reactive=False)
        assert metadata["function_name"] == "build"


class TestTickFunctionTimeout:
    """Which settings the tick's own container timeout is read from.

    It is the input the per-pass spawn cap is derived from, so reading it
    from the wrong place is not a cosmetic error.
    """

    @staticmethod
    def _settings(**kwargs) -> "FunctionSettings":
        # `image` is Required on the TypedDict but irrelevant here.
        return typing.cast("FunctionSettings", kwargs)

    def test_reads_tick_settings_when_given(self):
        from stardag.integration.modal._tick import _tick_function_timeout_seconds

        assert (
            _tick_function_timeout_seconds(
                self._settings(timeout=300), self._settings(timeout=3600)
            )
            == 300.0
        )

    def test_falls_back_to_builder_settings(self):
        """tick_settings defaults to builder_settings in finalize(), so the
        timeout must follow the same fallback — otherwise every app that
        does not configure the tick separately (the common case) would
        report "unknown"."""
        from stardag.integration.modal._tick import _tick_function_timeout_seconds

        builder = self._settings(timeout=3600)
        assert _tick_function_timeout_seconds(None, builder) == 3600.0
        assert _tick_function_timeout_seconds(self._settings(), builder) == 3600.0

    def test_a_configured_zero_is_a_value_not_an_absence(self):
        """`timeout=0` was reported as "not declared", which sends the spawn
        cap to a different fallback rung than the one the function was
        registered with."""
        from stardag.integration.modal._tick import _tick_function_timeout_seconds

        assert _tick_function_timeout_seconds(self._settings(timeout=0), None) == 0.0

    def test_none_when_neither_declares_one(self):
        from stardag.integration.modal._tick import _tick_function_timeout_seconds

        assert _tick_function_timeout_seconds(None, None) is None
        assert (
            _tick_function_timeout_seconds(self._settings(cpu=2), self._settings(cpu=4))
            is None
        )


class TestBuildTickConfig:
    """Config assembly for scheduler ticks: stored tick_kwargs shared by all
    ticks, explicit kwargs win, app-level limit key selector injected."""

    def test_stored_kwargs_applied(self):
        from stardag.integration.modal._tick import _build_tick_config

        config = _build_tick_config(
            {"linger_seconds": 42, "fail_mode": "continue"},
            None,
            None,
        )
        assert config.linger_seconds == 42
        assert config.fail_mode.value == "continue"
        assert config.limit_key_selector is None

    def test_explicit_kwargs_win_and_selector_injected(self):
        from stardag.integration.modal._tick import _build_tick_config

        selector = lambda t: ["gpu"]  # noqa: E731
        config = _build_tick_config(
            {"linger_seconds": 42},
            {"linger_seconds": 7},
            selector,
        )
        assert config.linger_seconds == 7
        assert config.limit_key_selector is selector

    def test_defaults_without_stored_kwargs(self):
        from stardag.build import TickConfig
        from stardag.integration.modal._tick import _build_tick_config

        config = _build_tick_config(None, None, None)
        assert config.linger_seconds == TickConfig().linger_seconds
        assert config.tick_timeout_seconds is None

    def test_tick_function_timeout_applied_as_a_default(self):
        """The deployed tick's own Modal ``timeout`` — how long this
        container may live — is what the per-pass spawn cap is derived
        from, so it has to reach the TickConfig."""
        from stardag.integration.modal._tick import _build_tick_config

        config = _build_tick_config(None, None, None, tick_timeout_seconds=300.0)

        assert config.tick_timeout_seconds == 300.0

    def test_caller_supplied_budget_wins_over_the_function_timeout(self):
        """A default, not an override: the watchdog sweep runs several
        ticks in one container and passes on its own share of the budget."""
        from stardag.integration.modal._tick import _build_tick_config

        config = _build_tick_config(
            None,
            {"linger_seconds": 0, "tick_timeout_seconds": 60.0},
            None,
            tick_timeout_seconds=600.0,
        )

        assert config.tick_timeout_seconds == 60.0

    def test_tick_timeout_is_not_a_persistable_tick_kwarg(self):
        """It is a deploy-time fact about the container, not per-build
        config: persisting it in a build's stored tick_kwargs would go
        stale on the next redeploy."""
        from stardag.integration.modal._tick import _TICK_KWARGS_ALLOWED

        assert "tick_timeout_seconds" not in _TICK_KWARGS_ALLOWED


class TestContainerSetup:
    """``StardagApp(container_setup=...)``: the app's per-container hook.

    The point of the hook is that it reaches all five registered
    functions. ``build`` and ``worker_*`` already import the app's code
    (they close over its build/run functions); ``tick``, ``bootstrap`` and
    ``tick_watchdog`` did not, and ``bootstrap`` closed over nothing of the
    app's at all — so those three are what these tests are really about.
    """

    @pytest.fixture(autouse=True)
    def _fresh_container(self):
        """Each test starts as a container that has not run setup yet."""
        _reset_container_setup_for_testing()
        yield
        _reset_container_setup_for_testing()

    @staticmethod
    def _app(container_setup) -> StardagApp:
        return StardagApp(
            "test-app",
            container_setup=container_setup,
            build_function=lambda *args, **kwargs: None,
            run_function=lambda task, **kwargs: None,
            builder_settings=FunctionSettings(image=_make_image()),
            worker_settings={
                "default": FunctionSettings(image=_make_image()),
                "gpu": FunctionSettings(image=_make_image()),
            },
            watchdog_period_minutes=5,
        )

    @staticmethod
    @contextlib.contextmanager
    def _stubbed_bodies():
        """Stub out what the wrappers delegate to, leaving only the hook."""
        with contextlib.ExitStack() as stack:
            tick = stack.enter_context(
                patch("stardag.integration.modal._app._run_tick")
            )
            bootstrap = stack.enter_context(
                patch("stardag.integration.modal._app.run_reactive_bootstrap")
            )
            stack.enter_context(
                patch("stardag.integration.modal._app._run_watchdog_sweep")
            )
            tick.return_value = {}
            bootstrap.return_value = MagicMock(summary={})
            yield

    # One invocation of each registered function, with dummy arguments.
    _INVOCATIONS: dict[str, typing.Callable[[typing.Any], typing.Any]] = {
        "build": lambda fn: fn("task", "selector", "test-app", None),
        "worker_default": lambda fn: fn("task"),
        "worker_gpu": lambda fn: fn("task"),
        "tick": lambda fn: fn(str(uuid4()), None),
        "bootstrap": lambda fn: fn(str(uuid4()), [], None),
        "tick_watchdog": lambda fn: fn(),
    }

    @pytest.mark.parametrize("function_name", list(_INVOCATIONS))
    def test_runs_in_every_registered_function(self, function_name):
        """Including the three that import nothing of the app's own."""
        calls = []
        app = self._app(lambda: calls.append("setup"))
        registered = _finalize_capturing_functions(app)

        assert function_name in registered
        with self._stubbed_bodies(), registry_provider.override(NoOpRegistry()):
            self._INVOCATIONS[function_name](registered[function_name])

        assert calls == ["setup"]

    def test_runs_once_per_container_not_once_per_input(self):
        """A worker serves many tasks and a tick container may be reused —
        stardag holds the guard so apps need not write one."""
        calls = []
        app = self._app(lambda: calls.append("setup"))
        registered = _finalize_capturing_functions(app)

        with self._stubbed_bodies(), registry_provider.override(NoOpRegistry()):
            registered["worker_default"]("task")
            registered["worker_default"]("task")
            registered["worker_gpu"]("task")
            registered["build"]("task", "selector", "test-app", None)
            registered["tick"](str(uuid4()), None)

        assert calls == ["setup"]

    def test_failure_propagates_and_is_retried_on_the_next_input(self):
        """Not memoised on failure: the alternative is a container whose
        remaining inputs run silently un-set-up."""
        attempts = []

        def flaky():
            attempts.append("attempt")
            if len(attempts) == 1:
                raise RuntimeError("setup boom")

        app = self._app(flaky)
        registered = _finalize_capturing_functions(app)

        with pytest.raises(RuntimeError, match="setup boom"):
            registered["worker_default"]("task")
        registered["worker_default"]("task")  # retried, and succeeds
        registered["worker_default"]("task")  # now remembered as done

        assert attempts == ["attempt", "attempt"]

    def test_runs_before_stardag_logging_default(self):
        """The whole reason an app can own its log formatter in these
        containers: ``basicConfig`` no-ops once root has handlers."""
        order = []
        app = self._app(lambda: order.append("container_setup"))
        registered = _finalize_capturing_functions(app)

        with (
            self._stubbed_bodies(),
            registry_provider.override(NoOpRegistry()),
            patch(
                "stardag.integration.modal._app._setup_logging",
                side_effect=lambda: order.append("stardag_logging"),
            ),
        ):
            registered["bootstrap"](str(uuid4()), [], None)

        assert order == ["container_setup", "stardag_logging"]

    def test_two_apps_in_one_process_each_run_their_own_hook(self):
        """The guard is per hook, not one global flag.

        A deployed container only ever unpickles one app's closure, so in
        production there is one hook — but a process holding two apps
        would otherwise have the first app's hook silence the second's,
        which is a wrong answer rather than a missed optimisation.
        """
        first, second = [], []
        app_a = self._app(lambda: first.append("a"))
        app_b = self._app(lambda: second.append("b"))
        registered_a = _finalize_capturing_functions(app_a)
        registered_b = _finalize_capturing_functions(app_b)

        with self._stubbed_bodies(), registry_provider.override(NoOpRegistry()):
            registered_a["worker_default"]("task")
            registered_b["worker_default"]("task")
            registered_a["worker_default"]("task")
            registered_b["worker_default"]("task")

        assert first == ["a"]
        assert second == ["b"]

    def test_defaults_to_none_and_is_a_no_op(self):
        """Additive: an app that passes nothing behaves exactly as before."""
        app = StardagApp(
            "test-app",
            builder_settings=FunctionSettings(image=_make_image()),
            worker_settings={"default": FunctionSettings(image=_make_image())},
        )
        assert app.container_setup is None

        registered = _finalize_capturing_functions(app)
        with self._stubbed_bodies(), registry_provider.override(NoOpRegistry()):
            registered["tick"](str(uuid4()), None)

        # Nothing recorded, so nothing was run and nothing is retained.
        assert _container_setup_module._setup_done == []

    def test_rejects_a_non_callable(self):
        """Caught where the app is declared, not in a container hours later."""
        with pytest.raises(TypeError, match="container_setup must be callable"):
            self._app("not-callable")  # type: ignore[arg-type]

    @pytest.mark.parametrize("function_name", list(_INVOCATIONS))
    def test_runs_before_the_wrapper_body(self, function_name):
        """Not merely *that* it ran — that it ran FIRST.

        Everything the feature claims rests on this: the hook has to
        precede the wrapper's own body, which is what puts it ahead of
        ``Builder.setup`` / ``Runner.setup`` / ``_run_tick`` and therefore
        ahead of stardag's ``logging.basicConfig`` default. Asserting only
        that the hook ran would pass with the call moved to the bottom of
        every wrapper.
        """
        order: list[str] = []

        def body(*args, **kwargs):
            order.append("body")
            return None

        app = StardagApp(
            "test-app",
            container_setup=lambda: order.append("container_setup"),
            build_function=body,
            run_function=body,
            builder_settings=FunctionSettings(image=_make_image()),
            worker_settings={
                "default": FunctionSettings(image=_make_image()),
                "gpu": FunctionSettings(image=_make_image()),
            },
            watchdog_period_minutes=5,
        )
        registered = _finalize_capturing_functions(app)

        with contextlib.ExitStack() as stack:
            tick = stack.enter_context(
                patch("stardag.integration.modal._app._run_tick")
            )
            bootstrap = stack.enter_context(
                patch("stardag.integration.modal._app.run_reactive_bootstrap")
            )
            sweep = stack.enter_context(
                patch("stardag.integration.modal._app._run_watchdog_sweep")
            )
            stack.enter_context(registry_provider.override(NoOpRegistry()))
            tick.side_effect = lambda *a, **kw: (order.append("body"), {})[1]
            bootstrap.side_effect = lambda *a, **kw: (
                order.append("body"),
                MagicMock(summary={}),
            )[1]
            sweep.side_effect = lambda *a, **kw: order.append("body")
            self._INVOCATIONS[function_name](registered[function_name])

        assert order == ["container_setup", "body"]

    def test_watchdog_does_not_run_a_tick_body_in_process(self):
        """The sweep spawns; it no longer calls the tick wrapper in-process.

        That call used to re-enter the container-setup guard, whose plain
        ``threading.Lock`` would have *hung* the container rather than failed
        it, so the re-entry needed pinning. Dispatching removes the hazard at
        the source: the watchdog hands the sweep an app name, not a callable,
        and there is no longer a tick body in this container to re-enter.
        """
        calls = []
        app = self._app(lambda: calls.append("setup"))
        registered = _finalize_capturing_functions(app)

        with contextlib.ExitStack() as stack:
            tick = stack.enter_context(
                patch("stardag.integration.modal._app._run_tick")
            )
            sweep = stack.enter_context(
                patch("stardag.integration.modal._app._run_watchdog_sweep")
            )
            stack.enter_context(registry_provider.override(NoOpRegistry()))
            registered["tick_watchdog"]()

        assert calls == ["setup"]
        assert tick.call_count == 0, (
            "the watchdog container must not run a tick body — one sweep "
            "spawns N ticks and returns"
        )
        args, kwargs = sweep.call_args
        assert args[1] == app.name, "the app name is the scope AND the target"
        assert not kwargs

    def test_concurrent_inputs_run_the_hook_exactly_once(self):
        """``allow_concurrent_inputs`` serves inputs on threads.

        Sparing every app from writing this guard is the stated reason it
        lives in stardag, so the double-checked lock is worth pinning: a
        regression to a bare check would let several threads through.
        """
        calls = []
        barrier = threading.Barrier(8)

        def slow_setup():
            calls.append("setup")
            time.sleep(0.05)

        app = self._app(slow_setup)
        registered = _finalize_capturing_functions(app)
        errors: list[BaseException] = []

        def invoke():
            try:
                barrier.wait(timeout=5)
                registered["worker_default"]("task")
            except BaseException as e:  # noqa: BLE001 - reported below
                errors.append(e)

        threads = [threading.Thread(target=invoke) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        assert errors == []
        assert calls == ["setup"]

    def test_rejects_a_hook_that_takes_arguments(self):
        """The likelier mistake than a non-callable, and it would
        otherwise deploy cleanly and raise in every container."""
        with pytest.raises(TypeError, match="no arguments"):
            self._app(lambda config: None)  # type: ignore[arg-type,misc]


class TestUnreachableWorkerWarning:
    """finalize() flags workers no task can be routed to.

    Without a ``worker_selector`` everything routes to ``"default"``, so a
    declared ``gpu`` worker is deployed and never reached — a deployment
    that looks entirely healthy while running on the wrong tier.
    """

    @staticmethod
    def _app(workers, worker_selector=None) -> StardagApp:
        return StardagApp(
            "test-app",
            worker_selector=worker_selector,
            builder_settings=FunctionSettings(image=_make_image()),
            worker_settings={
                name: FunctionSettings(image=_make_image()) for name in workers
            },
        )

    def test_warns_when_extra_workers_have_no_selector(self, caplog):
        app = self._app(["default", "gpu", "high_memory"])

        with caplog.at_level("WARNING", logger="stardag.integration.modal._app"):
            _finalize_capturing_functions(app)

        assert "no worker_selector" in caplog.text
        # Names the workers that are actually unreachable, not "default".
        assert "gpu, high_memory" in caplog.text

    def test_no_warning_when_a_selector_is_declared(self, caplog):
        """Explicit is intent — even a selector that returns 'default'."""
        app = self._app(["default", "gpu"], worker_selector=lambda task: "default")

        with caplog.at_level("WARNING", logger="stardag.integration.modal._app"):
            _finalize_capturing_functions(app)

        assert "worker_selector" not in caplog.text

    def test_raises_when_there_is_no_default_worker_and_no_selector(self):
        """Nothing works at all here, so it is an error, not a warning:
        every task routes to a function the app does not deploy."""
        app = self._app(["gpu", "high_memory"])

        with pytest.raises(StardagError, match="no 'default' worker"):
            _finalize_capturing_functions(app)

    def test_no_default_worker_is_fine_with_a_declared_selector(self):
        """An app routing everything to its own tiers works today —
        refusing it would break a working deployment over a name."""
        app = self._app(["gpu"], worker_selector=lambda task: "gpu")

        registered = _finalize_capturing_functions(app)

        assert "worker_gpu" in registered
        assert "worker_default" not in registered

    def test_no_warning_for_a_single_worker(self, caplog):
        """The default routing is correct by construction here."""
        app = self._app(["default"])

        with caplog.at_level("WARNING", logger="stardag.integration.modal._app"):
            _finalize_capturing_functions(app)

        assert "worker_selector" not in caplog.text


ENTRY_POINT_SOURCE = '''\
"""Stands in for a deploy entry point — a conventional modal/app.py."""

import functools


def pick_worker(task):
    return "default"


def setup():
    return None


pick_worker_partial = functools.partial(pick_worker)

pick_worker_lambda = lambda task: "default"  # noqa: E731


def _make_closure():
    def pick(task):
        return "default"

    return pick


pick_worker_closure = _make_closure()


class Selector:
    def __call__(self, task):
        return "default"


selector_instance = Selector()
'''


@pytest.fixture
def entry_point(tmp_path):
    """A module loaded exactly the way ``stardag modal deploy`` loads one.

    ``_import_file_or_module`` names the module after the file, puts its
    directory on ``sys.path`` and registers it in ``sys.modules`` — so
    "app" is a perfectly resolvable module *in this process*, and that is
    the whole trap. The fixture reproduces that, and the surrounding
    ``_loading_deploy_entrypoint`` scope, without shelling out to Modal.
    """
    path = tmp_path / "app.py"
    path.write_text(ENTRY_POINT_SOURCE)
    spec = importlib.util.spec_from_file_location("app", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get("app")
    sys.modules["app"] = module
    sys.path.insert(0, str(tmp_path))
    try:
        with _loading_deploy_entrypoint("app"):
            spec.loader.exec_module(module)
            yield module
    finally:
        sys.path.remove(str(tmp_path))
        if previous is None:
            sys.modules.pop("app", None)
        else:
            sys.modules["app"] = previous


class TestSerializedCallablePlacement:
    """Callables an app hands ``StardagApp`` must be importable in a container.

    All five are cloudpickled into the ``serialized=True`` functions
    ``finalize()`` registers, and cloudpickle stores a module-level
    callable as a reference to its defining module. One defined in the
    deploy entry point therefore deploys cleanly and then cannot be
    hydrated anywhere — the failure lands minutes later, in whichever
    functions happen to carry it.
    """

    @staticmethod
    def _app(**kwargs) -> StardagApp:
        return StardagApp(
            "test-app",
            builder_settings=FunctionSettings(image=_make_image()),
            worker_settings={"default": FunctionSettings(image=_make_image())},
            task_modules=[],
            **kwargs,
        )

    # -- the failure this exists to prevent ---------------------------------

    def test_entry_point_def_pickles_by_reference_to_a_module_no_container_has(
        self, entry_point
    ):
        """The bug itself, pinned at the layer where it happens.

        Modal serializes with its vendored cloudpickle, so this asserts
        against the very pickler a deploy uses: a module-level def in the
        entry point comes out as a bare ``app.pick_worker`` reference, and
        unpickling it anywhere else means importing ``app``.
        """
        from modal._vendor import cloudpickle

        buffer = io.BytesIO()
        cloudpickle.CloudPickler(buffer, protocol=4).dump(entry_point.pick_worker)

        globals_referenced = [
            argument
            for opcode, argument, _ in pickletools.genops(buffer.getvalue())
            if opcode.name in ("SHORT_BINUNICODE", "BINUNICODE")
        ]
        assert globals_referenced == ["app", "pick_worker"]

    def test_the_module_resolves_locally_which_is_why_find_spec_cannot_catch_it(
        self, entry_point
    ):
        """Why the CLI has to *tell* the app the name it loaded.

        The deploying process can import ``app`` — it is in
        ``sys.modules`` and its directory is on ``sys.path``. Nothing
        about the name looks synthetic from here; only the container
        knows it is not real.
        """
        assert importlib.util.find_spec("app") is not None

    # -- the guardrail ------------------------------------------------------

    @pytest.mark.parametrize(
        "parameter",
        [
            "build_function",
            "run_function",
            "container_setup",
            "worker_selector",
            "limit_key_selector",
        ],
    )
    def test_rejects_a_def_from_the_entry_point_for_every_parameter(
        self, entry_point, parameter
    ):
        """All five are serialized the same way, so all five are checked."""
        callable_ = (
            entry_point.setup
            if parameter == "container_setup"
            else (entry_point.pick_worker)
        )

        with pytest.raises(SerializedCallablePlacementError) as excinfo:
            self._app(**{parameter: callable_})

        message = str(excinfo.value)
        assert parameter in message
        assert "add_local_python_source" in message

    def test_the_error_names_the_callable_the_module_and_the_symptom(self, entry_point):
        """An author reading this should not have to infer any of it: what
        was rejected, the module name that will not exist, and the
        ``ModuleNotFoundError`` they would otherwise have gone looking
        for."""
        with pytest.raises(SerializedCallablePlacementError) as excinfo:
            self._app(worker_selector=entry_point.pick_worker)

        message = str(excinfo.value)
        assert "pick_worker" in message
        assert "'app'" in message
        assert "No module named 'app'" in message

    def test_rejects_an_instance_of_a_class_defined_in_the_entry_point(
        self, entry_point
    ):
        """A ``Builder``/``Runner`` subclass is the documented way to
        customise a build, and it fails identically: the instance pickles
        as a reconstruction of its class, and the class is the reference.
        """
        with pytest.raises(SerializedCallablePlacementError, match="Selector"):
            self._app(worker_selector=entry_point.selector_instance)

    def test_rejects_a_partial_wrapping_an_entry_point_def(self, entry_point):
        """``functools.partial`` is what an app is pointed at for binding
        configuration to a hook, and it is transparent to the trap: the
        partial pickles by value but carries the reference to its func.
        """
        with pytest.raises(SerializedCallablePlacementError, match="pick_worker"):
            self._app(worker_selector=entry_point.pick_worker_partial)

    # -- what must keep working --------------------------------------------

    def test_accepts_a_lambda_defined_in_the_entry_point(self, entry_point):
        """cloudpickle cannot look a lambda up by name, so it writes the
        code object out by value — no import needed in the container.
        Rejecting these would break apps that work today.
        """
        self._app(worker_selector=entry_point.pick_worker_lambda)

    def test_accepts_a_closure_defined_in_the_entry_point(self, entry_point):
        """Same reason as the lambda: ``pick`` is not reachable under its
        own qualname, so it is serialized by value."""
        self._app(worker_selector=entry_point.pick_worker_closure)

    def test_accepts_a_callable_imported_into_the_entry_point(self, entry_point):
        """The fix the error asks for. Defined in a real, importable
        module and merely *referenced* from the entry point."""
        self._app(worker_selector=_importable_worker_selector)

    def test_accepts_a_module_level_def_outside_a_deploy(self):
        """Constructing an app in ordinary code — a test, a notebook, a
        library — is untouched: nothing is loading an entry point, and the
        module resolves."""
        assert _container_setup_module._deploy_entrypoint_module is None

        self._app(worker_selector=_importable_worker_selector)

    def test_accepts_the_defaults(self, entry_point):
        """The default build/run functions are stardag's own, and stardag
        is in the image by construction."""
        app = self._app()

        assert app._build_function is _default_build
        assert app._run_function is _default_run

    def test_accepts_a_main_module_callable(self, entry_point, monkeypatch):
        """``__main__`` is the one unimportable module cloudpickle already
        handles: it refuses to reference it and falls back to pickling by
        value. Rejecting it would be wrong, and would fire on every app
        run as a script."""
        monkeypatch.setattr(
            _importable_worker_selector, "__module__", "__main__", raising=False
        )

        self._app(worker_selector=_importable_worker_selector)

    def test_entry_point_name_is_restored_after_loading(self, tmp_path):
        """Nested scopes restore rather than clear: a process deploying
        two apps must not carry the first entry point's name into the
        second."""
        assert _container_setup_module._deploy_entrypoint_module is None

        with _loading_deploy_entrypoint("first"):
            with _loading_deploy_entrypoint("second"):
                assert _container_setup_module._deploy_entrypoint_module == "second"
            assert _container_setup_module._deploy_entrypoint_module == "first"

        assert _container_setup_module._deploy_entrypoint_module is None


def _importable_worker_selector(task) -> str:
    """A selector living in a module a container really could import."""
    return "default"
