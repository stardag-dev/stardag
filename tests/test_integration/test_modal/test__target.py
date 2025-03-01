import uuid

import pytest

try:
    import modal

    from stardag.integration import modal as sd_modal
except ImportError:
    pytest.skip("Skipping modal tests", allow_module_level=True)

VOLUME_NAME = "stardag-testing"
VOLUME = modal.Volume.from_name(VOLUME_NAME, create_if_missing=True)
MOUNT_PATH = "/data"

TEST_IMAGE = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install(
        "pydantic>=2.8.2",
        "pydantic-settings>=2.7.1",
        "uuid6>=2024.7.10",
        "pytest>=8.2.2",
    )
    .env(
        {
            "STARDAG_TARGET_ROOT__DEFAULT": (
                "modalvol://stardag-default/stardag/root/default"
            ),
            "STARDAG_MODAL_VOLUME_MOUNTS": '{"/data": "stardag-testing"}',
        }
    )
    .add_local_python_source("stardag")
)

app = modal.App(
    "stardag-testing",
    image=TEST_IMAGE,
)


def write_read(temp_test_dir: str):
    uri = f"modalvol://{VOLUME_NAME}/{MOUNT_PATH}/{temp_test_dir}"
    target = sd_modal.MountedModalVolumeTarget(uri)
    assert not target.exists()

    with target.open("w") as f:
        f.write("test")

    assert target.exists()

    with target.open("r") as f:
        assert f.read() == "test"

    with target.open("rb") as f:
        assert f.read() == b"test"


@app.function(volumes={MOUNT_PATH: VOLUME})
def write_read_function(temp_test_dir: str):
    VOLUME.reload()  # TODO: should not be necessary
    write_read(temp_test_dir)


def test_modal_mounted_volume_target_base():
    temp_test_dir = f"test-{uuid.uuid4()}"
    write_read_function.remote(temp_test_dir=temp_test_dir)


@app.local_entrypoint()
def main():
    test_modal_mounted_volume_target_base()
