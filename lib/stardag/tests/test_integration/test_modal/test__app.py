"""StardagApp.finalize() auto-mount integration tests.

These tests verify that finalize() auto-discovers Modal volumes from target
roots and mounts them at predefined paths, so that ModalMountedVolumeTarget
(local I/O) is used instead of ModalVolumeRemoteFileSystem (API-based).

The remote tests require a deployed Modal app. The app is automatically
deployed before tests run via the `ensure_app_deployed` fixture.

To manually deploy:
    modal deploy tests/test_integration/test_modal/test__app.py
"""

import json
import os
import subprocess
from pathlib import Path
from typing import cast

import pytest

VOLUME_NAME = "stardag-testing"
ROOT_DEFAULT = "stardag/root/default"

try:
    import modal
    from modal.exception import AuthError

    from stardag.integration.modal import (
        VOLUME_MOUNT_PATH_PREFIX,
        FunctionSettings,
        StardagApp,
        get_default_volume_mount_path,
    )

    # check if logged in and volume exists
    try:
        _volume_check = modal.Volume.from_name(VOLUME_NAME)
        _volume_check.listdir("/")
    except AuthError:
        pytest.skip("Skipping modal tests (not authenticated)", allow_module_level=True)

except ImportError:
    pytest.skip("Skipping modal tests (import not available)", allow_module_level=True)


# --- Module-level app setup (also used as Modal deploy entrypoint) ---

os.environ.setdefault(
    "STARDAG_TARGET_ROOTS__DEFAULT",
    f"modalvol://{VOLUME_NAME}/{ROOT_DEFAULT}",
)


def _get_deps_for_modal_image() -> list[str]:
    try:
        pyproject_path = Path(__file__).parents[3] / "pyproject.toml"
        if not pyproject_path.exists():
            return []
        from stardag.integration.modal._config import get_package_deps

        return get_package_deps(
            pyproject_path=pyproject_path,
            groups=["dev"],
            optional=["modal"],
        )
    except (IndexError, FileNotFoundError):
        return []


TEST_IMAGE = modal.Image.debian_slim(python_version="3.12").pip_install(
    *_get_deps_for_modal_image()
)

stardag_app = StardagApp(
    "stardag-testing-app",
    builder_settings=FunctionSettings(image=TEST_IMAGE),
    worker_settings={"default": FunctionSettings(image=TEST_IMAGE)},
)

finalize_result = stardag_app.finalize()

# Expose modal app at module level for `modal deploy` to discover
app = stardag_app.modal_app

# Expected auto-mount path
EXPECTED_MOUNT_PATH = str(get_default_volume_mount_path(VOLUME_NAME))


# --- Probe function: runs remotely on Modal to check mount state ---


@stardag_app.modal_app.function(
    image=TEST_IMAGE.add_local_python_source("stardag"),
    volumes=cast(dict, finalize_result.auto_volumes),
    secrets=[
        modal.Secret.from_dict(
            {
                "STARDAG_TARGET_ROOTS__DEFAULT": (
                    f"modalvol://{VOLUME_NAME}/{ROOT_DEFAULT}"
                ),
                "STARDAG_MODAL_VOLUME_MOUNTS": json.dumps(
                    finalize_result.volume_mounts
                ),
            }
        )
    ],
)
def probe_auto_mount():
    """Probe function that runs inside Modal to check auto-mount state."""
    import os
    from pathlib import Path

    mount_path = os.environ.get("STARDAG_MODAL_VOLUME_MOUNTS", "{}")
    from stardag.integration.modal import get_modal_target

    target = get_modal_target(f"modalvol://{VOLUME_NAME}/probe-test.txt")
    target_type = type(target).__name__
    predefined_path_exists = Path(EXPECTED_MOUNT_PATH).is_dir()

    return {
        "target_type": target_type,
        "predefined_path_exists": predefined_path_exists,
        "volume_mounts_env": mount_path,
    }


# --- Deploy fixture ---

TEST_APP_NAME = "stardag-testing-app"


@pytest.fixture(scope="module", autouse=True)
def ensure_app_deployed():
    """Deploy the test Modal app before tests run."""
    result = subprocess.run(
        ["modal", "deploy", __file__],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.fail(f"Failed to deploy Modal app:\n{result.stderr}\n{result.stdout}")
    yield


# --- Tests ---


class TestFinalizeProducesVolumeMounts:
    """Test that finalize() produces correct volume_mounts and auto_volumes."""

    def test_volume_mounts_contains_expected_volume(self):
        assert EXPECTED_MOUNT_PATH in finalize_result.volume_mounts
        assert finalize_result.volume_mounts[EXPECTED_MOUNT_PATH] == VOLUME_NAME

    def test_auto_volumes_contains_expected_volume(self):
        assert EXPECTED_MOUNT_PATH in finalize_result.auto_volumes
        assert isinstance(
            finalize_result.auto_volumes[EXPECTED_MOUNT_PATH], modal.Volume
        )

    def test_volume_mounts_path_uses_prefix(self):
        for mount_path in finalize_result.volume_mounts:
            assert mount_path.startswith(VOLUME_MOUNT_PATH_PREFIX)


class TestAutoMountRemote:
    """Test that auto-mounted volumes work correctly inside Modal containers."""

    def test_auto_mount_target_type(self):
        """Inside Modal, get_modal_target should return ModalMountedVolumeTarget."""
        probe_fn = modal.Function.from_name(
            app_name=TEST_APP_NAME,
            name="probe_auto_mount",
        )
        result = probe_fn.remote()
        assert result["target_type"] == "ModalMountedVolumeTarget"

    def test_auto_mount_path_exists(self):
        """Inside Modal, the predefined mount path should exist as a directory."""
        probe_fn = modal.Function.from_name(
            app_name=TEST_APP_NAME,
            name="probe_auto_mount",
        )
        result = probe_fn.remote()
        assert result["predefined_path_exists"] is True


class TestUserVolumesOverride:
    """Test that user-specified volumes override auto-mounted volumes."""

    def test_user_volumes_take_precedence(self):
        """User volumes at the same mount path should override auto volumes."""
        user_volume = modal.Volume.from_name(VOLUME_NAME)
        custom_path = "/custom/mount"

        app = StardagApp(
            "test-override-app",
            builder_settings=FunctionSettings(
                image=TEST_IMAGE,
                volumes={custom_path: user_volume},
            ),
            worker_settings={
                "default": FunctionSettings(
                    image=TEST_IMAGE,
                    volumes={custom_path: user_volume},
                )
            },
        )

        result = app.finalize()

        # User volumes should be present
        assert (
            custom_path in result.auto_volumes
            or EXPECTED_MOUNT_PATH in result.auto_volumes
        )

        # Verify _prepare_function_settings merges correctly
        settings = StardagApp._prepare_function_settings(
            FunctionSettings(
                image=TEST_IMAGE,
                volumes={EXPECTED_MOUNT_PATH: user_volume},
            ),
            extra_secrets=[],
            auto_volumes=result.auto_volumes,
        )
        # User volume should override auto volume at the same path
        assert settings["volumes"][EXPECTED_MOUNT_PATH] is user_volume

    def test_auto_volumes_added_to_functions(self):
        """Auto volumes should be added when user doesn't specify that path."""
        app = StardagApp(
            "test-auto-add-app",
            builder_settings=FunctionSettings(image=TEST_IMAGE),
            worker_settings={"default": FunctionSettings(image=TEST_IMAGE)},
        )

        result = app.finalize()

        # Verify auto volumes are present in the result
        assert EXPECTED_MOUNT_PATH in result.auto_volumes
        assert EXPECTED_MOUNT_PATH in result.volume_mounts

        # Verify _prepare_function_settings includes auto volumes
        settings = StardagApp._prepare_function_settings(
            FunctionSettings(image=TEST_IMAGE),
            extra_secrets=[],
            auto_volumes=result.auto_volumes,
        )
        assert EXPECTED_MOUNT_PATH in settings["volumes"]
