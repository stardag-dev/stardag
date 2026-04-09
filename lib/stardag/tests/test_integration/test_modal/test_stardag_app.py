"""Tests for StardagApp build_function and run_function customization."""

import pytest

try:
    import modal  # noqa: F401
except ImportError:
    pytest.skip("Skipping modal tests (import not available)", allow_module_level=True)

from unittest.mock import MagicMock, patch

from stardag.integration.modal import (
    BuildFunction,
    FunctionSettings,
    RunFunction,
    StardagApp,
    default_build,
    default_run,
)


def _make_image() -> modal.Image:
    return modal.Image.debian_slim()


class TestStardagAppCustomFunctions:
    def test_default_functions_when_none_provided(self):
        """When no custom functions are given, defaults are used."""
        app = StardagApp(
            "test-app",
            builder_settings=FunctionSettings(image=_make_image()),
            worker_settings={"default": FunctionSettings(image=_make_image())},
        )
        # The resolved functions are checked in finalize(), but we can verify
        # the stored values
        assert app._build_function is None
        assert app._run_function is None

    def test_custom_build_function(self):
        """Custom build_function is stored and used."""

        def my_build(task, worker_selector, modal_app_name):
            pass  # custom logic

        app = StardagApp(
            "test-app",
            build_function=my_build,
            builder_settings=FunctionSettings(image=_make_image()),
            worker_settings={"default": FunctionSettings(image=_make_image())},
        )
        assert app._build_function is my_build

    def test_custom_run_function(self):
        """Custom run_function is stored and used."""

        def my_run(task):
            pass  # custom logic

        app = StardagApp(
            "test-app",
            run_function=my_run,
            builder_settings=FunctionSettings(image=_make_image()),
            worker_settings={"default": FunctionSettings(image=_make_image())},
        )
        assert app._run_function is my_run

    def test_build_function_overrides_builder_type(self):
        """Explicit build_function takes precedence over builder_type."""

        def my_build(task, worker_selector, modal_app_name):
            pass

        app = StardagApp(
            "test-app",
            builder_type="prefect",
            build_function=my_build,
            builder_settings=FunctionSettings(image=_make_image()),
            worker_settings={"default": FunctionSettings(image=_make_image())},
        )
        # build_function should win over builder_type="prefect"
        assert app._build_function is my_build

    @patch("stardag.integration.modal._app.get_target_roots_volumes")
    def test_finalize_uses_custom_build_function(self, mock_volumes):
        """finalize() registers the custom build function with Modal."""
        mock_volumes.return_value = MagicMock(by_volume_name={}, by_root_key={})

        calls = []

        def my_build(task, worker_selector, modal_app_name):
            calls.append("build")

        app = StardagApp(
            "test-app",
            build_function=my_build,
            builder_settings=FunctionSettings(image=_make_image()),
            worker_settings={"default": FunctionSettings(image=_make_image())},
        )

        # Mock modal_app.function to capture what gets registered
        registered_fns = {}

        def capture_function(**kwargs):
            name = kwargs.get("name", "unknown")

            def decorator(fn):
                registered_fns[name] = fn
                return fn

            return decorator

        app.modal_app.function = capture_function  # type: ignore[assignment]
        app.finalize()

        assert registered_fns["build"] is my_build

    @patch("stardag.integration.modal._app.get_target_roots_volumes")
    def test_finalize_uses_custom_run_function(self, mock_volumes):
        """finalize() registers the custom run function for all workers."""
        mock_volumes.return_value = MagicMock(by_volume_name={}, by_root_key={})

        def my_run(task):
            pass

        app = StardagApp(
            "test-app",
            run_function=my_run,
            builder_settings=FunctionSettings(image=_make_image()),
            worker_settings={
                "default": FunctionSettings(image=_make_image()),
                "gpu": FunctionSettings(image=_make_image()),
            },
        )

        registered_fns = {}

        def capture_function(**kwargs):
            name = kwargs.get("name", "unknown")

            def decorator(fn):
                registered_fns[name] = fn
                return fn

            return decorator

        app.modal_app.function = capture_function  # type: ignore[assignment]
        app.finalize()

        assert registered_fns["worker_default"] is my_run
        assert registered_fns["worker_gpu"] is my_run

    @patch("stardag.integration.modal._app.get_target_roots_volumes")
    def test_finalize_uses_defaults_when_no_custom(self, mock_volumes):
        """finalize() uses default_build and default_run when no custom fns."""
        mock_volumes.return_value = MagicMock(by_volume_name={}, by_root_key={})

        app = StardagApp(
            "test-app",
            builder_settings=FunctionSettings(image=_make_image()),
            worker_settings={"default": FunctionSettings(image=_make_image())},
        )

        registered_fns = {}

        def capture_function(**kwargs):
            name = kwargs.get("name", "unknown")

            def decorator(fn):
                registered_fns[name] = fn
                return fn

            return decorator

        app.modal_app.function = capture_function  # type: ignore[assignment]
        app.finalize()

        assert registered_fns["build"] is default_build
        assert registered_fns["worker_default"] is default_run


class TestBuildAndRunProtocols:
    def test_default_build_matches_protocol(self):
        """default_build is a valid BuildFunction."""
        fn: BuildFunction = default_build
        assert callable(fn)

    def test_default_run_matches_protocol(self):
        """default_run is a valid RunFunction."""
        fn: RunFunction = default_run
        assert callable(fn)

    def test_custom_functions_match_protocols(self):
        """Custom functions with matching signatures satisfy the protocols."""

        def my_build(
            task,  # noqa: ANN001
            worker_selector,  # noqa: ANN001
            modal_app_name,  # noqa: ANN001
        ) -> None:
            pass

        def my_run(task) -> None:  # noqa: ANN001
            pass

        # These assignments should not raise type errors
        build_fn: BuildFunction = my_build
        run_fn: RunFunction = my_run
        assert callable(build_fn)
        assert callable(run_fn)
