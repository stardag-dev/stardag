"""Modal integration tests.

These tests require a deployed Modal app. The app is automatically deployed
before tests run if needed (via the `ensure_app_deployed` fixture).

To manually deploy:
    modal deploy tests/test_integration/test_modal/test__target.py
"""

import subprocess
import uuid

import pytest

from stardag import FileSystemTarget, get_target
from stardag.target import RemoteFileSystemTarget

VOLUME_NAME = "stardag-testing"

try:
    import modal
    from modal.exception import AuthError

    from stardag.integration import modal as sd_modal

    # check if logged in and volume exists
    try:
        VOLUME = modal.Volume.from_name(VOLUME_NAME)
        VOLUME.listdir("/")
    except AuthError:
        pytest.skip("Skipping modal tests (not authenticated)", allow_module_level=True)

except ImportError:
    pytest.skip("Skipping modal tests (import not available)", allow_module_level=True)


MOUNT_PATH = "/data"
ROOT_DEFAULT = "stardag/root/default"

TEST_IMAGE = modal.Image.debian_slim(python_version="3.12").pip_install(
    # TODO extract from pyproject.toml
    "pydantic>=2.8.2",
    "pydantic-settings>=2.7.1",
    "uuid6>=2024.7.10",
    "pytest>=8.2.2",
    "aiofiles>=23.1.0",
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


def _write_read_full_uri(temp_test_dir: str, mount_expected: bool):
    uri = f"modalvol://{VOLUME_NAME}/{temp_test_dir}/test.txt"
    target = sd_modal.get_modal_target(uri)
    assert target.path == uri
    if mount_expected:
        assert isinstance(target, sd_modal.ModalMountedVolumeTarget)
    else:
        assert isinstance(target, RemoteFileSystemTarget)

    _write_read_(target)


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
def write_read_full_uri(temp_test_dir: str):
    VOLUME.reload()
    _write_read_full_uri(temp_test_dir, mount_expected=True)


@app.function(
    image=TEST_IMAGE.add_local_python_source("stardag"),
)
def write_read_full_uri_no_mount(temp_test_dir: str):
    _write_read_full_uri(temp_test_dir, mount_expected=False)


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
        write_read_function.remote(temp_test_dir=temp_test_dir)
        res = read_file(f"{temp_test_dir}/test.txt")
        assert res == b"test"
    finally:
        try:
            VOLUME.remove_file(temp_test_dir, recursive=True)
        except Exception:
            pass  # File might not exist if test failed early


def _write_read_default_root(temp_test_dir: str, mount_expected: bool):
    target = get_target(f"{temp_test_dir}/test.txt")
    assert (
        target.path
        == f"modalvol://{VOLUME_NAME}/{ROOT_DEFAULT}/{temp_test_dir}/test.txt"
    )
    if mount_expected:
        assert isinstance(target, sd_modal.ModalMountedVolumeTarget)
    else:
        assert isinstance(target, RemoteFileSystemTarget)
    _write_read_(target)


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
def write_read_default_root(temp_test_dir: str):
    VOLUME.reload()
    _write_read_default_root(temp_test_dir, mount_expected=True)


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
def write_read_default_root_no_mount(temp_test_dir: str):
    _write_read_default_root(temp_test_dir, mount_expected=False)


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
        write_read_function.remote(temp_test_dir=temp_test_dir)
        res = read_file(f"{ROOT_DEFAULT}/{temp_test_dir}/test.txt")
        assert res == b"test"
    finally:
        try:
            VOLUME.remove_file(f"{ROOT_DEFAULT}/{temp_test_dir}", recursive=True)
        except Exception:
            pass  # File might not exist if test failed early


def _write_read_(target: FileSystemTarget):
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


async def _write_read_aio_(target: FileSystemTarget):
    """Async version of _write_read_ using async methods."""
    assert not await target.exists_aio()

    async with target.open_aio("w") as f:
        await f.write("test")

    assert await target.exists_aio()

    async with target.open_aio("r") as f:
        assert await f.read() == "test"

    async with target.open_aio("rb") as f:
        assert await f.read() == b"test"


def _write_read_aio_full_uri(temp_test_dir: str, mount_expected: bool):
    """Test async interface with full URI."""
    import asyncio

    uri = f"modalvol://{VOLUME_NAME}/{temp_test_dir}/test_aio.txt"
    target = sd_modal.get_modal_target(uri)
    assert target.path == uri
    if mount_expected:
        assert isinstance(target, sd_modal.ModalMountedVolumeTarget)
    else:
        assert isinstance(target, RemoteFileSystemTarget)

    asyncio.run(_write_read_aio_(target))


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
def write_read_aio_full_uri(temp_test_dir: str):
    VOLUME.reload()
    _write_read_aio_full_uri(temp_test_dir, mount_expected=True)


@app.function(
    image=TEST_IMAGE.add_local_python_source("stardag"),
)
def write_read_aio_full_uri_no_mount(temp_test_dir: str):
    _write_read_aio_full_uri(temp_test_dir, mount_expected=False)


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
        write_read_function.remote(temp_test_dir=temp_test_dir)
        res = read_file(f"{temp_test_dir}/test_aio.txt")
        assert res == b"test"
    finally:
        try:
            VOLUME.remove_file(temp_test_dir, recursive=True)
        except Exception:
            pass  # File might not exist if test failed early
