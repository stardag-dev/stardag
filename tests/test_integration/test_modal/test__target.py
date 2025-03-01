"""NOTE: after any changes, the app must be redeployed for the changes to take effect.

```sh
modal deploy tests/test_integration/test_modal/test__target.py
```
"""

import uuid

import pytest

from stardag import FileSystemTarget, get_target

try:
    import modal

    from stardag.integration import modal as sd_modal
except ImportError:
    pytest.skip("Skipping modal tests", allow_module_level=True)

VOLUME_NAME = "stardag-testing"
VOLUME = modal.Volume.from_name(VOLUME_NAME, create_if_missing=True)
MOUNT_PATH = "/data"
ROOT_DEAFULT = "stardag/root/default"

TEST_IMAGE = (
    modal.Image.debian_slim(python_version="3.12").pip_install(
        "pydantic>=2.8.2",
        "pydantic-settings>=2.7.1",
        "uuid6>=2024.7.10",
        "pytest>=8.2.2",
    )
    # .env(
    #     {
    #         "STARDAG_TARGET_ROOT__DEFAULT": (
    #             f"modalvol://stardag-default/{ROOT_DEAFULT}"
    #         ),
    #         "STARDAG_MODAL_VOLUME_MOUNTS": '{"/data": "stardag-testing"}',
    #     }
    # )
    # .add_local_python_source("stardag")
)

TEST_APP_NAME = "stardag-testing"

app = modal.App(TEST_APP_NAME)


def _write_read_full_uri(temp_test_dir: str):
    uri = f"modalvol://{VOLUME_NAME}/{temp_test_dir}/test.txt"
    target = sd_modal.ModalMountedVolumeTarget(uri)
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
    VOLUME.reload()  # TODO: should not be necessary
    _write_read_full_uri(temp_test_dir)


def test_modal_mounted_volume_target_full_uri():
    write_read_function = modal.Function.from_name(
        app_name=TEST_APP_NAME,
        name="write_read_full_uri",
    )
    temp_test_dir = f"test-{uuid.uuid4()}"
    try:
        write_read_function.remote(temp_test_dir=temp_test_dir)
        res = read_file(f"{temp_test_dir}/test.txt")
        assert res == b"test"
    finally:
        VOLUME.remove_file(temp_test_dir, recursive=True)


def _write_read_default_root(temp_test_dir: str):
    target = get_target(f"{temp_test_dir}/test.txt")
    assert isinstance(target, sd_modal.ModalMountedVolumeTarget)
    assert (
        target.path
        == f"modalvol://{VOLUME_NAME}/{ROOT_DEAFULT}/{temp_test_dir}/test.txt"
    )
    _write_read_(target)


@app.function(
    image=(
        TEST_IMAGE.env(
            {
                "STARDAG_TARGET_ROOT__DEFAULT": (
                    f"modalvol://stardag-testing/{ROOT_DEAFULT}"
                ),
                "STARDAG_MODAL_VOLUME_MOUNTS": '{"/data": "stardag-testing"}',
            }
        ).add_local_python_source("stardag")
    ),
    volumes={MOUNT_PATH: VOLUME},
)
def write_read_default_root(temp_test_dir: str):
    VOLUME.reload()  # TODO: should not be necessary
    _write_read_default_root(temp_test_dir)


def test_modal_mounted_volume_target_default_root():
    write_read_function = modal.Function.from_name(
        app_name=TEST_APP_NAME,
        name="write_read_default_root",
    )
    temp_test_dir = f"test-{uuid.uuid4()}"
    try:
        write_read_function.remote(temp_test_dir=temp_test_dir)
        res = read_file(f"{ROOT_DEAFULT}/{temp_test_dir}/test.txt")
        assert res == b"test"
    finally:
        VOLUME.remove_file(f"{ROOT_DEAFULT}/{temp_test_dir}", recursive=True)


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
