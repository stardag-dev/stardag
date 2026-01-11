from pathlib import Path

import modal
from grpclib import GRPCError, Status
from modal.volume import FileEntryType

from stardag.integration.modal._config import modal_config_provider
from stardag.target import LocalTarget, RemoteFileSystemABC, RemoteFileSystemTarget
from stardag.utils.resource_provider import resource_provider

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


class ModalVolumeRemoteFileSystem(RemoteFileSystemABC):
    URI_PREFIX = MODAL_VOLUME_URI_PREFIX

    def exists(self, uri: str) -> bool:
        volume_name, in_volume_path = get_volume_name_and_path(uri)
        volume = modal.Volume.from_name(volume_name)

        try:
            entry = next(volume.iterdir(in_volume_path))
            if entry.type == FileEntryType.FILE and entry.path == in_volume_path:
                return True
            else:
                return False

        # This is what happens when running in modal function
        except GRPCError as exc:
            if exc.status == Status.NOT_FOUND:
                return False

            raise

        # This is what happens when running outside of modal functions
        except StopIteration:
            # The prefix is a directory, with no entries
            return False
        except FileNotFoundError:
            # No entry found
            return False

    def download(self, uri: str, destination: Path):
        volume_name, in_volume_path = get_volume_name_and_path(uri)
        volume = modal.Volume.from_name(volume_name)
        with destination.open("wb") as dest_handle:
            volume.read_file_into_fileobj(in_volume_path, dest_handle)

    def upload(self, source: Path, uri: str, ok_remove: bool = False):
        volume_name, in_volume_path = get_volume_name_and_path(uri)
        volume = modal.Volume.from_name(volume_name)
        with volume.batch_upload() as batch:
            batch.put_file(source, in_volume_path)

    # Async implementations using Modal's .aio interface

    async def exists_aio(self, uri: str) -> bool:
        """Asynchronously check if the file exists in the Modal volume."""
        volume_name, in_volume_path = get_volume_name_and_path(uri)
        volume = modal.Volume.from_name(volume_name)

        try:
            async for entry in volume.iterdir.aio(in_volume_path):
                if entry.type == FileEntryType.FILE and entry.path == in_volume_path:
                    return True
                return False
            return False

        except GRPCError as exc:
            if exc.status == Status.NOT_FOUND:
                return False
            raise

        except FileNotFoundError:
            return False

    async def download_aio(self, uri: str, destination: Path) -> None:
        """Asynchronously download a file from the Modal volume."""
        import aiofiles

        volume_name, in_volume_path = get_volume_name_and_path(uri)
        volume = modal.Volume.from_name(volume_name)

        # Modal's read_file returns an iterator of bytes chunks
        # We need to write them to the destination file
        async with aiofiles.open(destination, "wb") as dest_handle:
            async for chunk in volume.read_file.aio(in_volume_path):
                await dest_handle.write(chunk)

    async def upload_aio(self, source: Path, uri: str, ok_remove: bool = False) -> None:
        """Asynchronously upload a file to the Modal volume."""
        volume_name, in_volume_path = get_volume_name_and_path(uri)
        volume = modal.Volume.from_name(volume_name)

        # Modal's batch_upload is a context manager, use the async version
        async with volume.batch_upload.aio() as batch:
            batch.put_file(source, in_volume_path)


modal_volume_rfs_provider = resource_provider(
    RemoteFileSystemABC, ModalVolumeRemoteFileSystem
)


def get_modal_target(path: str) -> ModalMountedVolumeTarget | RemoteFileSystemTarget:
    volume_name, in_volume_path = get_volume_name_and_path(uri=path)
    mount_path = modal_config_provider.get().volume_name_to_mount_path.get(volume_name)
    if mount_path is not None:
        return ModalMountedVolumeTarget(path)
    else:
        return RemoteFileSystemTarget(path, rfs=modal_volume_rfs_provider.get())
