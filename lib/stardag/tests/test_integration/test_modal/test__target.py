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
# Unit tests for async methods (mocked, no Modal deployment required)
# =============================================================================


class TestModalVolumeRemoteFileSystemAsync:
    """Unit tests for ModalVolumeRemoteFileSystem async methods.

    These tests mock the Modal library to verify that:
    1. modal.Volume.from_name() is called synchronously (not with .aio)
    2. The async methods on Volume (.iterdir.aio, .read_file.aio, .batch_upload.aio)
       are called correctly

    This tests the fix for the bug where modal.Volume.from_name.aio() was incorrectly
    used (from_name doesn't have an .aio variant - it's a sync factory).
    """

    @pytest.fixture
    def mock_modal_volume(self):
        """Create a mock Modal Volume with async methods."""
        from unittest.mock import AsyncMock, MagicMock

        # Create the volume mock
        volume = MagicMock()

        # Mock iterdir.aio as an async generator
        async def mock_iterdir_aio(path):
            # Yield nothing (simulating empty directory / file not found)
            return
            yield  # Make it a generator

        volume.iterdir.aio = mock_iterdir_aio

        # Mock read_file.aio as an async generator that yields bytes
        async def mock_read_file_aio(path):
            yield b"test content"

        volume.read_file.aio = mock_read_file_aio

        # Mock batch_upload.aio as an async context manager
        batch_mock = MagicMock()
        batch_mock.put_file = MagicMock()

        async_cm = AsyncMock()
        async_cm.__aenter__.return_value = batch_mock
        async_cm.__aexit__.return_value = None

        volume.batch_upload.aio.return_value = async_cm

        return volume

    @pytest.mark.asyncio
    async def test_exists_aio_uses_sync_from_name(self, mock_modal_volume):
        """Test that exists_aio uses modal.Volume.from_name() synchronously.

        The bug was calling `await modal.Volume.from_name.aio(volume_name)`
        which doesn't exist - from_name is a synchronous factory method.
        """
        from unittest.mock import patch

        with patch("modal.Volume") as MockVolume:
            # from_name should be called synchronously and return the volume
            MockVolume.from_name.return_value = mock_modal_volume

            # Import after patching
            from stardag.integration.modal._target import ModalVolumeRemoteFileSystem

            rfs = ModalVolumeRemoteFileSystem()
            uri = "modalvol://test-volume/path/to/file.txt"

            # This should NOT raise "object MagicMock can't be used in 'await' expression"
            result = await rfs.exists_aio(uri)

            # Verify from_name was called synchronously (not awaited)
            MockVolume.from_name.assert_called_once_with("test-volume")

            # from_name should NOT have been called with .aio
            assert (
                not hasattr(MockVolume.from_name, "aio")
                or not MockVolume.from_name.aio.called
            )

            # Result should be False since mock returns empty iterator
            assert result is False

    @pytest.mark.asyncio
    async def test_download_aio_uses_sync_from_name(self, mock_modal_volume, tmp_path):
        """Test that download_aio uses modal.Volume.from_name() synchronously."""
        from unittest.mock import patch

        with patch("modal.Volume") as MockVolume:
            MockVolume.from_name.return_value = mock_modal_volume

            from stardag.integration.modal._target import ModalVolumeRemoteFileSystem

            rfs = ModalVolumeRemoteFileSystem()
            uri = "modalvol://test-volume/path/to/file.txt"
            dest = tmp_path / "downloaded.txt"

            await rfs.download_aio(uri, dest)

            # Verify from_name was called synchronously
            MockVolume.from_name.assert_called_once_with("test-volume")

            # Verify file was written
            assert dest.exists()
            assert dest.read_bytes() == b"test content"

    @pytest.mark.asyncio
    async def test_upload_aio_uses_sync_from_name(self, mock_modal_volume, tmp_path):
        """Test that upload_aio uses modal.Volume.from_name() synchronously."""
        from unittest.mock import patch

        # Create a source file
        source = tmp_path / "source.txt"
        source.write_text("upload content")

        with patch("modal.Volume") as MockVolume:
            MockVolume.from_name.return_value = mock_modal_volume

            from stardag.integration.modal._target import ModalVolumeRemoteFileSystem

            rfs = ModalVolumeRemoteFileSystem()
            uri = "modalvol://test-volume/path/to/uploaded.txt"

            await rfs.upload_aio(source, uri)

            # Verify from_name was called synchronously
            MockVolume.from_name.assert_called_once_with("test-volume")

    @pytest.mark.asyncio
    async def test_exists_aio_returns_true_for_existing_file(self):
        """Test exists_aio returns True when file exists."""
        from unittest.mock import MagicMock, patch

        from modal.volume import FileEntryType

        # Create a mock that simulates a file existing
        volume = MagicMock()

        async def mock_iterdir_aio(path):
            entry = MagicMock()
            entry.type = FileEntryType.FILE
            entry.path = path
            yield entry

        volume.iterdir.aio = mock_iterdir_aio

        with patch("modal.Volume") as MockVolume:
            MockVolume.from_name.return_value = volume

            from stardag.integration.modal._target import ModalVolumeRemoteFileSystem

            rfs = ModalVolumeRemoteFileSystem()
            uri = "modalvol://test-volume/path/to/file.txt"

            result = await rfs.exists_aio(uri)

            assert result is True

    @pytest.mark.asyncio
    async def test_exists_aio_returns_false_for_directory(self):
        """Test exists_aio returns False when path is a directory."""
        from unittest.mock import MagicMock, patch

        from modal.volume import FileEntryType

        volume = MagicMock()

        async def mock_iterdir_aio(path):
            entry = MagicMock()
            entry.type = FileEntryType.DIRECTORY
            entry.path = path
            yield entry

        volume.iterdir.aio = mock_iterdir_aio

        with patch("modal.Volume") as MockVolume:
            MockVolume.from_name.return_value = volume

            from stardag.integration.modal._target import ModalVolumeRemoteFileSystem

            rfs = ModalVolumeRemoteFileSystem()
            uri = "modalvol://test-volume/path/to/dir"

            result = await rfs.exists_aio(uri)

            assert result is False

    @pytest.mark.asyncio
    async def test_exists_aio_handles_file_not_found(self):
        """Test exists_aio returns False for FileNotFoundError."""
        from unittest.mock import MagicMock, patch

        volume = MagicMock()

        async def mock_iterdir_aio(path):
            raise FileNotFoundError("No such file")
            yield  # Make it a generator

        volume.iterdir.aio = mock_iterdir_aio

        with patch("modal.Volume") as MockVolume:
            MockVolume.from_name.return_value = volume

            from stardag.integration.modal._target import ModalVolumeRemoteFileSystem

            rfs = ModalVolumeRemoteFileSystem()
            uri = "modalvol://test-volume/nonexistent.txt"

            result = await rfs.exists_aio(uri)

            assert result is False
