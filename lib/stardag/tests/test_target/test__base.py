from pathlib import Path

import pytest

from stardag.target import (
    DirectoryTarget,
    InMemoryRemoteFileSystem,
    LocalTarget,
    RemoteFileSystemTarget,
)
from stardag.target._base import CachedRemoteFileSystem


def test_local_target(tmp_path: Path):
    target = LocalTarget(str(tmp_path / "test.txt"))
    assert not target.exists()

    with target.open("w") as f:
        f.write("test")

    assert target.exists()

    with target.open("r") as f:
        assert f.read() == "test"

    with target.open("rb") as f:
        assert f.read() == b"test"


# NOTE we are intentionally not parameterizing this test with binary=True/False
# to also check type checking based in the mode parameter.
def test_local_target_binary(tmp_path: Path):
    target = LocalTarget(str(tmp_path / "test.txt"))
    assert not target.exists()

    with target.open("wb") as f:
        f.write(b"test")

    assert target.exists()

    with target.open("r") as f:
        assert f.read() == "test"

    with target.open("rb") as f:
        assert f.read() == b"test"


def test_local_target_proxy_path(tmp_path: Path):
    target = LocalTarget(str(tmp_path / "test.txt"))
    assert not target.exists()

    with target.proxy_path("w") as proxy_path:
        with proxy_path.open("w") as f:
            f.write("test")

    assert target.exists()

    with target.open("r") as proxy_handle:
        assert proxy_handle.read() == "test"

    with target.proxy_path("r") as proxy_path:
        with proxy_path.open("r") as proxy_handle:
            assert proxy_handle.read() == "test"


def test_remote_filesystem_target():
    rfs = InMemoryRemoteFileSystem()
    uri = "in-memory://bucket/key"
    target = RemoteFileSystemTarget(uri=uri, rfs=rfs)
    assert not target.exists()

    with target.open("w") as f:
        f.write("test")

    assert target.exists()

    with target.open("r") as f:
        assert f.read() == "test"

    with target.open("rb") as f:
        assert f.read() == b"test"

    assert rfs.uri_to_bytes[uri] == b"test"


# NOTE we are intentionally not parameterizing this test with binary=True/False
# to also check type checking based in the mode parameter.
def test_remote_filesystem_target_binary():
    rfs = InMemoryRemoteFileSystem()
    uri = "in-memory://bucket/key"
    target = RemoteFileSystemTarget(uri=uri, rfs=rfs)
    assert not target.exists()

    with target.open("wb") as f:
        f.write(b"test")

    assert target.exists()

    with target.open("r") as f:
        assert f.read() == "test"

    with target.open("rb") as f:
        assert f.read() == b"test"

    assert rfs.uri_to_bytes[uri] == b"test"


def test_remote_filesystem_target_proxy_path():
    rfs = InMemoryRemoteFileSystem()
    uri = "in-memory://bucket/key"
    target = RemoteFileSystemTarget(uri=uri, rfs=rfs)
    assert not target.exists()

    with target.proxy_path("w") as proxy_path:
        with proxy_path.open("w") as f:
            f.write("test")

    assert target.exists()

    with target.open("r") as proxy_handle:
        assert proxy_handle.read() == "test"

    with target.proxy_path("r") as proxy_path:
        with proxy_path.open("r") as proxy_handle:
            assert proxy_handle.read() == "test"

    assert rfs.uri_to_bytes[uri] == b"test"


def test_cached_remote_filesystem_target(tmp_path: Path):
    rfs_base = InMemoryRemoteFileSystem()
    rfs = CachedRemoteFileSystem(
        wrapped=rfs_base,
        root=str(tmp_path / "cache"),
    )
    uri = "in-memory://bucket/key"
    target = RemoteFileSystemTarget(uri=uri, rfs=rfs)
    assert not target.exists()

    with target.open("w") as f:
        f.write("test")

    assert target.exists()

    with target.open("r") as f:
        assert f.read() == "test"

    with target.open("rb") as f:
        assert f.read() == b"test"

    assert rfs_base.uri_to_bytes[uri] == b"test"

    # Check that the cached file is present.
    cache_path = rfs.get_cache_path(uri)
    assert cache_path.exists()
    assert str(cache_path.relative_to(tmp_path)) == "cache/bucket/key"

    assert cache_path.read_text() == "test"


def test_directory_target(tmp_path: Path):
    dir_target = DirectoryTarget(uri=str(tmp_path / "test"), prototype=LocalTarget)
    assert not dir_target.exists()

    sub_a: LocalTarget = dir_target.get_sub_target("a")
    assert not sub_a.exists()
    assert sub_a.uri == str(tmp_path / "test" / "a")
    with sub_a.proxy_path("w") as sub_a_path:
        sub_a_path.write_text("A")
    assert sub_a.exists()

    assert not dir_target.exists()
    assert dir_target._sub_keys == {"a"}  # noqa

    sub_b: LocalTarget = dir_target / "b"
    assert not sub_b.exists()
    assert sub_b.uri == str(tmp_path / "test" / "b")
    with sub_b.proxy_path("w") as sub_b_path:
        sub_b_path.write_text("B")
    assert sub_b.exists()

    assert not dir_target.exists()
    assert dir_target._sub_keys == {"a", "b"}  # noqa

    dir_target.mark_done()
    assert dir_target.exists()

    sub_keys_target = dir_target.sub_keys_target()
    assert sub_keys_target.exists()
    with sub_keys_target.proxy_path("r") as sub_keys_target:
        assert sub_keys_target.read_text() == "a\nb"


# ==================== Async Tests ====================


@pytest.mark.asyncio
async def test_local_target_open_aio(tmp_path: Path):
    target = LocalTarget(str(tmp_path / "test.txt"))
    assert not target.exists()

    async with target.open_aio("w") as f:
        await f.write("test")

    assert target.exists()

    async with target.open_aio("r") as f:
        content = await f.read()
        assert content == "test"


@pytest.mark.asyncio
async def test_local_target_open_aio_binary(tmp_path: Path):
    target = LocalTarget(str(tmp_path / "test.txt"))
    assert not target.exists()

    async with target.open_aio("wb") as f:
        await f.write(b"test")

    assert target.exists()

    async with target.open_aio("rb") as f:
        content = await f.read()
        assert content == b"test"


@pytest.mark.asyncio
async def test_remote_filesystem_target_open_aio():
    rfs = InMemoryRemoteFileSystem()
    uri = "in-memory://bucket/key"
    target = RemoteFileSystemTarget(uri=uri, rfs=rfs)
    assert not target.exists()

    async with target.open_aio("w") as f:
        await f.write("test")

    assert target.exists()

    async with target.open_aio("r") as f:
        content = await f.read()
        assert content == "test"

    assert rfs.uri_to_bytes[uri] == b"test"


@pytest.mark.asyncio
async def test_remote_filesystem_target_open_aio_binary():
    rfs = InMemoryRemoteFileSystem()
    uri = "in-memory://bucket/key"
    target = RemoteFileSystemTarget(uri=uri, rfs=rfs)
    assert not target.exists()

    async with target.open_aio("wb") as f:
        await f.write(b"test")

    assert target.exists()

    async with target.open_aio("rb") as f:
        content = await f.read()
        assert content == b"test"

    assert rfs.uri_to_bytes[uri] == b"test"


@pytest.mark.asyncio
async def test_in_memory_remote_filesystem_async_methods():
    """Test InMemoryRemoteFileSystem async methods directly."""
    rfs = InMemoryRemoteFileSystem()
    uri = "in-memory://bucket/key"

    # Test exists_aio (not exists)
    assert not await rfs.exists_aio(uri)

    # Upload via sync method, check async exists
    from pathlib import Path
    import tempfile

    with tempfile.NamedTemporaryFile(delete=False) as f:
        f.write(b"test data")
        temp_path = Path(f.name)

    try:
        rfs.upload(temp_path, uri)
        assert await rfs.exists_aio(uri)

        # Test download_aio
        download_path = temp_path.with_suffix(".downloaded")
        await rfs.download_aio(uri, download_path)
        assert download_path.read_bytes() == b"test data"
        download_path.unlink()

        # Test upload_aio
        new_uri = "in-memory://bucket/key2"
        temp_path.write_bytes(b"async uploaded")
        await rfs.upload_aio(temp_path, new_uri)
        assert await rfs.exists_aio(new_uri)
        assert rfs.uri_to_bytes[new_uri] == b"async uploaded"
    finally:
        temp_path.unlink(missing_ok=True)
