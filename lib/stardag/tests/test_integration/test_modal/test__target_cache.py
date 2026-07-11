"""Round-trip tests for the optional disk-cache wrapper around
``ModalVolumeRemoteFileSystem``.

These tests hit the **real** Modal API and create/delete files on a
pre-existing ``stardag-testing`` volume in whichever
``(workspace, environment)`` is currently active for your local Modal
credentials. They run locally (no ``modal.Function``) — caching only
applies to the API-based ``RemoteFileTarget`` path used outside Modal.
Pure configuration wiring is covered (without Modal auth) by
``test__target_cache_config.py``.

.. warning::
    The caller is responsible for ensuring an appropriate
    profile/environment is selected before running — e.g. a personal/dev
    workspace, *not* a shared or production-adjacent one. Check with
    ``modal profile current`` and switch with
    ``modal profile activate <profile>`` if needed. The ``stardag-testing``
    volume must already exist in the active workspace/environment; these
    tests deliberately do **not** auto-create it (test discovery should
    not mutate external state). Create it once with
    ``modal volume create stardag-testing`` if you intend to run these
    tests locally.

TODO: harden the setup so these tests can run in CI — pin to a dedicated
test workspace/environment via ``MODAL_PROFILE`` / ``MODAL_ENVIRONMENT``,
provision credentials as a CI secret, and gate on those being set
instead of skipping silently on missing auth/volume.
"""

import asyncio
import uuid
from pathlib import Path

import pytest

from stardag.target import CachedRemoteFileSystem, RemoteFileTarget

VOLUME_NAME = "stardag-testing"

try:
    import modal

    from stardag.integration import modal as sd_modal
    from stardag.integration.modal._target import (
        ModalVolumeRemoteFileSystem,
        modal_volume_rfs_provider,
    )
    from stardag.testing.modal import live_modal_guard

    live_modal_guard(VOLUME_NAME)
    VOLUME = modal.Volume.from_name(VOLUME_NAME)

except ImportError:
    pytest.skip("Skipping modal tests (import not available)", allow_module_level=True)

pytestmark = pytest.mark.modal_live


@pytest.fixture
def reset_rfs_provider():
    """Drop the cached provider state before and after each test."""
    modal_volume_rfs_provider.clear()
    yield
    modal_volume_rfs_provider.clear()


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
