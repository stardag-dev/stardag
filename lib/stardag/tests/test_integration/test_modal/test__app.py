"""StardagApp.finalize() auto-mount integration tests.

These tests verify that finalize() auto-discovers Modal volumes from target
roots and mounts them at predefined paths, so that ModalMountedVolumeFileTarget
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

from stardag.config import config_provider

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
    from stardag.integration.modal._config import with_stardag_on_image
    from stardag.testing.modal._tasks import (
        AsyncDoubleTask,
        AsyncDynamicRangeSumTask,
        SyncDynamicRangeSumTask,
        make_range,
        sum_list,
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


TEST_IMAGE = with_stardag_on_image(
    modal.Image.debian_slim(python_version="3.12").pip_install(
        *_get_deps_for_modal_image()
    )
)

TARGET_ROOTS_SECRET = modal.Secret.from_dict(
    {"STARDAG_TARGET_ROOTS__DEFAULT": f"modalvol://{VOLUME_NAME}/{ROOT_DEFAULT}"}
)

stardag_app = StardagApp(
    "stardag-testing-app",
    builder_settings=FunctionSettings(image=TEST_IMAGE, secrets=[TARGET_ROOTS_SECRET]),
    worker_settings={
        "default": FunctionSettings(image=TEST_IMAGE, secrets=[TARGET_ROOTS_SECRET])
    },
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
        """Inside Modal, get_modal_target should return ModalMountedVolumeFileTarget."""
        probe_fn = modal.Function.from_name(
            app_name=TEST_APP_NAME,
            name="probe_auto_mount",
        )
        result = probe_fn.remote()
        assert result["target_type"] == "ModalMountedVolumeFileTarget"

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

    @pytest.fixture(autouse=True)
    def setup_target_roots_env(self):
        """Re-set target roots env var (cleared by global autouse fixture) and clear config cache."""
        os.environ["STARDAG_TARGET_ROOTS__DEFAULT"] = (
            f"modalvol://{VOLUME_NAME}/{ROOT_DEFAULT}"
        )
        config_provider.clear()
        yield
        config_provider.clear()

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


# --- End-to-end build test ---


class TestEndToEndBuild:
    """End-to-end test: build a DAG through the deployed Modal app.

    This exercises the full Builder/Runner flow:
    1. ``isolated_modal_target_root`` points the ``default`` target root at
       a unique subpath on the Modal volume (no pre-existing targets).
    2. build_remote() calls the deployed "build" function (Builder.__call__)
    3. Builder creates ModalTaskExecutor and runs stardag.build()
    4. ModalTaskExecutor dispatches each task to "worker_default" (Runner.__call__)
    5. Runner calls task.run(), which writes output to the Modal volume
    6. Load results locally via the Modal volume remote filesystem API
    7. Fixture recursively deletes the subpath on teardown.

    Tasks are defined in stardag.testing.modal._tasks (inside the stardag
    package) so they can be deserialized in the Modal container.
    """

    def test_build_and_load_result(self, isolated_modal_target_root):
        """Build a DAG through Modal, then load the result locally."""
        from stardag.build._base import BuildSummary

        root = sum_list(values=make_range(limit=5))
        assert not root.target().exists()

        # Build remotely through Modal
        result = stardag_app.build_remote(root)

        # Verify BuildSummary — both tasks should have succeeded (not previously_completed)
        assert isinstance(result, BuildSummary)
        assert result.task_count.succeeded == 2, (
            f"Expected 2 succeeded, got {result.task_count}"
        )

        # Load results locally via Modal volume remote filesystem API
        assert root.target().exists()
        assert root.target().load() == 10  # sum(range(5)) = 0+1+2+3+4


@pytest.fixture
def isolated_modal_target_root():
    """Wipe the test target-root subtree on the Modal volume before & after a test.

    The target root is baked into the Modal app at deploy time as a Secret
    (``STARDAG_TARGET_ROOTS__DEFAULT``), so client-side ``target_roots_override``
    does not propagate to remote containers. Instead we:

    1. Before the test: recursively delete everything under
       ``stardag/root/default`` on the Modal volume, and override local
       target roots so ``task.target().exists()`` and ``.load()`` see the
       same state as the remote container.
    2. After the test: recursively delete the subtree again.

    This is the Modal analogue of ``stardag.testing.test_harness``:
    independent of which tasks a test builds (including dynamic deps), the
    subtree is guaranteed empty pre-test and cleaned post-test. Any test
    running after this one also starts from a clean subtree.
    """
    from stardag.testing import target_roots_override

    target_roots = {"default": f"modalvol://{VOLUME_NAME}/{ROOT_DEFAULT}"}
    volume = modal.Volume.from_name(VOLUME_NAME)

    def _wipe():
        try:
            volume.remove_file(ROOT_DEFAULT, recursive=True)
        except Exception:
            pass  # path didn't exist — fine

    _wipe()
    with target_roots_override(target_roots):
        yield target_roots
    _wipe()


class TestEndToEndDynamicDepsBuild:
    """End-to-end tests verifying Runner.run()'s handling of async tasks and
    dynamic deps across the Modal boundary.

    Generators cannot be pickled, so ``Runner.run()`` drives them and returns
    a ``TaskStruct`` of incomplete yielded deps for idempotent re-execution.
    The ``ModalTaskExecutor`` builds those deps and re-invokes the task.

    Every test uses ``isolated_modal_target_root`` to start from a clean
    subpath on the Modal volume — no cross-test state, including dynamically
    discovered tasks that a walk-from-requires() cleanup approach would miss.
    """

    # Distinct limits per test method — Modal worker containers cache the
    # volume mount across invocations within a pytest run, so a ``make_range``
    # file from one test can still be visible to a warm container after the
    # volume-wipe fixture runs. Different params → different task IDs →
    # different file paths → no cross-test cache hits.

    def test_async_only_task_build(self, isolated_modal_target_root):
        """Async-only class-based task (``run_aio`` only) — Runner uses ``asyncio.run``."""
        from stardag.build._base import BuildSummary

        limit = 7
        source = sum_list(values=make_range(limit=limit))
        root = AsyncDoubleTask(input_task=source)
        assert not root.target().exists()

        result = stardag_app.build_remote(root)
        assert isinstance(result, BuildSummary)
        # 3 tasks: make_range, sum_list, AsyncDoubleTask
        assert result.task_count.succeeded == 3, (
            f"Expected 3 succeeded, got {result.task_count}"
        )
        assert root.target().exists()
        assert root.target().load() == sum(range(limit)) * 2

    def test_sync_dynamic_deps_build(self, isolated_modal_target_root):
        """Sync generator dynamic deps — Runner drives via re-execution."""
        from stardag.build._base import BuildSummary

        limit = 11
        root = SyncDynamicRangeSumTask(limit=limit)
        assert not root.target().exists()

        result = stardag_app.build_remote(root)
        assert isinstance(result, BuildSummary)
        # 2 tasks succeed: make_range (dynamically discovered) and the root.
        # The root is re-executed on Modal when the dyn dep becomes available,
        # but each task is counted once at final completion.
        assert result.task_count.succeeded == 2, (
            f"Expected 2 succeeded, got {result.task_count}"
        )
        assert root.target().exists()
        assert root.target().load() == sum(range(limit))

    def test_async_dynamic_deps_build(self, isolated_modal_target_root):
        """Async generator dynamic deps — Runner drives via ``_drive_async_generator``."""
        from stardag.build._base import BuildSummary

        limit = 13
        root = AsyncDynamicRangeSumTask(limit=limit)
        assert not root.target().exists()

        result = stardag_app.build_remote(root)
        assert isinstance(result, BuildSummary)
        assert result.task_count.succeeded == 2, (
            f"Expected 2 succeeded, got {result.task_count}"
        )
        assert root.target().exists()
        assert root.target().load() == sum(range(limit))
