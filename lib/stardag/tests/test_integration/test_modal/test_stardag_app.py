"""Tests for StardagApp build_function and run_function customization."""

import pytest

try:
    import modal  # noqa: F401
except ImportError:
    pytest.skip("Skipping modal tests (import not available)", allow_module_level=True)

from unittest.mock import MagicMock, patch

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

        def my_build(tasks, worker_selector, app_name) -> BuildSummary:  # type: ignore[empty-body]
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

        def my_build(tasks, worker_selector, app_name) -> BuildSummary:  # type: ignore[empty-body]
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
        registered_fns["build"]("task", "selector", "app")
        assert calls == [("build", "task", "app")]

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

        def my_build(tasks, worker_selector, app_name) -> BuildSummary:  # type: ignore[empty-body]
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
# _ModalCallable: __qualname__/__name__ robustness
# ---------------------------------------------------------------------------


class TestModalCallableSubclassing:
    def test_builder_has_qualname(self):
        b = Builder()
        assert b.__qualname__ == "Builder"
        assert b.__name__ == "Builder"

    def test_runner_has_qualname(self):
        r = Runner()
        assert r.__qualname__ == "Runner"
        assert r.__name__ == "Runner"

    def test_subclass_without_init(self):
        class MyBuilder(Builder):
            def setup(self, tasks):
                pass

        b = MyBuilder()
        assert b.__name__ == "MyBuilder"
        # __qualname__ includes enclosing scope for locally-defined classes
        assert "MyBuilder" in b.__qualname__

    def test_subclass_with_init_calling_super(self):
        class MyBuilder(Builder):
            def __init__(self, x):
                super().__init__()
                self.x = x

        b = MyBuilder(42)
        assert b.__name__ == "MyBuilder"
        assert "MyBuilder" in b.__qualname__
        assert b.x == 42

    def test_subclass_with_init_not_calling_super(self):
        """The tricky case: __init_subclass__ ensures attrs are set anyway."""

        class MyBuilder(Builder):
            def __init__(self, config):
                self.config = config  # no super().__init__()

        b = MyBuilder("prod")
        assert b.__name__ == "MyBuilder"
        assert b.config == "prod"

    def test_runner_subclass_without_super(self):
        class MyRunner(Runner):
            def __init__(self, gpu_id):
                self.gpu_id = gpu_id

        r = MyRunner(0)
        assert r.__name__ == "MyRunner"
        assert r.gpu_id == 0


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

            def build(self, tasks, task_executor):
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

            def build(self, tasks, task_executor):
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

            def build(self, tasks, task_executor):
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
