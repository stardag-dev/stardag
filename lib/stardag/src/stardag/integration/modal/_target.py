import asyncio
import logging
import threading
import time
from functools import lru_cache
from pathlib import Path

import aiofiles
import modal
from modal.exception import NotFoundError, ResourceExhaustedError
from pydantic import ValidationError
from pydantic_settings import SettingsConfigDict
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

try:
    # Newer modal defines this in the public `modal.types`. `modal.volume`
    # still re-exports it at runtime, but its .pyi no longer does, so
    # importing it from there resolves fine and then type-checks as a
    # missing symbol.
    from modal.types import (  # pyright: ignore[reportMissingImports]
        FileEntryType,
    )
except ImportError:  # no `modal.types` yet — still inside `modal>=1.0.0`
    from modal.volume import (
        FileEntryType,  # pyright: ignore[reportAttributeAccessIssue]
    )

from stardag.integration.modal._config import modal_config_provider
from stardag.target import (
    CachedRemoteFileSystem,
    CachedRemoteFileSystemConfig,
    LocalFileTarget,
    RemoteFileSystemABC,
    RemoteFileTarget,
)
from stardag.utils.resource_provider import resource_provider

logger = logging.getLogger(__name__)

MODAL_VOLUME_URI_PREFIX = "modalvol://"
VOLUME_MOUNT_PATH_PREFIX = "/mnt/stardag-volumes"


class ModalVolumeCacheConfig(CachedRemoteFileSystemConfig):
    """Local-disk cache config for the Modal-volume RemoteFileSystem.

    Caching wraps the API-based ``ModalVolumeRemoteFileSystem`` only — when a
    volume is locally mounted (running on Modal, or via
    ``STARDAG_MODAL_VOLUME_MOUNTS`` / the auto-mount path), ``get_modal_target``
    returns a ``ModalMountedVolumeFileTarget`` that bypasses the RFS entirely
    and is therefore unaffected by these settings.

    Caching is **opt-in** via ``STARDAG_TARGET_MODALVOL_CACHE_ROOT``: ``root``
    has no default. Modal volume names are only unique within a
    ``(workspace, environment)`` pair (unlike S3 bucket names which are
    globally unique), so a default cache root would silently collide across
    workspaces/environments — the cache is keyed by URI alone, and the same
    ``modalvol://<name>/...`` URI can resolve to different content under
    different credentials. Forcing the user to set the root makes the
    workspace/environment scoping a deliberate choice — e.g.
    ``STARDAG_TARGET_MODALVOL_CACHE_ROOT=~/.stardag/cache/modalvol/<workspace>/<environment>/``,
    or use ``root_by_prefix`` to map specific volumes to dedicated cache
    directories.
    """

    # NOTE inherited ``root: str`` is required (no default) — set
    # STARDAG_TARGET_MODALVOL_CACHE_ROOT to enable caching.

    model_config = SettingsConfigDict(
        env_prefix="stardag_target_modalvol_cache_",
        env_nested_delimiter="__",
    )


def get_default_volume_mount_path(volume_name: str) -> Path:
    """Predefined mount path for auto-mounted Modal volumes.

    Returns /mnt/stardag-volumes/<volume-name>. This path is used by
    StardagApp.finalize() to auto-mount discovered volumes, so that
    ModalMountedVolumeFileTarget (local I/O) is used instead of
    ModalVolumeRemoteFileSystem (API-based, rate-limit-prone).
    """
    return Path(VOLUME_MOUNT_PATH_PREFIX) / volume_name


# Modal's volume API is rate-limited, and hitting it from *outside* Modal —
# a local build or a trigger whose target root is a modalvol:// URI — pays
# full network cost per call on top. A wide DAG asks "does this output
# exist?" once per task, so the limit is reachable in ordinary use and must
# degrade into slowness, never into a failed build.
#
# Three things this policy gets right that the obvious one does not:
#
# - ``reraise=True``. Without it tenacity raises RetryError, which *hides*
#   the ResourceExhaustedError and its message inside a Future repr — the
#   caller cannot tell a rate limit from any other failure, and neither can
#   a log. Diagnosability first.
# - A budget sized for a rate limit rather than a blip: a stampede of
#   concurrent callers all back off into the same window, so the last one
#   through needs far more headroom than the first.
# - WARNING, not DEBUG, once the wait becomes user-visible. Silently
#   retrying for half a minute looks like a hang, and the user cannot act on
#   what they cannot see.
_MAX_VOLUME_API_ATTEMPTS = 10
_VOLUME_API_MAX_WAIT_SECONDS = 30

_retry_on_rate_limit = retry(
    retry=retry_if_exception_type(ResourceExhaustedError),
    wait=wait_exponential_jitter(initial=0.5, max=_VOLUME_API_MAX_WAIT_SECONDS),
    stop=stop_after_attempt(_MAX_VOLUME_API_ATTEMPTS),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)

# Bound how many volume-API calls this process has in flight, so concurrent
# callers cannot stampede the limit in the first place. Backing off *after*
# the fact converges slowly when N callers keep arriving; refusing to make
# the N+1th call until one returns converges immediately.
#
# This belongs here rather than in whatever is calling: the ceiling is a
# property of the backend, and a scheduler bounding its own fan-out has no
# way to know it. It also means a caller that raises its own concurrency
# still cannot exceed what the backend tolerates.
_MAX_CONCURRENT_VOLUME_API_CALLS = 8
_volume_api_semaphores: "dict[int, asyncio.Semaphore]" = {}
_volume_api_sync_semaphore = threading.Semaphore(_MAX_CONCURRENT_VOLUME_API_CALLS)


def _volume_api_semaphore() -> asyncio.Semaphore:
    """Per-event-loop semaphore for the volume API.

    Keyed by loop because an asyncio.Semaphore binds to the loop that first
    awaits it, and this package is used from more than one (the registry
    client already recreates its async client on a loop change for the same
    reason).
    """
    loop = asyncio.get_running_loop()
    semaphore = _volume_api_semaphores.get(id(loop))
    if semaphore is None:
        semaphore = asyncio.Semaphore(_MAX_CONCURRENT_VOLUME_API_CALLS)
        _volume_api_semaphores[id(loop)] = semaphore
    return semaphore


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


# --- Lazy volume reload with singleflight coalescing ---
#
# A reload only flushes writes that landed before it was *issued*. The previous
# implementation imposed a fixed cooldown between reloads to suppress thundering
# herds on bursty discovery scans, but as a side-effect it could also suppress
# a reload that was actually needed to observe a write committed during the
# cooldown window — leading to false negatives from exists()/_open() up to
# ``cooldown`` seconds wide.
#
# This implementation drops the cooldown and instead coalesces concurrent
# callers onto a single in-flight reload via per-volume locks. After the
# in-flight reload completes, *all* contending callers observe its result —
# no caller is ever told "stale view, but I won't reload for you". Sequential
# callers each pay the reload cost (fine: they want fresh state), but a
# burst of N concurrent callers still produces only one reload.
#
# We track the time at which the most recent reload was *issued* (not when
# it completed): a reload only covers writes ≤ its issue time, so a caller
# that started after another's reload was issued must trigger a fresh
# reload of its own to observe writes that may have landed in between. The
# timestamp is published only after the reload completes, so a coalesced
# waiter that observes it is also guaranteed that the reload data is
# locally visible (not just in-flight).

_volume_last_reload_issued: dict[str, float] = {}
_volume_reload_locks: dict[str, threading.Lock] = {}
# Per-loop async lock cache: asyncio.Lock instances are bound to the running
# event loop at acquire-time, so a Lock created in one loop must not be
# reused in another (e.g., across consecutive ``asyncio.run()`` calls).
# Keyed by ``(volume_name, id(loop))`` — entries from closed loops are
# bounded in number (one per closed loop that touched a given volume) and
# harmless beyond a small memory footprint.
_volume_reload_aio_locks: dict[tuple[str, int], asyncio.Lock] = {}


def _ensure_fresh_volume(volume_name: str) -> None:
    """Ensure the local view of the volume reflects writes committed before
    this call started. Concurrent calls coalesce onto one reload."""
    started = time.monotonic()
    # Fast path: a reload was issued at-or-after we started → covers us.
    if _volume_last_reload_issued.get(volume_name, 0.0) >= started:
        return
    lock = _volume_reload_locks.setdefault(volume_name, threading.Lock())
    with lock:
        # Re-check inside the lock: another caller may have just reloaded.
        if _volume_last_reload_issued.get(volume_name, 0.0) >= started:
            return
        # Capture issue time *before* the reload runs; publish *after* it
        # completes so the timestamp simultaneously means "issued at T" and
        # "data is locally visible". A concurrent caller in the fast path
        # that observes the timestamp can therefore trust both properties.
        issued_at = time.monotonic()
        _get_volume(volume_name).reload()
        _volume_last_reload_issued[volume_name] = issued_at


async def _ensure_fresh_volume_aio(volume_name: str) -> None:
    """Async variant of :func:`_ensure_fresh_volume`."""
    started = time.monotonic()
    if _volume_last_reload_issued.get(volume_name, 0.0) >= started:
        return
    # Key the lock by (volume, current loop) so that a fresh ``asyncio.run()``
    # gets its own lock instance instead of reusing one bound to a now-closed
    # loop.
    lock_key = (volume_name, id(asyncio.get_running_loop()))
    lock = _volume_reload_aio_locks.setdefault(lock_key, asyncio.Lock())
    async with lock:
        if _volume_last_reload_issued.get(volume_name, 0.0) >= started:
            return
        issued_at = time.monotonic()
        await _get_volume(volume_name).reload.aio()
        _volume_last_reload_issued[volume_name] = issued_at


class ModalMountedVolumeFileTarget(LocalFileTarget):
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
        _ensure_fresh_volume(self._volume_name)
        return self.path.exists()

    async def exists_aio(self) -> bool:
        if self.path.exists():
            return True
        await _ensure_fresh_volume_aio(self._volume_name)
        return self.path.exists()

    def _open(self, mode):  # type: ignore[override]
        try:
            return super()._open(mode)
        except FileNotFoundError:
            if mode in ("r", "rb"):
                _ensure_fresh_volume(self._volume_name)
                return super()._open(mode)
            raise

    def _open_aio(self, mode):  # type: ignore[override]
        # NOTE _open_aio is sync (returns an async context manager), so we
        # must use the sync reload helper. This briefly blocks the event
        # loop on a cache miss; acceptable given the alternative (returning
        # a custom CM that awaits the reload before opening) is significantly
        # more invasive.
        if mode in ("r", "rb") and not self.path.exists():
            _ensure_fresh_volume(self._volume_name)
        return super()._open_aio(mode)


class ModalVolumeRemoteFileSystem(RemoteFileSystemABC):
    """API-based remote filesystem for Modal volumes.

    Uses Modal's Python API for all operations. This works without mounting
    the volume, but is subject to rate limits under heavy load. Prefer
    ModalMountedVolumeFileTarget when the volume is mounted locally.
    """

    URI_PREFIX = MODAL_VOLUME_URI_PREFIX

    @_retry_on_rate_limit
    def exists(self, uri: str) -> bool:
        """Check if a file exists in the Modal volume via API."""
        volume_name, in_volume_path = get_volume_name_and_path(uri)
        volume = _get_volume(volume_name)
        # Bounded at the source — see _MAX_CONCURRENT_VOLUME_API_CALLS. Held
        # across the call, not just its start, so the bound is on in-flight
        # requests rather than on how fast they are issued.
        with _volume_api_sync_semaphore:
            return self._exists_uncontended(volume, in_volume_path)

    @staticmethod
    def _exists_uncontended(volume, in_volume_path: str) -> bool:
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
        async with _volume_api_semaphore():
            return await self._exists_aio_uncontended(volume, in_volume_path)

    @staticmethod
    async def _exists_aio_uncontended(volume, in_volume_path: str) -> bool:
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


def _get_modal_volume_cache_config() -> ModalVolumeCacheConfig | None:
    """Return cache config if STARDAG_TARGET_MODALVOL_CACHE_ROOT is set,
    else None. Caching is opt-in via that env var; ``root`` has no default
    (see ``ModalVolumeCacheConfig`` docstring for why)."""
    try:
        return ModalVolumeCacheConfig()  # type: ignore[call-arg]
    except ValidationError:
        return None


def _init_modal_volume_file_system() -> RemoteFileSystemABC:
    file_system: RemoteFileSystemABC = ModalVolumeRemoteFileSystem()
    cache_config = _get_modal_volume_cache_config()
    if cache_config is not None:
        file_system = CachedRemoteFileSystem(
            wrapped=file_system,
            **cache_config.model_dump(),
        )
    return file_system


modal_volume_rfs_provider = resource_provider(
    RemoteFileSystemABC, _init_modal_volume_file_system
)


def get_modal_target(uri: str) -> ModalMountedVolumeFileTarget | RemoteFileTarget:
    """Get the appropriate target for a Modal volume URI.

    Returns ModalMountedVolumeFileTarget (local I/O) if the volume is mounted,
    otherwise falls back to RemoteFileTarget (API-based).

    Mount detection order:
    1. Explicit mount from STARDAG_MODAL_VOLUME_MOUNTS config
    2. Predefined auto-mount path /mnt/stardag-volumes/<volume-name> (if it exists)
    3. Fallback to API-based RemoteFileTarget
    """
    volume_name, in_volume_path = get_volume_name_and_path(uri=uri)
    mount_path = modal_config_provider.get().volume_name_to_mount_path.get(volume_name)
    if mount_path is None:
        default = get_default_volume_mount_path(volume_name)
        if default.is_dir():
            mount_path = default
    if mount_path is not None:
        return ModalMountedVolumeFileTarget(uri)
    else:
        return RemoteFileTarget(uri, rfs=modal_volume_rfs_provider.get())
