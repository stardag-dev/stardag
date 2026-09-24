"""Tests for StardagApp build_function and run_function customization."""

import asyncio
import contextlib
import importlib.util
import inspect
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

from unittest.mock import AsyncMock, MagicMock, call, patch
from uuid import uuid4

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


from tests.test_integration.test_modal._app_helpers import (  # noqa: F401
    _CONCURRENCY_REQUESTS,
    _UNCOVERING_PATTERN,
    _finalize_capturing_functions,
    _invoke,
    _make_image,
    _mock_secret_hydrate,
    _stub_modal_concurrent,
)


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
    def test_finalize_build_wrapper_imports_the_task_modules_first(self, mock_volumes):
        """The deployed ``build`` wrapper imports the declared task modules
        before the builder runs, exactly as the tick does, so the resident
        build's classes resolve the same way in every function."""
        mock_volumes.return_value = MagicMock(by_volume_name={}, by_root_key={})
        order: list = []

        def my_build(tasks, worker_selector, app_name, build_kwargs=None):
            order.append("build")

        app = StardagApp(
            "test-app",
            build_function=my_build,
            builder_settings=FunctionSettings(image=_make_image()),
            worker_settings={"default": FunctionSettings(image=_make_image())},
            task_modules=["stardag.testing.modal._tasks"],
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

        with patch(
            "stardag.integration.modal._functions.import_task_modules",
            side_effect=lambda modules: order.append(("import", tuple(modules))),
        ):
            registered_fns["build"]("task", "selector", "app", None)

        assert order == [("import", ("stardag.testing.modal._tasks",)), "build"]

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

    def test_settings_reach_the_engine_unchanged(self):
        """The resident path carries the build's settings in ``build_kwargs``
        to :func:`stardag.build`, which applies them for the whole build
        (workers get them from the executor's env layering); the builder
        itself neither reads nor rewrites them."""
        captured: dict = {}
        mock_summary = MagicMock()
        mock_summary.status = MagicMock(value="SUCCESS")

        class CapturingBuilder(Builder):
            def build(self, tasks, task_executor, build_kwargs=None):
                captured["build_kwargs"] = build_kwargs
                return mock_summary

        build_id = uuid4()
        kwargs = {"resume_build_id": build_id, "settings": {"MY_FLAG": "1"}}
        CapturingBuilder()(MagicMock(), MagicMock(), "app", build_kwargs=kwargs)
        assert captured["build_kwargs"] == kwargs

    def test_an_executor_that_cannot_be_built_fails_the_resumed_build(self):
        """Nothing but this function drives a trigger-created build, so a
        failure before the engine runs is recorded rather than leaving the
        build RUNNING with no driver."""
        from stardag.testing import InMemoryRegistry

        registry = InMemoryRegistry()
        build_id = registry.build_create(root_task_ids=["root"]).id
        with (
            registry_provider.override(registry),
            patch(
                "stardag.integration.modal._builder.ModalTaskExecutor",
                side_effect=RuntimeError("no executor"),
            ),
        ):
            with pytest.raises(RuntimeError, match="no executor"):
                Builder()(
                    MagicMock(),
                    MagicMock(),
                    "app",
                    build_kwargs={"resume_build_id": build_id},
                )
        assert registry.builds[build_id].status == "failed"
        assert "no executor" in (registry.builds[build_id].error_message or "")

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
        from stardag.testing import InMemoryRegistry
        from stardag.utils.testing.helper_tasks import SyncOnlyTask

        captured = modal_function_stub
        app = self._make_app()
        root = SyncOnlyTask(name="trigger-root")
        registry = InMemoryRegistry()

        with registry_provider.override(registry):
            result = app.build_trigger(root, description="a build")

        build = registry.builds[result.build_id]
        assert build.root_task_ids == [str(root.id)]
        assert build.description == "a build"
        assert build.executor_metadata == {
            "kind": "modal",
            "app_name": app.name,
            "function_name": "build",
            "reactive": False,
            "workspace": "test-workspace",
            "environment": "test-env",
        }
        # The id is client-minted (uuid7) and sent with the create.
        (create,) = registry.calls_to("build_create")
        assert create["build_id"] == result.build_id
        assert result.build_id.version == 7
        assert result.function_call == "spawn-handle"
        assert captured["op"] == "spawn"
        assert captured["kwargs"]["tasks"] is root
        assert captured["kwargs"]["build_kwargs"] == {
            "resume_build_id": result.build_id
        }

    def test_a_retrigger_resumes_the_build_and_names_no_scope(
        self, modal_function_stub
    ):
        """``build_trigger(build_id=...)`` un-terminals the build from the
        trigger; the scope (deployment, settings) is named by the driver
        when it plans, not here."""
        from stardag.testing import InMemoryRegistry
        from stardag.utils.testing.helper_tasks import SyncOnlyTask

        app = self._make_app()
        registry = InMemoryRegistry()
        root = SyncOnlyTask(name="retrigger-root")
        build_id = registry.build_create(root_task_ids=[str(root.id)]).id
        registry.build_fail(build_id, "earlier failure")

        with registry_provider.override(registry):
            app.build_trigger(root, build_id=build_id)

        assert registry.builds[build_id].status == "running"
        (resume,) = registry.calls_to("build_resume")
        assert resume["deployment_id"] is None
        assert len(registry.calls_to("build_create")) == 1
        # A bare re-trigger of a build with no plan yet: no settings.
        assert modal_function_stub["kwargs"]["build_kwargs"] == {
            "resume_build_id": build_id,
            "settings": {},
        }

    @pytest.mark.parametrize(
        "given,expected",
        [(None, {"MY_FLAG": "on"}), ({}, {}), ({"OTHER": "x"}, {"OTHER": "x"})],
        ids=["bare-reuses-stored", "explicit-empty", "explicit-other"],
    )
    def test_a_bare_retrigger_reuses_the_active_plans_settings(
        self, modal_function_stub, given, expected
    ):
        """Omitted settings on a re-trigger mean the build's own (its active
        plan's), as a bare re-trigger read the stored config in v1; only an
        explicit ``{}`` means none."""
        from datetime import datetime, timezone

        from stardag.build._registration import new_id, registration_item
        from stardag.testing import InMemoryRegistry
        from stardag.utils.testing.helper_tasks import SyncOnlyTask

        app = self._make_app()
        registry = InMemoryRegistry()
        root = SyncOnlyTask(name=f"stored-settings-{new_id()}")
        build_id = registry.build_create(root_task_ids=[str(root.id)]).id
        registry.plan_create(
            build_id,
            plan_id=new_id(),
            deployment_id=registry.add_deployment(app_name=app.name),
            settings={"MY_FLAG": "on"},
            roots=[
                registration_item(
                    root,
                    declared_upstreams=None,
                    observed_complete=False,
                    observed_at=datetime.now(timezone.utc),
                )
            ],
        )
        with registry_provider.override(registry):
            app.build_trigger(root, build_id=build_id, settings=given)
        assert modal_function_stub["kwargs"]["build_kwargs"] == {
            "resume_build_id": build_id,
            "settings": expected,
        }

    def test_settings_are_forwarded_to_the_builder(self, modal_function_stub):
        from stardag.testing import InMemoryRegistry
        from stardag.utils.testing.helper_tasks import SyncOnlyTask

        app = self._make_app()
        with registry_provider.override(InMemoryRegistry()):
            result = app.build_trigger(
                SyncOnlyTask(name="settings-root"), settings={"MY_FLAG": "on"}
            )
        assert modal_function_stub["kwargs"]["build_kwargs"] == {
            "resume_build_id": result.build_id,
            "settings": {"MY_FLAG": "on"},
        }

    @pytest.mark.parametrize(
        "settings", [{"STARDAG_PROFILE": "x"}, {"MODAL_PROFILE": "x"}, {"A": 1}]
    )
    def test_invalid_settings_fail_before_a_build_is_minted(
        self, modal_function_stub, settings
    ):
        """Settings are validated at the trigger, synchronously, with no
        build created: ``STARDAG_*`` / ``MODAL_*`` keys are the framework's
        own, and values are strings."""
        from stardag.build import SettingsError
        from stardag.testing import InMemoryRegistry
        from stardag.utils.testing.helper_tasks import SyncOnlyTask

        app = self._make_app()
        registry = InMemoryRegistry()
        with registry_provider.override(registry):
            with pytest.raises(SettingsError):
                app.build_trigger(SyncOnlyTask(name="bad-settings"), settings=settings)
        assert not registry.called("build_create")
        assert "op" not in modal_function_stub

    def test_rejects_settings_in_build_kwargs(self, modal_function_stub):
        app = self._make_app()
        with pytest.raises(TypeError, match="settings"):
            app.build_trigger(
                MagicMock(spec=BaseTask),
                build_id=uuid4(),
                build_kwargs={"settings": {"A": "1"}},
            )

    def test_sequence_of_roots_passed_as_list_to_registry(self, modal_function_stub):
        from stardag.testing import InMemoryRegistry
        from stardag.utils.testing.helper_tasks import SyncOnlyTask

        app = self._make_app()
        roots = [SyncOnlyTask(name="root-a"), SyncOnlyTask(name="root-b")]
        registry = InMemoryRegistry()

        with registry_provider.override(registry):
            result = app.build_trigger(roots)

        assert registry.builds[result.build_id].root_task_ids == sorted(
            str(r.id) for r in roots
        )
        assert registry.builds[result.build_id].description is None

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
# task_modules: declaration, deploy-time expansion, pre-flight, pickle elision
# ---------------------------------------------------------------------------


def _app_with_task_modules(name: str, **kwargs) -> StardagApp:
    # Defined here, not in _app_helpers: task_modules inference reads the
    # module that constructs the app.
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
        # Names the consequence — the app is resident-only — and the fix.
        assert "can only run RESIDENT builds" in caplog.text
        assert 'task_modules=["my_pkg.tasks.*"]' in caplog.text


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
        registry.build_get_aio = AsyncMock(
            return_value=BuildInfo(
                id=build_id,
                reactive_app_name="tm-finalize",
                reactive_tick_kwargs=None,
            )
        )

        async def stub_tick_aio(build_uuid, **kwargs):
            return TickSummary(outcome="noop")

        with (
            patch("stardag.integration.modal._tick.run_tick_aio", stub_tick_aio),
            patch("stardag.integration.modal._tick.registry_provider") as rp,
            patch(
                "stardag.integration.modal._tick.import_task_modules"
            ) as import_modules,
        ):
            rp.get.return_value = registry
            _invoke(captured["tick"], str(build_id))

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
        # `build_get_aio`, not `build_get` — and it has to be stubbed
        # explicitly. `MagicMock(spec=...)` auto-specs an async member as an
        # AsyncMock, so leaving it alone does not raise: it returns a mock
        # whose `reactive_app_name` is a mock, the tick decides the build
        # belongs to another app, and the assertion below then passes
        # because the tick never got as far as importing anything.
        registry.build_get_aio = AsyncMock(
            return_value=BuildInfo(
                id=build_id,
                reactive_app_name="tm-finalize",
                reactive_tick_kwargs=None,
            )
        )

        async def stub_tick_aio(build_uuid, **kwargs):
            return TickSummary(outcome="noop")

        with (
            patch("stardag.integration.modal._tick.run_tick_aio", stub_tick_aio),
            patch("stardag.integration.modal._tick.registry_provider") as rp,
            patch(
                "stardag.integration.modal._tick.import_task_modules"
            ) as import_modules,
        ):
            rp.get.return_value = registry
            _invoke(captured["tick"], str(build_id))

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


class TestRequirePickleFreeIsDeprecated:
    """The flag asked for what is now the only behaviour.

    Kept as an accepted no-op rather than removed, so an app definition
    written against an older SDK keeps deploying instead of failing at
    import with a ``TypeError``.
    """

    def test_passing_it_warns_and_changes_nothing(self):
        from stardag.utils.testing.helper_tasks import SyncOnlyTask

        with pytest.warns(DeprecationWarning, match="require_pickle_free"):
            app = _app_with_task_modules(
                "tm-deprecated-flag",
                task_modules=[SyncOnlyTask.__module__],
                require_pickle_free=True,
            )

        assert app.task_modules == (SyncOnlyTask.__module__,)
        assert not hasattr(app, "require_pickle_free")

    def test_it_no_longer_contradicts_an_opted_out_app(self):
        """It used to raise here — "meaningless without task_modules". The
        contradiction is gone with the flag's meaning: the app is simply
        resident-only, which its reactive trigger says."""
        with pytest.warns(DeprecationWarning):
            app = _app_with_task_modules(
                "tm-deprecated-optout", task_modules=[], require_pickle_free=True
            )
        assert app.task_modules == ()

    def test_not_passing_it_warns_nothing(self, recwarn):
        from stardag.utils.testing.helper_tasks import SyncOnlyTask

        _app_with_task_modules("tm-no-flag", task_modules=[SyncOnlyTask.__module__])
        assert [
            w
            for w in recwarn
            if issubclass(w.category, DeprecationWarning)
            and "require_pickle_free" in str(w.message)
        ] == []


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
        registry.build_get_aio = AsyncMock(
            return_value=BuildInfo(
                id=build_id,
                reactive_app_name="another-app",
                reactive_tick_kwargs=None,
            )
        )
        with (
            patch("stardag.integration.modal._tick.registry_provider") as rp,
            patch("modal.Function.from_name", side_effect=Exception("no such app")),
        ):
            rp.get.return_value = registry
            result = _invoke(tick, str(build_id))

        # It compared the build's owner against the app name it was
        # deployed with, which means that capture survived the round trip.
        assert result == {
            "outcome": "foreign_app",
            "owner_app": "another-app",
            "forwarded": False,
        }


class TestTickAppOwnership:
    """Only the app recorded as the build's reactive_app_name (in the
    registry) may drive its ticks — read via the lighter build_get_aio."""

    def _capture_tick(self, app_name: str, **app_kwargs):
        app = StardagApp(
            app_name,
            builder_settings=FunctionSettings(image=MagicMock()),
            worker_settings={"default": FunctionSettings(image=MagicMock())},
            **app_kwargs,
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
        """A registry whose build_get_aio returns the given reactive marker.

        ``reactive_app_name=None`` models a non-reactive build.
        """
        from stardag.registry import BuildInfo

        registry = MagicMock(spec=RegistryABC)
        registry.build_get_aio = AsyncMock(
            return_value=BuildInfo(
                id=build_id,
                reactive_app_name=reactive_app_name,
                reactive_tick_kwargs=tick_kwargs,
            )
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
        ):
            rp.get.return_value = registry
            _invoke(tick, str(build_id))

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
        with its own commit and its own task modules, against a build the
        owner's code planned — but it forwards the wake-up to the owner's
        tick, so e.g. a
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
            result = _invoke(tick, str(build_id))

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
            result = _invoke(tick, str(build_id))

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
            result = _invoke(tick, str(build_id))

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
        ):
            rp.get.return_value = own_registry
            assert _invoke(tick, str(own))["outcome"] == "noop"
        assert ticked == [str(own)]


class TestWatchdogSweep:
    """The sweep dispatches: one spawned tick per build, then it returns.

    It used to run every build's tick body sequentially inside the *sweep's*
    single container, which is why it had to force ``linger_seconds=0`` and
    hand each build a fraction of that container's timeout. Both overrides
    are gone with the inline run.
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

    def test_the_default_spawner_is_the_deployed_tick(self):
        """The one path production actually takes.

        Every other test here injects ``spawn=``, which leaves
        ``spawn = spawn or _spawn_tick`` — the line that decides what really
        happens — unexercised. This calls the sweep with no spawner at all.

        It also pins the three things that would silently break: the build
        id goes out as a ``UUID`` (``build_list_running`` returns UUIDs and
        ``spawn_tick`` does its own ``str()``, so an over-eager ``str()``
        here would double-encode); the app name is the spawn *target* rather
        than only the listing scope; and the sweep asks for **no linger**.

        That last one is the whole cost model. Without it each swept build
        holds a container for its own ``linger_seconds`` — 120 s by default
        — every watchdog period, and it spends that on the builds least
        likely to have anything to do, since a sweep's population is builds
        where nothing is known to have happened.
        """
        from stardag.integration.modal._tick import _run_watchdog_sweep

        build_ids = [uuid4(), uuid4()]

        with patch("stardag.integration.modal._tick._spawn_tick") as spawn:
            _run_watchdog_sweep(self._registry(build_ids), "an-app")

        assert spawn.call_args_list == [
            call(build_ids[0], "an-app", tick_kwargs={"linger_seconds": 0}),
            call(build_ids[1], "an-app", tick_kwargs={"linger_seconds": 0}),
        ]

    def test_a_wake_up_spawn_carries_no_overrides(self):
        """The other side of the same coin: everything that is *not* the
        sweep must leave the build's stored config alone.

        A wake-up means something changed and more is likely to, so its tick
        should linger — reconfiguring somebody else's scheduler by accident
        is what the default-``None`` argument exists to prevent.
        """
        import inspect

        from stardag.integration.modal._spawn import spawn_tick

        assert inspect.signature(spawn_tick).parameters["tick_kwargs"].default is None

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
                patch(
                    "stardag.integration.modal._functions._run_deployed_tick_aio",
                    new_callable=AsyncMock,
                )
            )
            bootstrap = stack.enter_context(
                patch("stardag.integration.modal._functions.run_reactive_bootstrap")
            )
            stack.enter_context(
                patch("stardag.integration.modal._functions._run_watchdog_sweep")
            )
            tick.return_value = {}
            bootstrap.return_value = MagicMock(summary={})
            yield

    # One invocation of each registered function, with dummy arguments.
    _INVOCATIONS: dict[str, typing.Callable[[typing.Any], typing.Any]] = {
        "build": lambda fn: fn("task", "selector", "test-app", None),
        "worker_default": lambda fn: fn("task"),
        "worker_gpu": lambda fn: fn("task"),
        "tick": lambda fn: _invoke(fn, str(uuid4()), None),
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
            _invoke(registered["tick"], str(uuid4()), None)

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
                "stardag.integration.modal._functions._setup_logging",
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
            _invoke(registered["tick"], str(uuid4()), None)

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
        ``Builder.setup`` / ``Runner.setup`` / the tick body, and therefore
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
                patch(
                    "stardag.integration.modal._functions._run_deployed_tick_aio",
                    new_callable=AsyncMock,
                )
            )
            bootstrap = stack.enter_context(
                patch("stardag.integration.modal._functions.run_reactive_bootstrap")
            )
            sweep = stack.enter_context(
                patch("stardag.integration.modal._functions._run_watchdog_sweep")
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

    def test_watchdog_hands_the_sweep_an_app_name_not_a_callable(self):
        """What the watchdog wrapper passes, and what it no longer passes.

        The sweep used to be handed the tick wrapper and call it in-process
        for each build, which is why a re-entrancy test guarded this pair.
        (That re-entry was in fact harmless — the outer
        ``_run_container_setup`` had already completed and released its lock
        before the sweep ran, so the inner call short-circuited on the
        already-run check rather than blocking. The hazard was hypothetical,
        and it is now absent rather than merely unreached.)

        What is worth pinning is the call shape: an app name, no kwargs, and
        no tick body in this container.
        """
        calls = []
        app = self._app(lambda: calls.append("setup"))
        registered = _finalize_capturing_functions(app)

        with contextlib.ExitStack() as stack:
            tick = stack.enter_context(
                patch(
                    "stardag.integration.modal._functions._run_deployed_tick_aio",
                    new_callable=AsyncMock,
                )
            )
            sweep = stack.enter_context(
                patch("stardag.integration.modal._functions._run_watchdog_sweep")
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


class TestInputConcurrency:
    """Which deployed functions may serve several inputs per container.

    The tick and nothing else. It is almost entirely I/O wait — read the
    frontier, spawn, then poll on a sleep until its linger deadline — so a
    container per tick is close to the worst packing available, and the
    linger that makes reactive scheduling efficient is what keeps those
    containers alive. Nothing else in the app has that shape.

    The two halves are one decision: Modal serves concurrent inputs to an
    ``async def`` as tasks on one event loop and to a ``def`` on threads,
    and threaded ticks would thrash the process-wide registry's per-loop
    HTTP client (see ``TestAsyncClientUnderConcurrentCallers`` in the
    registry tests). So "async" and "concurrent" are asserted together.
    """

    @staticmethod
    def _app(**app_kwargs) -> StardagApp:
        return StardagApp(
            "test-concurrency",
            builder_settings=FunctionSettings(image=_make_image()),
            worker_settings={
                "default": FunctionSettings(image=_make_image()),
                "gpu": FunctionSettings(image=_make_image()),
            },
            watchdog_period_minutes=5,
            **app_kwargs,
        )

    def _concurrency(self, app) -> dict:
        """What ``@modal.concurrent`` was asked for, by function name."""
        registered = _finalize_capturing_functions(app)
        return {name: _CONCURRENCY_REQUESTS.get(fn) for name, fn in registered.items()}

    def test_the_deployed_tick_is_a_coroutine_function(self):
        """Load-bearing, not a style choice: a sync tick would be served
        on threads, each with its own ``asyncio.run`` and therefore its own
        event loop, and the shared ``APIRegistry`` closes and rebuilds its
        async client whenever the running loop changes."""
        registered = _finalize_capturing_functions(self._app())
        assert inspect.iscoroutinefunction(registered["tick"])

    def test_every_other_registered_function_is_sync(self):
        """The tick is the exception, and it should stay legible as one."""
        registered = _finalize_capturing_functions(self._app())
        assert [
            name for name, fn in registered.items() if inspect.iscoroutinefunction(fn)
        ] == ["tick"]

    def test_the_tick_gets_a_default(self):
        assert self._concurrency(self._app())["tick"] == {"max_inputs": 10}

    def test_nothing_else_does(self):
        """Workers run user code and may be CPU- or GPU-bound; the builder
        runs a whole build; the bootstrap walks a DAG. And the watchdog —
        which shares ``tick_settings`` — is a separate function with its own
        containers that receives one input per period, so packing it would
        change nothing while quietly opting a ``def`` into threading."""
        concurrency = self._concurrency(self._app())
        assert concurrency.pop("tick") is not None
        assert set(concurrency) == {
            "build",
            "worker_default",
            "worker_gpu",
            "bootstrap",
            "tick_watchdog",
        }
        assert all(value is None for value in concurrency.values())

    def test_tick_settings_override_the_default(self):
        concurrency = self._concurrency(
            self._app(
                tick_settings=FunctionSettings(
                    image=_make_image(),
                    max_concurrent_inputs=32,
                    target_concurrent_inputs=24,
                )
            )
        )
        assert concurrency["tick"] == {"max_inputs": 32, "target_inputs": 24}

    def test_a_target_without_a_max_fails_the_deploy_here(self):
        """Not by merging stardag's own ceiling underneath — that would
        invent a limit nobody asked for, and could still sit below the
        target. Modal refuses the function either way; this refuses it
        first, in the setting names the app actually wrote."""
        from stardag.exceptions import StardagError

        with pytest.raises(StardagError, match="without max_concurrent_inputs"):
            self._concurrency(
                self._app(
                    tick_settings=FunctionSettings(
                        image=_make_image(), target_concurrent_inputs=4
                    )
                )
            )

    def test_the_legacy_spelling_also_overrides_it(self):
        """``allow_concurrent_inputs`` is the name someone would reach for
        from Modal's pre-1.0 docs, and it never worked here — it raised."""
        concurrency = self._concurrency(
            self._app(
                tick_settings=FunctionSettings(
                    image=_make_image(), allow_concurrent_inputs=3
                )
            )
        )
        assert concurrency["tick"] == {"max_inputs": 3}

    def test_a_packed_tick_does_not_drag_the_watchdog_with_it(self):
        """``tick`` and ``tick_watchdog`` are registered from one
        ``tick_settings``, so "no default for the watchdog" is not enough —
        a declared value would reach it too. The watchdog is sync, so that
        is Modal's *threaded* concurrency, which is the whole hazard the
        tick is a coroutine to avoid; and Modal accepts a sync ``def`` with
        ``@modal.concurrent`` and a ``schedule`` without complaint, so
        nothing downstream would catch it."""
        concurrency = self._concurrency(
            self._app(
                tick_settings=FunctionSettings(
                    image=_make_image(), max_concurrent_inputs=32
                )
            )
        )
        assert concurrency["tick"] == {"max_inputs": 32}
        assert concurrency["tick_watchdog"] is None

    def test_a_worker_may_opt_in_explicitly(self):
        """Stardag has no opinion for workers; an app that knows its own
        run function is I/O-bound is entitled to one."""
        app = StardagApp(
            "test-concurrency",
            builder_settings=FunctionSettings(image=_make_image()),
            worker_settings={
                "default": FunctionSettings(
                    image=_make_image(), max_concurrent_inputs=5
                )
            },
        )
        assert self._concurrency(app)["worker_default"] == {"max_inputs": 5}


class TestConcurrentTicksInOneContainer:
    """Two ticks genuinely overlapping in one process, which is the only
    condition under which any of this is observable.

    ``TestInputConcurrency`` pins that the tick is async and asks for
    concurrency; this pins that two of them actually running at once stay
    out of each other's way. Every hazard here is silent until it happens:
    a shared registry singleton, a shared HTTP client, a ``ContextVar``
    holding "the build this code is running for".
    """

    @staticmethod
    def _tick_wrapper(app_name: str = "shared-container"):
        app = StardagApp(
            app_name,
            builder_settings=FunctionSettings(image=_make_image()),
            worker_settings={"default": FunctionSettings(image=_make_image())},
        )
        return _finalize_capturing_functions(app)["tick"]

    @staticmethod
    def _registry(app_name: str = "shared-container"):
        from stardag.registry import BuildInfo

        registry = MagicMock(spec=RegistryABC)

        async def build_get_aio(build_id):
            # A round trip, so the two ticks interleave here as well as in
            # the body — the pre-lease read is the tick's first await.
            await asyncio.sleep(0)
            return BuildInfo(
                id=build_id,
                reactive_app_name=app_name,
                reactive_tick_kwargs=None,
            )

        registry.build_get_aio = build_get_aio
        return registry

    def test_overlapping_ticks_each_drive_their_own_build(
        self, default_in_memory_fs_target
    ):
        """The bodies are held at a barrier, so neither can finish before
        the other has started: whatever they share, they share it live."""
        from stardag.build import TickSummary

        tick = self._tick_wrapper()
        build_ids = [uuid4(), uuid4()]
        # `asyncio.Barrier` is 3.11+, which the engine already requires
        # (`_reactive/_discovery.py` uses `asyncio.TaskGroup` and
        # `BaseExceptionGroup`) — CI runs 3.11-3.14.
        barrier = asyncio.Barrier(len(build_ids))
        driven: list[str] = []

        async def stub_tick_aio(build_uuid, **kwargs):
            await barrier.wait()
            driven.append(str(build_uuid))
            return TickSummary(outcome="lingered_out", iterations=1)

        async def main():
            return await asyncio.gather(
                *(tick(str(build_id)) for build_id in build_ids)
            )

        with (
            patch("stardag.integration.modal._tick.run_tick_aio", stub_tick_aio),
            patch("stardag.integration.modal._tick.registry_provider") as rp,
        ):
            rp.get.return_value = self._registry()
            summaries = asyncio.run(main())

        assert sorted(driven) == sorted(str(b) for b in build_ids)
        assert [summary["outcome"] for summary in summaries] == [
            "lingered_out",
            "lingered_out",
        ]

    def test_the_build_context_var_stays_per_tick(self, default_in_memory_fs_target):
        """``current_build_context_var`` is how an executor learns which
        build (and plan, and settings) it is spawning for. Asyncio tasks copy context, so a set
        inside one tick is invisible to the other — but only as long as
        each input really is its own task, which is the assumption the
        whole design rides on. Held at a barrier so the two sets are live
        at the same moment.
        """
        from stardag.build import TickSummary
        from stardag.build._base import BuildContext, current_build_context_var

        tick = self._tick_wrapper()
        build_ids = [uuid4(), uuid4()]
        barrier = asyncio.Barrier(len(build_ids))
        observed: dict[str, object] = {}

        async def stub_tick_aio(build_uuid, **kwargs):
            token = current_build_context_var.set(BuildContext(build_id=build_uuid))
            try:
                await barrier.wait()
                context = current_build_context_var.get()
                observed[str(build_uuid)] = context and context.build_id
                return TickSummary(outcome="lingered_out")
            finally:
                current_build_context_var.reset(token)

        async def main():
            await asyncio.gather(*(tick(str(b)) for b in build_ids))

        with (
            patch("stardag.integration.modal._tick.run_tick_aio", stub_tick_aio),
            patch("stardag.integration.modal._tick.registry_provider") as rp,
        ):
            rp.get.return_value = self._registry()
            asyncio.run(main())

        assert observed == {str(b): b for b in build_ids}
        assert current_build_context_var.get() is None

    def test_a_foreign_app_forward_does_not_block_the_other_tick(
        self, default_in_memory_fs_target, modal_function_stub
    ):
        """The forward is a blocking Modal RPC. It has to leave the event
        loop, or one tick that lands on the wrong app stalls every tick
        beside it for the length of a backend round trip."""
        tick = self._tick_wrapper("app-b")
        forwarding_build, own_build = uuid4(), uuid4()
        released = threading.Event()
        progressed: list[str] = []
        spawn_saw_progress: list[bool] = []

        def slow_spawn(build_id, app_name, tick_kwargs=None):
            # Blocks until the *other* tick has run. Off the loop that
            # happens; on the loop nothing else can run while this call is
            # on the stack, so the wait times out and the assertion below
            # is what tells you the spawn went back inline.
            spawn_saw_progress.append(released.wait(timeout=2))

        async def stub_tick_aio(build_uuid, **kwargs):
            from stardag.build import TickSummary

            progressed.append(str(build_uuid))
            released.set()
            return TickSummary(outcome="lingered_out")

        registry = MagicMock(spec=RegistryABC)

        async def build_get_aio(build_id):
            from stardag.registry import BuildInfo

            return BuildInfo(
                id=build_id,
                reactive_app_name="app-a" if build_id == forwarding_build else "app-b",
                reactive_tick_kwargs=None,
            )

        registry.build_get_aio = build_get_aio

        async def main():
            return await asyncio.gather(
                tick(str(forwarding_build)), tick(str(own_build))
            )

        with (
            patch("stardag.integration.modal._tick.run_tick_aio", stub_tick_aio),
            patch("stardag.integration.modal._tick._spawn_tick", slow_spawn),
            patch("stardag.integration.modal._tick.registry_provider") as rp,
        ):
            rp.get.return_value = registry
            forwarded, own = asyncio.run(main())

        assert forwarded["outcome"] == "foreign_app"
        assert forwarded["forwarded"] is True
        assert progressed == [str(own_build)]
        assert spawn_saw_progress == [True], (
            "the other tick made no progress while the forward's blocking "
            "spawn was in flight — it is running on the event loop"
        )


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
