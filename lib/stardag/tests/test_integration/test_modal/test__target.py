"""Modal integration tests.

These tests require a deployed Modal app. The app is automatically deployed
before tests run if needed (via the `ensure_app_deployed` fixture).

To manually deploy:
    modal deploy tests/test_integration/test_modal/test__target.py

Each remote function writes under a fresh per-*attempt* directory and returns
the in-volume path it wrote. Modal executes an input at least once, not
exactly once: the client re-submits an input whose attempt ended in an
internal failure (a lost input, a preempted worker) without counting it
against the function's retries. A re-run attempt writing to the path the
first one already wrote would fail its own ``assert not target.exists()``
-- which is how this module flaked (STA-169).
"""

import subprocess
import uuid
from pathlib import Path

import pytest
import tomllib

from stardag import FileTarget, get_file_target
from stardag.target import RemoteFileTarget

VOLUME_NAME = "stardag-testing"

try:
    import modal

    from stardag.integration import modal as sd_modal
    from stardag.testing.modal import live_modal_guard

    live_modal_guard(VOLUME_NAME)
    VOLUME = modal.Volume.from_name(VOLUME_NAME)

except ImportError:
    pytest.skip("Skipping modal tests (import not available)", allow_module_level=True)

pytestmark = pytest.mark.modal_live


MOUNT_PATH = "/data"
ROOT_DEFAULT = "stardag/root/default"


def _get_deps_for_modal_image() -> list[str]:
    """Extract dependencies from pyproject.toml for Modal image.

    Returns empty list when running inside Modal (pyproject.toml not available),
    since deps are already installed in the image at that point.
    """
    try:
        pyproject_path = Path(__file__).parents[3] / "pyproject.toml"
        with open(pyproject_path, "rb") as f:
            pyproject = tomllib.load(f)
    except (IndexError, FileNotFoundError):
        return []

    result = list(pyproject["project"]["dependencies"])
    result += pyproject["project"].get("optional-dependencies", {}).get("modal", [])
    result += pyproject.get("dependency-groups", {}).get("dev", [])
    return result


TEST_IMAGE = modal.Image.debian_slim(python_version="3.12").pip_install(
    *_get_deps_for_modal_image()
)

TEST_APP_NAME = "stardag-testing"

app = modal.App(TEST_APP_NAME)


@pytest.fixture(scope="module", autouse=True)
def ensure_app_deployed():
    """Ensure the Modal app is deployed before tests run.

    This deploys the app using the current local code, ensuring tests
    always run against the latest version.
    """
    result = subprocess.run(
        ["modal", "deploy", __file__],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.fail(f"Failed to deploy Modal app:\n{result.stderr}")
    yield
    # Optionally stop the app after tests (commented out to allow inspection)
    # subprocess.run(["modal", "app", "stop", TEST_APP_NAME])


def _attempt_dir(temp_test_dir: str) -> str:
    """A directory no earlier attempt of this input can have written to."""
    return f"{temp_test_dir}/attempt-{uuid.uuid4().hex}"


def _write_read_full_uri(temp_test_dir: str, mount_expected: bool) -> str:
    in_volume_path = f"{_attempt_dir(temp_test_dir)}/test.txt"
    uri = f"modalvol://{VOLUME_NAME}/{in_volume_path}"
    target = sd_modal.get_modal_target(uri)
    assert target.uri == uri
    if mount_expected:
        assert isinstance(target, sd_modal.ModalMountedVolumeFileTarget)
    else:
        assert isinstance(target, RemoteFileTarget)

    _write_read_(target)
    return in_volume_path


@app.function(
    image=(
        TEST_IMAGE.env(
            {
                "STARDAG_MODAL_VOLUME_MOUNTS": '{"/data": "stardag-testing"}',
            }
        ).add_local_python_source("stardag")
    ),
    volumes={MOUNT_PATH: VOLUME},
)
def write_read_full_uri(temp_test_dir: str) -> str:
    VOLUME.reload()
    return _write_read_full_uri(temp_test_dir, mount_expected=True)


@app.function(
    image=TEST_IMAGE.add_local_python_source("stardag"),
)
def write_read_full_uri_no_mount(temp_test_dir: str) -> str:
    return _write_read_full_uri(temp_test_dir, mount_expected=False)


@pytest.mark.parametrize(
    "use_mount",
    [True, False],
)
def test_modal_mounted_volume_target_full_uri(use_mount: bool):
    write_read_function = modal.Function.from_name(
        app_name=TEST_APP_NAME,
        name="write_read_full_uri" if use_mount else "write_read_full_uri_no_mount",
    )
    temp_test_dir = f"test-{uuid.uuid4()}"
    try:
        in_volume_path = write_read_function.remote(temp_test_dir=temp_test_dir)
        assert in_volume_path.startswith(f"{temp_test_dir}/")
        res = read_file(in_volume_path)
        assert res == b"test"
    finally:
        try:
            VOLUME.remove_file(temp_test_dir, recursive=True)
        except Exception:
            pass  # File might not exist if test failed early


def _write_read_default_root(temp_test_dir: str, mount_expected: bool) -> str:
    relpath = f"{_attempt_dir(temp_test_dir)}/test.txt"
    target = get_file_target(relpath)
    assert target.uri == f"modalvol://{VOLUME_NAME}/{ROOT_DEFAULT}/{relpath}"
    if mount_expected:
        assert isinstance(target, sd_modal.ModalMountedVolumeFileTarget)
    else:
        assert isinstance(target, RemoteFileTarget)
    _write_read_(target)
    return f"{ROOT_DEFAULT}/{relpath}"


@app.function(
    image=(
        TEST_IMAGE.env(
            {
                "STARDAG_TARGET_ROOTS__DEFAULT": (
                    f"modalvol://stardag-testing/{ROOT_DEFAULT}"
                ),
                "STARDAG_MODAL_VOLUME_MOUNTS": '{"/data": "stardag-testing"}',
            }
        ).add_local_python_source("stardag")
    ),
    volumes={MOUNT_PATH: VOLUME},
)
def write_read_default_root(temp_test_dir: str) -> str:
    VOLUME.reload()
    return _write_read_default_root(temp_test_dir, mount_expected=True)


@app.function(
    image=(
        TEST_IMAGE.env(
            {
                "STARDAG_TARGET_ROOTS__DEFAULT": (
                    f"modalvol://stardag-testing/{ROOT_DEFAULT}"
                ),
            }
        ).add_local_python_source("stardag")
    ),
)
def write_read_default_root_no_mount(temp_test_dir: str) -> str:
    return _write_read_default_root(temp_test_dir, mount_expected=False)


@pytest.mark.parametrize(
    "use_mount",
    [True, False],
)
def test_modal_mounted_volume_target_default_root(use_mount: bool):
    write_read_function = modal.Function.from_name(
        app_name=TEST_APP_NAME,
        name="write_read_default_root"
        if use_mount
        else "write_read_default_root_no_mount",
    )
    temp_test_dir = f"test-{uuid.uuid4()}"
    try:
        in_volume_path = write_read_function.remote(temp_test_dir=temp_test_dir)
        assert in_volume_path.startswith(f"{ROOT_DEFAULT}/{temp_test_dir}/")
        res = read_file(in_volume_path)
        assert res == b"test"
    finally:
        try:
            VOLUME.remove_file(f"{ROOT_DEFAULT}/{temp_test_dir}", recursive=True)
        except Exception:
            pass  # File might not exist if test failed early


def _write_read_(target: FileTarget):
    assert not target.exists()

    with target.open("w") as f:
        f.write("test")

    assert target.exists()

    with target.open("r") as f:
        assert f.read() == "test"

    with target.open("rb") as f:
        assert f.read() == b"test"


def read_file(in_volume_path: str) -> bytes:
    data = b""
    for chunk in VOLUME.read_file(in_volume_path):
        data += chunk

    return data


# =============================================================================
# Async interface tests (using real Modal app and volume)
# =============================================================================


async def _write_read_aio_(target: FileTarget):
    """Async version of _write_read_ using async methods."""
    assert not await target.exists_aio()

    async with target.open_aio("w") as f:
        await f.write("test")

    assert await target.exists_aio()

    async with target.open_aio("r") as f:
        assert await f.read() == "test"

    async with target.open_aio("rb") as f:
        assert await f.read() == b"test"


def _write_read_aio_full_uri(temp_test_dir: str, mount_expected: bool) -> str:
    """Test async interface with full URI."""
    import asyncio

    in_volume_path = f"{_attempt_dir(temp_test_dir)}/test_aio.txt"
    uri = f"modalvol://{VOLUME_NAME}/{in_volume_path}"
    target = sd_modal.get_modal_target(uri)
    assert target.uri == uri
    if mount_expected:
        assert isinstance(target, sd_modal.ModalMountedVolumeFileTarget)
    else:
        assert isinstance(target, RemoteFileTarget)

    asyncio.run(_write_read_aio_(target))
    return in_volume_path


@app.function(
    image=(
        TEST_IMAGE.env(
            {
                "STARDAG_MODAL_VOLUME_MOUNTS": '{"/data": "stardag-testing"}',
            }
        ).add_local_python_source("stardag")
    ),
    volumes={MOUNT_PATH: VOLUME},
)
def write_read_aio_full_uri(temp_test_dir: str) -> str:
    VOLUME.reload()
    return _write_read_aio_full_uri(temp_test_dir, mount_expected=True)


@app.function(
    image=TEST_IMAGE.add_local_python_source("stardag"),
)
def write_read_aio_full_uri_no_mount(temp_test_dir: str) -> str:
    return _write_read_aio_full_uri(temp_test_dir, mount_expected=False)


@pytest.mark.parametrize(
    "use_mount",
    [True, False],
)
def test_modal_target_async_full_uri(use_mount: bool):
    """Test async interface (exists_aio, open_aio) with full Modal volume URI."""
    write_read_function = modal.Function.from_name(
        app_name=TEST_APP_NAME,
        name="write_read_aio_full_uri"
        if use_mount
        else "write_read_aio_full_uri_no_mount",
    )
    temp_test_dir = f"test-aio-{uuid.uuid4()}"
    try:
        in_volume_path = write_read_function.remote(temp_test_dir=temp_test_dir)
        assert in_volume_path.startswith(f"{temp_test_dir}/")
        res = read_file(in_volume_path)
        assert res == b"test"
    finally:
        try:
            VOLUME.remove_file(temp_test_dir, recursive=True)
        except Exception:
            pass  # File might not exist if test failed early
