"""Tests for the optional disk-cache wrapper around ModalVolumeRemoteFileSystem.

These tests run locally (no `modal.Function`) — caching only applies to the
API-based ``RemoteFileTarget`` path used outside Modal. Auth + a real
``stardag-testing`` volume are required, mirroring ``test__target.py``.
"""

import asyncio
import uuid
from pathlib import Path

import pytest

from stardag.target import CachedRemoteFileSystem, RemoteFileTarget

VOLUME_NAME = "stardag-testing"

try:
    import modal
    from modal.exception import AuthError

    from stardag.integration import modal as sd_modal
    from stardag.integration.modal._target import (
        ModalVolumeRemoteFileSystem,
        _init_modal_volume_file_system,
        modal_volume_rfs_provider,
    )

    try:
        VOLUME = modal.Volume.from_name(VOLUME_NAME, create_if_missing=True)
        VOLUME.listdir("/")
    except AuthError:
        pytest.skip("Skipping modal tests (not authenticated)", allow_module_level=True)

except ImportError:
    pytest.skip("Skipping modal tests (import not available)", allow_module_level=True)


@pytest.fixture
def reset_rfs_provider():
    """Drop the cached provider state before and after each test."""
    modal_volume_rfs_provider.clear()
    yield
    modal_volume_rfs_provider.clear()


# ---------------------------------------------------------------------------
# Wiring: USE_CACHE env var toggles the wrapper.
# ---------------------------------------------------------------------------


def test_init_unwrapped_when_use_cache_unset(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("STARDAG_TARGET_MODALVOL_USE_CACHE", raising=False)
    fs = _init_modal_volume_file_system()
    assert isinstance(fs, ModalVolumeRemoteFileSystem)
    assert not isinstance(fs, CachedRemoteFileSystem)


def test_init_wrapped_when_use_cache_set(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("STARDAG_TARGET_MODALVOL_USE_CACHE", "true")
    fs = _init_modal_volume_file_system()
    assert isinstance(fs, CachedRemoteFileSystem)
    assert isinstance(fs.wrapped, ModalVolumeRemoteFileSystem)


# ---------------------------------------------------------------------------
# Round-trip: write/read/exists through a cached target hits the real volume
# AND populates the local cache directory.
# ---------------------------------------------------------------------------


def _override_with_cached_rfs(cache_root: Path):
    return modal_volume_rfs_provider.override(
        CachedRemoteFileSystem(
            wrapped=ModalVolumeRemoteFileSystem(),
            root=str(cache_root),
        )
    )


def _expected_cache_path(cache_root: Path, rel: str) -> Path:
    return cache_root / VOLUME_NAME / rel


def test_round_trip_populates_cache(tmp_path: Path, reset_rfs_provider):
    cache_root = tmp_path / "cache"
    temp_dir = f"test-cache-{uuid.uuid4()}"
    rel = f"{temp_dir}/test.txt"
    uri = f"modalvol://{VOLUME_NAME}/{rel}"
    expected_cache_file = _expected_cache_path(cache_root, rel)

    with _override_with_cached_rfs(cache_root):
        target = sd_modal.get_modal_target(uri)
        # Cache only applies on the API-based path; mounted targets bypass the RFS.
        assert isinstance(target, RemoteFileTarget)

        try:
            with target.open("w") as f:
                f.write("hello cache")

            assert expected_cache_file.exists()
            assert expected_cache_file.read_text() == "hello cache"

            assert target.exists()

            with target.open("r") as f:
                assert f.read() == "hello cache"
        finally:
            try:
                VOLUME.remove_file(temp_dir, recursive=True)
            except Exception:
                pass


def test_round_trip_populates_cache_aio(tmp_path: Path, reset_rfs_provider):
    cache_root = tmp_path / "cache"
    temp_dir = f"test-cache-aio-{uuid.uuid4()}"
    rel = f"{temp_dir}/test_aio.txt"
    uri = f"modalvol://{VOLUME_NAME}/{rel}"
    expected_cache_file = _expected_cache_path(cache_root, rel)

    async def _run():
        with _override_with_cached_rfs(cache_root):
            target = sd_modal.get_modal_target(uri)
            assert isinstance(target, RemoteFileTarget)

            async with target.open_aio("w") as f:
                await f.write("hello cache aio")

            assert expected_cache_file.exists()
            assert expected_cache_file.read_text() == "hello cache aio"

            assert await target.exists_aio()

            async with target.open_aio("r") as f:
                assert await f.read() == "hello cache aio"

    try:
        asyncio.run(_run())
    finally:
        try:
            VOLUME.remove_file(temp_dir, recursive=True)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Cache-hit isolation: once cached, a read can be served without the wrapped
# RFS being touched. Verified by swapping the wrapped RFS for one that errors
# on every operation, then re-reading.
# ---------------------------------------------------------------------------


class _ExplodingRemoteFileSystem(ModalVolumeRemoteFileSystem):
    """Test double: any RFS operation raises, so a successful read proves
    the result came from the local cache."""

    URI_PREFIX = ModalVolumeRemoteFileSystem.URI_PREFIX

    def exists(self, uri: str) -> bool:
        raise AssertionError(f"unexpected exists() call for {uri}")

    def download(self, uri: str, destination: Path):
        raise AssertionError(f"unexpected download() call for {uri}")

    def upload(self, source: Path, uri: str, ok_remove: bool = False):
        raise AssertionError(f"unexpected upload() call for {uri}")

    async def exists_aio(self, uri: str) -> bool:
        raise AssertionError(f"unexpected exists_aio() call for {uri}")

    async def download_aio(self, uri: str, destination: Path) -> None:
        raise AssertionError(f"unexpected download_aio() call for {uri}")

    async def upload_aio(self, source: Path, uri: str, ok_remove: bool = False) -> None:
        raise AssertionError(f"unexpected upload_aio() call for {uri}")


def test_cached_read_does_not_hit_wrapped_rfs(tmp_path: Path, reset_rfs_provider):
    """After a write populates the cache, reads should not touch the wrapped RFS."""
    cache_root = tmp_path / "cache"
    temp_dir = f"test-cache-hit-{uuid.uuid4()}"
    rel = f"{temp_dir}/test.txt"
    uri = f"modalvol://{VOLUME_NAME}/{rel}"

    # 1) Write through a real cached RFS so both volume and cache are populated.
    with _override_with_cached_rfs(cache_root):
        target = sd_modal.get_modal_target(uri)
        try:
            with target.open("w") as f:
                f.write("cache hit")

            # 2) Swap in a cached RFS whose wrapped backend explodes on access.
            #    A successful read proves it came from the cache layer alone.
            exploding = CachedRemoteFileSystem(
                wrapped=_ExplodingRemoteFileSystem(),
                root=str(cache_root),
            )
            with modal_volume_rfs_provider.override(exploding):
                target_hit = sd_modal.get_modal_target(uri)
                assert target_hit.exists()
                with target_hit.open("r") as f:
                    assert f.read() == "cache hit"
        finally:
            try:
                VOLUME.remove_file(temp_dir, recursive=True)
            except Exception:
                pass
