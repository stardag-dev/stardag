import asyncio
import logging
import time
from functools import lru_cache
from pathlib import Path

import aiofiles
import modal
from modal.exception import NotFoundError, ResourceExhaustedError
from modal.volume import FileEntryType
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from stardag.integration.modal._config import modal_config_provider
from stardag.target import LocalTarget, RemoteFileSystemABC, RemoteFileSystemTarget
from stardag.utils.resource_provider import resource_provider

logger = logging.getLogger(__name__)

MODAL_VOLUME_URI_PREFIX = "modalvol://"
VOLUME_MOUNT_PATH_PREFIX = "/mnt/stardag-volumes"


def get_default_volume_mount_path(volume_name: str) -> Path:
    """Predefined mount path for auto-mounted Modal volumes.

    Returns /mnt/stardag-volumes/<volume-name>. This path is used by
    StardagApp.finalize() to auto-mount discovered volumes, so that
    ModalMountedVolumeTarget (local I/O) is used instead of
    ModalVolumeRemoteFileSystem (API-based, rate-limit-prone).
    """
    return Path(VOLUME_MOUNT_PATH_PREFIX) / volume_name


_retry_on_rate_limit = retry(
    retry=retry_if_exception_type(ResourceExhaustedError),
    wait=wait_exponential_jitter(initial=0.5, max=10),
    stop=stop_after_attempt(6),
    before_sleep=before_sleep_log(logger, logging.DEBUG),
)


def get_volume_name_and_path(uri: str) -> tuple[str, str]:
    """Get the volume name from a modal volume URI.

    Modal volume URIs are of the form `modalvol://<volume-name>/<path>`.
    """
    if not uri.startswith(MODAL_VOLUME_URI_PREFIX):
        raise ValueError(f"URI '{uri}' does not start with '{MODAL_VOLUME_URI_PREFIX}'")
    volume_and_path = uri[len(MODAL_VOLUME_URI_PREFIX) :]
    volume, path = volume_and_path.split("/", 1)
    return volume, path


@lru_cache(maxsize=16)
def _get_volume(volume_name: str) -> modal.Volume:
    return modal.Volume.from_name(volume_name)


# --- Lazy volume reload with cooldown ---

VOLUME_RELOAD_COOLDOWN_SECONDS = 5.0
"""Minimum seconds between consecutive reloads of the same volume.

Prevents thundering-herd reloads when many targets on the same volume are
checked in quick succession (e.g. 1000 exists() calls during discovery).
"""

_volume_last_reload: dict[str, float] = {}
_volume_reload_aio_locks: dict[str, asyncio.Lock] = {}


def _maybe_reload_volume(volume_name: str) -> bool:
    """Reload a volume if the cooldown has expired (sync).

    Returns True if a reload was performed, False if skipped due to cooldown.
    """
    now = time.monotonic()
    if now - _volume_last_reload.get(volume_name, 0.0) < VOLUME_RELOAD_COOLDOWN_SECONDS:
        return False
    _get_volume(volume_name).reload()
    _volume_last_reload[volume_name] = time.monotonic()
    return True


async def _maybe_reload_volume_aio(volume_name: str) -> bool:
    """Reload a volume if the cooldown has expired (async).

    Uses a per-volume lock to prevent concurrent reloads when many async
    exists_aio() calls miss simultaneously.

    Returns True if a reload was performed, False if skipped.
    """
    # Fast path: skip if recently reloaded (no lock needed)
    now = time.monotonic()
    if now - _volume_last_reload.get(volume_name, 0.0) < VOLUME_RELOAD_COOLDOWN_SECONDS:
        return False

    # Acquire per-volume lock to serialize reloads
    if volume_name not in _volume_reload_aio_locks:
        _volume_reload_aio_locks[volume_name] = asyncio.Lock()
    async with _volume_reload_aio_locks[volume_name]:
        # Re-check after acquiring lock — another coroutine may have reloaded
        now = time.monotonic()
        if (
            now - _volume_last_reload.get(volume_name, 0.0)
            < VOLUME_RELOAD_COOLDOWN_SECONDS
        ):
            return False
        await _get_volume(volume_name).reload.aio()
        _volume_last_reload[volume_name] = time.monotonic()
        return True


class ModalMountedVolumeTarget(LocalTarget):
    """Target backed by a Modal volume mounted to the local filesystem.

    Uses local file I/O for reads/writes, which is much faster than the
    API-based ModalVolumeRemoteFileSystem and avoids rate-limit errors.

    Mount path resolution order:
    1. Explicit mount from STARDAG_MODAL_VOLUME_MOUNTS config
    2. Predefined auto-mount path /mnt/stardag-volumes/<volume-name> (if it exists)
    """

    def __init__(self, uri: str, **kwargs):
        super().__init__(uri, **kwargs)
        volume_name, in_volume_path = get_volume_name_and_path(uri)
        mount_path = modal_config_provider.get().volume_name_to_mount_path.get(
            volume_name
        )
        if mount_path is None:
            default = get_default_volume_mount_path(volume_name)
            if default.is_dir():
                mount_path = default
        if mount_path is None:
            raise ValueError(f"Volume '{volume_name}' is not mounted")

        self._volume_name = volume_name
        self.volume = _get_volume(volume_name)
        self.local_path = mount_path / in_volume_path

    @property
    def path(self) -> Path:
        return self.local_path

    def _post_write_hook(self) -> None:
        self.volume.commit()

    # --- Lazy reload on read-miss ---

    def exists(self) -> bool:
        if self.path.exists():
            return True
        if _maybe_reload_volume(self._volume_name):
            return self.path.exists()
        return False

    async def exists_aio(self) -> bool:
        if self.path.exists():
            return True
        if await _maybe_reload_volume_aio(self._volume_name):
            return self.path.exists()
        return False

    def _open(self, mode):  # type: ignore[override]
        try:
            return super()._open(mode)
        except FileNotFoundError:
            if mode in ("r", "rb") and _maybe_reload_volume(self._volume_name):
                return super()._open(mode)
            raise

    def _open_aio(self, mode):  # type: ignore[override]
        if mode in ("r", "rb") and not self.path.exists():
            _maybe_reload_volume(self._volume_name)
        return super()._open_aio(mode)


class ModalVolumeRemoteFileSystem(RemoteFileSystemABC):
    """API-based remote filesystem for Modal volumes.

    Uses Modal's Python API for all operations. This works without mounting
    the volume, but is subject to rate limits under heavy load. Prefer
    ModalMountedVolumeTarget when the volume is mounted locally.
    """

    URI_PREFIX = MODAL_VOLUME_URI_PREFIX

    @_retry_on_rate_limit
    def exists(self, uri: str) -> bool:
        """Check if a file exists in the Modal volume via API."""
        volume_name, in_volume_path = get_volume_name_and_path(uri)
        volume = _get_volume(volume_name)
        try:
            # recursive=False: we're checking a single file, not listing a subtree
            entry = next(volume.iterdir(in_volume_path, recursive=False))
            return entry.type == FileEntryType.FILE and entry.path == in_volume_path
        except NotFoundError:
            return False
        except StopIteration:
            return False
        except FileNotFoundError:
            return False

    def download(self, uri: str, destination: Path):
        """Download a file from the Modal volume via API."""
        volume_name, in_volume_path = get_volume_name_and_path(uri)
        volume = _get_volume(volume_name)
        with destination.open("wb") as dest_handle:
            volume.read_file_into_fileobj(in_volume_path, dest_handle)

    def upload(self, source: Path, uri: str, ok_remove: bool = False):
        """Upload a file to the Modal volume via API."""
        volume_name, in_volume_path = get_volume_name_and_path(uri)
        volume = _get_volume(volume_name)
        with volume.batch_upload() as batch:
            batch.put_file(source, in_volume_path)

    # Async implementations using Modal's .aio interface

    @_retry_on_rate_limit
    async def exists_aio(self, uri: str) -> bool:
        """Asynchronously check if the file exists in the Modal volume."""
        volume_name, in_volume_path = get_volume_name_and_path(uri)
        volume = _get_volume(volume_name)
        try:
            # recursive=False: we're checking a single file, not listing a subtree
            async for entry in volume.iterdir.aio(in_volume_path, recursive=False):
                return entry.type == FileEntryType.FILE and entry.path == in_volume_path
            return False
        except NotFoundError:
            return False
        except FileNotFoundError:
            return False

    async def download_aio(self, uri: str, destination: Path) -> None:
        """Asynchronously download a file from the Modal volume."""
        volume_name, in_volume_path = get_volume_name_and_path(uri)
        volume = _get_volume(volume_name)

        async with aiofiles.open(destination, "wb") as dest_handle:
            async for chunk in volume.read_file.aio(in_volume_path):
                await dest_handle.write(chunk)

    async def upload_aio(self, source: Path, uri: str, ok_remove: bool = False) -> None:
        """Asynchronously upload a file to the Modal volume."""
        volume_name, in_volume_path = get_volume_name_and_path(uri)
        volume = _get_volume(volume_name)

        async with volume.batch_upload.aio() as batch:
            batch.put_file(source, in_volume_path)


modal_volume_rfs_provider = resource_provider(
    RemoteFileSystemABC, ModalVolumeRemoteFileSystem
)


def get_modal_target(uri: str) -> ModalMountedVolumeTarget | RemoteFileSystemTarget:
    """Get the appropriate target for a Modal volume URI.

    Returns ModalMountedVolumeTarget (local I/O) if the volume is mounted,
    otherwise falls back to RemoteFileSystemTarget (API-based).

    Mount detection order:
    1. Explicit mount from STARDAG_MODAL_VOLUME_MOUNTS config
    2. Predefined auto-mount path /mnt/stardag-volumes/<volume-name> (if it exists)
    3. Fallback to API-based RemoteFileSystemTarget
    """
    volume_name, in_volume_path = get_volume_name_and_path(uri=uri)
    mount_path = modal_config_provider.get().volume_name_to_mount_path.get(volume_name)
    if mount_path is None:
        default = get_default_volume_mount_path(volume_name)
        if default.is_dir():
            mount_path = default
    if mount_path is not None:
        return ModalMountedVolumeTarget(uri)
    else:
        return RemoteFileSystemTarget(uri, rfs=modal_volume_rfs_provider.get())
