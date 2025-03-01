from pathlib import Path

import modal

from stardag.integration.modal._config import modal_config_provider
from stardag.target import LocalTarget

MODAL_VOLUME_URI_PREFIX = "modalvol://"


def get_volume_name_and_path(uri: str) -> tuple[str, str]:
    """Get the volume name from a modal volume URI.

    Modal volume URIs are of the form `modalvol://<volume-name>/<path>`.
    """
    if not uri.startswith(MODAL_VOLUME_URI_PREFIX):
        raise ValueError(f"URI '{uri}' does not start with '{MODAL_VOLUME_URI_PREFIX}'")
    volume_and_path = uri[len(MODAL_VOLUME_URI_PREFIX) :]
    volume, path = volume_and_path.split("/", 1)
    return volume, path


class ModalMountedVolumeTarget(LocalTarget):
    def __init__(self, path: str, **kwargs):
        super().__init__(path, **kwargs)
        volume_name, in_volume_path = get_volume_name_and_path(path)
        mount_path = modal_config_provider.get().volume_name_to_mount_path.get(
            volume_name
        )
        if mount_path is None:
            raise ValueError(f"Volume '{volume_name}' is not mounted")

        self.volume = modal.Volume.from_name(volume_name)
        self.local_path = mount_path / in_volume_path

    @property
    def _path(self) -> Path:
        return self.local_path

    def _post_write_hook(self) -> None:
        self.volume.commit()


def get_modal_target(path: str) -> ModalMountedVolumeTarget:
    volume_name, in_volume_path = get_volume_name_and_path(uri=path)
    mount_path = modal_config_provider.get().volume_name_to_mount_path.get(volume_name)
    if mount_path is not None:
        return ModalMountedVolumeTarget(path)
    else:
        # TODO implement "remote access" target
        raise NotImplementedError(f"Volume '{volume_name}' is not mounted")
