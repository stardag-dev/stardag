"""Test Modal app factory.

Creates a StardagApp configured for integration testing with the
``stardag-testing`` Modal volume and local stardag source.
"""

from __future__ import annotations

import sys
from pathlib import Path

import modal

from stardag.integration.modal import FunctionSettings, StardagApp
from stardag.integration.modal._app import FinalizeResult
from stardag.integration.modal._config import get_package_deps, with_stardag_on_image

TEST_APP_NAME = "stardag-testing-app"
VOLUME_NAME = "stardag-testing"
ROOT_DEFAULT = "stardag/root/default"


def _get_test_deps() -> list[str]:
    """Get pip dependencies for the test Modal image."""
    try:
        pyproject_path = Path(__file__).parents[4] / "pyproject.toml"
        if not pyproject_path.exists():
            return []
        return get_package_deps(
            pyproject_path=pyproject_path,
            groups=["dev"],
            optional=["modal"],
        )
    except (IndexError, FileNotFoundError):
        return []


def _get_test_image() -> modal.Image:
    """Build a Modal image with stardag installed from local source."""
    python_version = f"{sys.version_info.major}.{sys.version_info.minor}"
    return with_stardag_on_image(
        modal.Image.debian_slim(python_version=python_version).pip_install(
            *_get_test_deps()
        )
    )


def create_test_app() -> tuple[StardagApp, FinalizeResult]:
    """Create and finalize a StardagApp for integration testing.

    Returns:
        (stardag_app, finalize_result) tuple. The app is finalized and
        ready for ``modal deploy``.
    """
    image = _get_test_image()

    stardag_app = StardagApp(
        TEST_APP_NAME,
        builder_settings=FunctionSettings(image=image),
        worker_settings={"default": FunctionSettings(image=image)},
    )

    finalize_result = stardag_app.finalize()
    return stardag_app, finalize_result
