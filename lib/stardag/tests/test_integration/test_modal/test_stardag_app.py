"""Tests for StardagApp build_function and run_function customization."""

import pytest

try:
    import modal  # noqa: F401
except ImportError:
    pytest.skip("Skipping modal tests (import not available)", allow_module_level=True)

from unittest.mock import MagicMock, patch
from uuid import uuid4

from stardag import BaseTask
from stardag.build._base import BuildSummary
from stardag.integration.modal import (
    Builder,
    BuildFunction,
    FunctionSettings,
    RunFunction,
    Runner,
    StardagApp,
)
from stardag.integration.modal._app import _default_build, _default_run
from stardag.registry import NoOpRegistry, RegistryABC, registry_provider


def _make_image() -> modal.Image:
    return modal.Image.debian_slim()


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
        from stardag.integration.modal import _app as modal_app_module

        captured: dict = {}

        def fake_build(tasks, **kwargs):
            captured["tasks"] = tasks
            captured["kwargs"] = kwargs
            return None

        monkeypatch.setattr(modal_app_module, "build", fake_build)

        builder = modal_app_module.Builder()
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
        from stardag.integration.modal import _app as modal_app_module

        captured: dict = {}

        def fake_build(tasks, **kwargs):
            captured["kwargs"] = kwargs
            return None

        monkeypatch.setattr(modal_app_module, "build", fake_build)

        builder = modal_app_module.Builder()
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

    def test_reactive_trigger_discovers_persists_and_spawns_tick(
        self, modal_function_stub, default_in_memory_fs_target
    ):
        from uuid import uuid4 as _uuid4

        from stardag.build import BuildTaskStore
        from stardag.utils.testing.helper_tasks import SyncOnlyTask

        app = self._make_app()
        build_id = _uuid4()
        registry = MagicMock(spec=RegistryABC)
        registry.build_start.return_value = build_id
        dep = SyncOnlyTask(name="reactive-dep")
        root = SyncOnlyTask(name="reactive-root", deps=(dep,))

        with registry_provider.override(registry):
            result = app.build_trigger(
                root, reactive=True, tick_kwargs={"linger_seconds": 30}
            )

        assert result.build_id == build_id
        # First tick spawned with only the build id (config comes from the
        # registry reactive_tick_kwargs so ALL ticks — worker wake-ups,
        # watchdog — share it).
        assert modal_function_stub["from_name"]["name"] == "tick"
        assert modal_function_stub["op"] == "spawn"
        assert modal_function_stub["kwargs"] == {"build_id": str(build_id)}
        # Discovery registered the DAG…
        registry.task_register_bulk_aio.assert_called()
        # …the reactive marker/owner/config were written to the REGISTRY
        # (not the target root, which may be immutable).
        registry.build_set_reactive_meta.assert_called_once_with(
            build_id, app_name=app.name, tick_kwargs={"linger_seconds": 30}
        )
        # …and the task store holds the rehydratable pickles (objects only).
        store = BuildTaskStore(build_id)
        loaded_root = store.load_task(root.id)
        assert loaded_root is not None and loaded_root.id == root.id
        assert store.load_task(dep.id) is not None

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
    """A build minted by ``build_start`` is RUNNING until a terminal EVENT
    says otherwise — and no orchestrator exists yet to emit one. So a
    trigger that dies after minting must emit ``BUILD_FAILED`` itself, or
    it leaves a build that is RUNNING forever and (because the reactive
    marker is written last) not even attributable to an app.
    """

    def _make_app(self):
        return StardagApp(
            "test-reactive-app",
            builder_settings=FunctionSettings(image=_make_image()),
            worker_settings={"default": FunctionSettings(image=_make_image())},
        )

    def _make_registry(self, build_id):
        registry = MagicMock(spec=RegistryABC)
        registry.build_start.return_value = build_id
        return registry

    def _root(self):
        from stardag.utils.testing.helper_tasks import SyncOnlyTask

        return SyncOnlyTask(name="reactive-root")

    def test_task_store_write_failure_fails_the_build(
        self, modal_function_stub, default_in_memory_fs_target
    ):
        app = self._make_app()
        build_id = uuid4()
        registry = self._make_registry(build_id)

        with patch(
            "stardag.integration.modal._app.BuildTaskStore.save_tasks",
            side_effect=PermissionError("read-only target root"),
        ):
            with registry_provider.override(registry):
                with pytest.raises(PermissionError, match="read-only target root"):
                    app.build_trigger(self._root(), reactive=True)

        registry.build_fail.assert_called_once()
        message = registry.build_fail.call_args.args[1]
        assert "task store write" in message
        assert "read-only target root" in message
        # The marker is written after the store, so this build would have
        # been an unattributable orphan; it must not be left RUNNING.
        registry.build_set_reactive_meta.assert_not_called()

    def test_discovery_failure_fails_the_build(
        self, modal_function_stub, default_in_memory_fs_target
    ):
        """Reordering the store write ahead of ``build_start`` would not
        cover this: discovery runs against the build id too."""
        app = self._make_app()
        build_id = uuid4()
        registry = self._make_registry(build_id)
        registry.task_register_bulk_aio.side_effect = RuntimeError("registry down")

        with registry_provider.override(registry):
            with pytest.raises(RuntimeError, match="registry down"):
                app.build_trigger(self._root(), reactive=True)

        assert (
            "task discovery and registration" in registry.build_fail.call_args.args[1]
        )

    def test_secondary_build_fail_error_never_masks_the_root_cause(
        self, modal_function_stub, default_in_memory_fs_target
    ):
        app = self._make_app()
        build_id = uuid4()
        registry = self._make_registry(build_id)
        registry.build_fail.side_effect = RuntimeError("registry unreachable too")

        with patch(
            "stardag.integration.modal._app.BuildTaskStore.save_tasks",
            side_effect=PermissionError("read-only target root"),
        ):
            with registry_provider.override(registry):
                # The ORIGINAL error propagates, not the bookkeeping one.
                with pytest.raises(PermissionError, match="read-only target root"):
                    app.build_trigger(self._root(), reactive=True)

    def test_resume_failure_on_retrigger_does_not_terminate_the_build(
        self, modal_function_stub, default_in_memory_fs_target
    ):
        """A re-trigger's build may still be TERMINAL on entry — nothing has
        started it. Emitting BUILD_FAILED when the resume itself fails would
        flip a COMPLETED build to FAILED over a transient registry error,
        which is strictly worse than the orphan this wrapper prevents.
        """
        app = self._make_app()
        build_id = uuid4()
        registry = self._make_registry(build_id)
        registry.build_resume.side_effect = RuntimeError("registry down")

        with registry_provider.override(registry):
            with pytest.raises(RuntimeError, match="registry down"):
                app.build_trigger(self._root(), reactive=True, build_id=build_id)

        registry.build_fail.assert_not_called()

    def test_failure_after_a_successful_resume_does_terminate_the_build(
        self, modal_function_stub, default_in_memory_fs_target
    ):
        """The other side of the same coin: once resume has put the build
        back into RUNNING, this trigger owns it and must not abandon it."""
        app = self._make_app()
        build_id = uuid4()
        registry = self._make_registry(build_id)
        registry.task_register_bulk_aio.side_effect = RuntimeError("registry down")

        with registry_provider.override(registry):
            with pytest.raises(RuntimeError, match="registry down"):
                app.build_trigger(self._root(), reactive=True, build_id=build_id)

        registry.build_resume.assert_called_once()
        assert (
            "task discovery and registration" in registry.build_fail.call_args.args[1]
        )

    def test_spawn_failure_leaves_the_build_recoverable(
        self, modal_function_stub, default_in_memory_fs_target, monkeypatch
    ):
        """The spawn is outside the wrapper on purpose: the durable state is
        complete and the build carries the reactive marker, so the app's
        watchdog recovers it. Failing it would remove that recovery path."""
        app = self._make_app()
        build_id = uuid4()
        registry = self._make_registry(build_id)

        def _boom(*args, **kwargs):
            raise RuntimeError("modal spawn failed")

        with registry_provider.override(registry):
            with patch("modal.Function.from_name") as from_name:
                from_name.return_value.spawn = _boom
                with pytest.raises(RuntimeError, match="modal spawn failed"):
                    app.build_trigger(self._root(), reactive=True)

        registry.build_set_reactive_meta.assert_called_once()
        registry.build_fail.assert_not_called()


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
        from stardag.integration.modal._app import STARDAG_MODAL_WORKSPACE_ENV

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
    def test_tick_registered_watchdog_off_by_default(self, mock_volumes):
        mock_volumes.return_value = MagicMock(by_volume_name={}, by_root_key={})
        app, registered = self._capture_app()

        result = app.finalize()

        assert "tick" in registered
        assert "tick_watchdog" not in registered
        assert "tick" in result.functions

    @patch("stardag.integration.modal._app.get_target_roots_volumes")
    def test_watchdog_registered_with_period(self, mock_volumes):
        mock_volumes.return_value = MagicMock(by_volume_name={}, by_root_key={})
        app, registered = self._capture_app(watchdog_period_minutes=7)

        result = app.finalize()

        assert "tick_watchdog" in registered
        schedule = registered["tick_watchdog"]["schedule"]
        assert isinstance(schedule, modal.Period)
        assert "tick_watchdog" in result.functions


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
            patch("stardag.integration.modal._app.registry_provider") as rp,
            patch("stardag.integration.modal._app.run_tick_aio") as tick_aio,
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
            patch("stardag.integration.modal._app.registry_provider") as rp,
            patch("stardag.integration.modal._app.run_tick_aio") as tick_aio,
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
            patch("stardag.integration.modal._app.registry_provider") as rp,
            patch("stardag.integration.modal._app.run_tick_aio") as tick_aio,
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
            patch("stardag.integration.modal._app.run_tick_aio", stub_tick_aio),
            patch("stardag.integration.modal._app.registry_provider") as rp,
            patch(
                "stardag.integration.modal._app.RegistryGlobalConcurrencyLockManager"
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

        # Initial trigger persists the reactive marker/config with tick_kwargs.
        with registry_provider.override(registry):
            app.build_trigger(
                original_root,
                build_id=build_id,
                reactive=True,
                tick_kwargs={"fail_mode": "continue"},
            )
        # Initial trigger with an explicit id is treated as re-trigger for
        # resume/add-roots (harmless no-ops server-side on a fresh build).
        # The resume carries the reactive trigger's executor metadata.
        registry.build_resume.assert_called_with(
            build_id,
            executor_metadata={
                "kind": "modal",
                "app_name": app.name,
                "function_name": "tick",
                "reactive": True,
                "workspace": "test-workspace",
                "environment": "test-env",
            },
        )
        registry.build_set_reactive_meta.assert_called_once_with(
            build_id, app_name=app.name, tick_kwargs={"fail_mode": "continue"}
        )

        registry.reset_mock()
        # Re-trigger with a NEW root and UPDATED tick_kwargs.
        with registry_provider.override(registry):
            app.build_trigger(
                new_root,
                build_id=build_id,
                reactive=True,
                tick_kwargs={"linger_seconds": 5},
            )

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
        # A BARE re-trigger (no explicit tick_kwargs) must PRESERVE the
        # stored config, not wipe it: the SDK passes tick_kwargs=None, which
        # the server interprets as "leave the stored config untouched" (the
        # 0.10.1 merge-semantics guarantee). Regression: a bare re-trigger
        # used to reset tick_kwargs to {}.
        with registry_provider.override(registry):
            app.build_trigger(new_root, build_id=build_id, reactive=True)
        registry.build_set_reactive_meta.assert_called_once_with(
            build_id, app_name=app.name, tick_kwargs=None
        )


class TestWatchdogSweep:
    def test_sweep_ticks_each_running_build_without_linger(self):
        from stardag.integration.modal._app import _run_watchdog_sweep

        build_ids = [uuid4(), uuid4()]
        registry = MagicMock(spec=RegistryABC)
        registry.build_list_running.return_value = build_ids
        ticked: list = []

        def tick(build_id, tick_kwargs=None):
            ticked.append((build_id, tick_kwargs))

        _run_watchdog_sweep(registry, tick)

        assert ticked == [
            (str(build_ids[0]), {"linger_seconds": 0}),
            (str(build_ids[1]), {"linger_seconds": 0}),
        ]

    def test_sweep_survives_individual_tick_failures(self):
        from stardag.integration.modal._app import _run_watchdog_sweep

        build_ids = [uuid4(), uuid4()]
        registry = MagicMock(spec=RegistryABC)
        registry.build_list_running.return_value = build_ids
        ticked: list = []

        def tick(build_id, tick_kwargs=None):
            if build_id == str(build_ids[0]):
                raise RuntimeError("boom")
            ticked.append(build_id)

        _run_watchdog_sweep(registry, tick)

        assert ticked == [str(build_ids[1])]  # second build still swept

    def test_sweep_noop_without_registry(self):
        from stardag.integration.modal._app import _run_watchdog_sweep

        _run_watchdog_sweep(NoOpRegistry(), lambda *a, **k: 1 / 0)  # no raise

    def test_sweep_scopes_listing_to_this_apps_reactive_builds(self):
        """The listing — not the tick — is where irrelevant builds must be
        dropped: a tick on a non-reactive build is a whole (wasted) function
        invocation, and unrelated builds otherwise consume the sweep limit."""
        from stardag.integration.modal._app import _run_watchdog_sweep

        registry = MagicMock(spec=RegistryABC)
        registry.build_list_running.return_value = []

        _run_watchdog_sweep(registry, lambda *a, **k: None, reactive_app_name="an-app")

        registry.build_list_running.assert_called_once_with(
            limit=100, reactive_app_name="an-app"
        )

    def test_sweep_degrades_to_unscoped_listing_on_old_registry(self):
        """A custom RegistryABC implementation predating the kwarg must not
        break the sweep — it just gets the wider listing it always got."""
        from stardag.integration.modal._app import _run_watchdog_sweep

        calls: list = []

        class OldRegistry(NoOpRegistry):
            def build_list_running(self, limit: int = 100):  # type: ignore[override]
                calls.append(limit)
                return []

        _run_watchdog_sweep(
            OldRegistry(), lambda *a, **k: None, reactive_app_name="an-app"
        )

        assert calls == [100]

    def test_truncation_warning_names_the_scope_and_the_remedy(self, caplog):
        import logging

        from stardag.integration.modal._app import _run_watchdog_sweep

        registry = MagicMock(spec=RegistryABC)
        registry.build_list_running.return_value = [uuid4(), uuid4()]

        with caplog.at_level(logging.WARNING):
            _run_watchdog_sweep(
                registry,
                lambda *a, **k: None,
                sweep_limit=2,
                reactive_app_name="an-app",
            )

        # "2+ reactive builds owned by X", not "2+ running builds": the
        # operator needs to know the cap was hit on RELEVANT builds.
        assert "2+ reactive builds owned by 'an-app'" in caplog.text
        assert "Cancel or clean up builds" in caplog.text


class TestBuildTickConfig:
    """Config assembly for scheduler ticks: stored tick_kwargs shared by all
    ticks, explicit kwargs win, app-level limit key selector injected."""

    def test_stored_kwargs_applied(self):
        from stardag.integration.modal._app import _build_tick_config

        config = _build_tick_config(
            {"linger_seconds": 42, "fail_mode": "continue"},
            None,
            None,
        )
        assert config.linger_seconds == 42
        assert config.fail_mode.value == "continue"
        assert config.limit_key_selector is None

    def test_explicit_kwargs_win_and_selector_injected(self):
        from stardag.integration.modal._app import _build_tick_config

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
        from stardag.integration.modal._app import _build_tick_config

        config = _build_tick_config(None, None, None)
        assert config.linger_seconds == TickConfig().linger_seconds
