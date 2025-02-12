from pathlib import Path

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
    target = RemoteFileSystemTarget(path=uri, rfs=rfs)
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
    target = RemoteFileSystemTarget(path=uri, rfs=rfs)
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
    target = RemoteFileSystemTarget(path=uri, rfs=rfs)
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
    target = RemoteFileSystemTarget(path=uri, rfs=rfs)
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
    dir_target = DirectoryTarget(path=str(tmp_path / "test"), prototype=LocalTarget)
    assert not dir_target.exists()

    sub_a: LocalTarget = dir_target.get_sub_target("a")
    assert not sub_a.exists()
    assert sub_a.path == str(tmp_path / "test" / "a")
    with sub_a.proxy_path("w") as sub_a_path:
        sub_a_path.write_text("A")
    assert sub_a.exists()

    assert not dir_target.exists()
    assert dir_target._sub_keys == ["a"]  # noqa

    sub_b: LocalTarget = dir_target / "b"
    assert not sub_b.exists()
    assert sub_b.path == str(tmp_path / "test" / "b")
    with sub_b.proxy_path("w") as sub_b_path:
        sub_b_path.write_text("B")
    assert sub_b.exists()

    assert not dir_target.exists()
    assert dir_target._sub_keys == ["a", "b"]  # noqa

    dir_target.mark_done()
    assert dir_target.exists()

    sub_keys_target = dir_target.sub_keys_target()
    assert sub_keys_target.exists()
    with sub_keys_target.proxy_path("r") as sub_keys_target:
        assert sub_keys_target.read_text() == "a\nb"
