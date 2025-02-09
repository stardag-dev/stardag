from pathlib import Path

from stardag.target import LocalTarget, MockRemoteFileSystem, RemoteFileSystemTarget


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


def test_remote_filesystem_target():
    rfs = MockRemoteFileSystem()
    uri = "s3://bucket/key"
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
    rfs = MockRemoteFileSystem()
    uri = "s3://bucket/key"
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
