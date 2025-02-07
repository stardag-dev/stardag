from pathlib import Path

from stardag.target import LocalTarget


def test_local_target(tmp_path: Path):
    target = LocalTarget(str(tmp_path / "test.txt"))
    assert not target.exists()
    with target.open("w") as f:
        f.write("test")
    assert target.exists()
    with target.open("r") as f:
        assert f.read() == "test"
