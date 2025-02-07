import abc
import tempfile
import typing

try:
    from typing import Self
except ImportError:
    from typing_extensions import Self

from pathlib import Path
from types import TracebackType

import uuid6


@typing.runtime_checkable
class Target(typing.Protocol):
    def exists(self) -> bool: ...


LoadedT = typing.TypeVar("LoadedT")
LoadedT_co = typing.TypeVar("LoadedT_co", covariant=True)
LoadedT_contra = typing.TypeVar("LoadedT_contra", contravariant=True)


@typing.runtime_checkable
class LoadableTarget(
    Target,
    typing.Generic[LoadedT_co],
    typing.Protocol,
):
    def load(self) -> LoadedT_co: ...


@typing.runtime_checkable
class SaveableTarget(
    Target,
    typing.Generic[LoadedT_contra],
    typing.Protocol,
):
    def save(self, obj: LoadedT_contra) -> None: ...


@typing.runtime_checkable
class LoadableSaveableTarget(
    LoadableTarget[LoadedT],
    SaveableTarget[LoadedT],
    typing.Generic[LoadedT],
    typing.Protocol,
): ...


StreamT = typing.TypeVar("StreamT", bound=typing.Union[str, bytes])
StreamT_co = typing.TypeVar(
    "StreamT_co", bound=typing.Union[str, bytes], covariant=True
)
StreamT_contra = typing.TypeVar(
    "StreamT_contra", bound=typing.Union[str, bytes], contravariant=True
)

OpenMode = typing.Literal["r", "w", "rb", "wb"]


@typing.runtime_checkable
class FileSystemTargetHandle(typing.Protocol):
    def close(self) -> None: ...
    def __enter__(self) -> Self: ...
    def __exit__(
        self,
        type: type[BaseException] | None,
        value: BaseException | None,
        traceback: TracebackType | None,
        /,
    ) -> None: ...


@typing.runtime_checkable
class ReadableFileSystemTargetHandle(
    FileSystemTargetHandle,
    typing.Generic[StreamT_co],
    typing.Protocol,
):
    def read(self, size: int = -1) -> StreamT_co: ...


@typing.runtime_checkable
class WritableFileSystemTargetHandle(
    FileSystemTargetHandle,
    typing.Generic[StreamT_contra],
    typing.Protocol,
):
    def write(self, data: StreamT_contra) -> None: ...


BytesT = typing.TypeVar("BytesT", bound=bytes)


class _FileSystemTargetGeneric(
    Target,
    typing.Generic[BytesT],
    typing.Protocol,
):
    path: str

    def __init__(self, path: str) -> None:
        self.path = path

    def exists(self) -> bool: ...

    @typing.overload
    def open(
        self, mode: typing.Literal["r"]
    ) -> ReadableFileSystemTargetHandle[str]: ...

    @typing.overload
    def open(
        self, mode: typing.Literal["rb"]
    ) -> ReadableFileSystemTargetHandle[BytesT]: ...

    @typing.overload
    def open(
        self, mode: typing.Literal["w"]
    ) -> WritableFileSystemTargetHandle[str]: ...

    @typing.overload
    def open(
        self, mode: typing.Literal["wb"]
    ) -> WritableFileSystemTargetHandle[BytesT]: ...

    def open(self, mode: OpenMode) -> FileSystemTargetHandle:
        """For convenience, subclasses of FileSystemTarget can implement the private
        method _open without type hints to not having to repeat the overload:s."""
        return self._open(mode=mode)  # type: ignore


class FileSystemTarget(_FileSystemTargetGeneric[bytes], typing.Protocol):
    pass


class LoadableSaveableFileSystemTarget(
    LoadableSaveableTarget[LoadedT],
    _FileSystemTargetGeneric[bytes],
    typing.Generic[LoadedT],
    typing.Protocol,
): ...


LSFST = LoadableSaveableFileSystemTarget


class LocalTarget(FileSystemTarget):
    """TODO use luigi-style atomic writes."""

    def __init__(self, path: str) -> None:
        self.path = path

    @property
    def _path(self) -> Path:
        return Path(self.path)

    def exists(self) -> bool:
        return self._path.exists()

    def _open(self, mode: OpenMode) -> FileSystemTargetHandle:  # type: ignore
        if mode in ["r", "rb"]:
            return self._path.open(mode)
        if mode in ["w", "wb"]:
            return _AtomicWriteFileHandle(self._path, mode)  # type: ignore

        raise ValueError(f"Invalid mode {mode}")


class _AtomicWriteFileHandle(
    WritableFileSystemTargetHandle[StreamT_contra],
    typing.Generic[StreamT_contra],
):
    def __init__(self, path: Path, mode: OpenMode) -> None:
        self.path = path
        self.mode = mode
        self._tmp_path = self.path.with_suffix(f".tmp-{uuid6.uuid7()}")
        self._tmp_handle = self._tmp_path.open(self.mode)

    def write(self, data: StreamT_contra) -> None:
        self._tmp_handle.write(data)

    def close(self):
        self._tmp_handle.close()
        try:
            self._tmp_path.rename(self.path)  # type: ignore
        finally:
            if self._tmp_path.exists():
                self._tmp_path.unlink()

    def __enter__(self) -> Self:
        self._tmp_handle.__enter__()  # type: ignore
        return self

    def __exit__(
        self,
        type: type[BaseException] | None,
        value: BaseException | None,
        traceback: TracebackType | None,
        /,
    ) -> None:
        try:
            self._tmp_handle.__exit__(type, value, traceback)  # type: ignore
            if type is None:
                self._tmp_path.rename(self.path)  # type: ignore
        finally:
            if self._tmp_path.exists():
                self._tmp_path.unlink()


class RemoteFileSystemABC(metaclass=abc.ABCMeta):
    """*Minimal* interface for a remote file system."""

    @abc.abstractmethod
    def exists(self, uri: str) -> bool: ...

    @abc.abstractmethod
    def download(self, uri: str, destination: Path): ...

    @abc.abstractmethod
    def upload(self, source: Path, uri: str): ...

    def get_local_readable_path(self, uri: str) -> Path:
        """Provides a local path for reading the file.

        Exposed only that a cached wrapper can be implemented.
        """
        tmp_dir_path = Path(tempfile.mkdtemp())
        local_path = tmp_dir_path / Path(uri).name
        self.download(uri, local_path)
        return local_path

    def cleanup_local_readable_path(self, local_path: Path) -> None:
        local_path.unlink()
        local_path.parent.rmdir()


class RemoteFileSystemTarget(FileSystemTarget):
    def __init__(self, path: str, rfs: RemoteFileSystemABC) -> None:
        self.path = path
        self.rfs = rfs

    def exists(self) -> bool:
        return self.rfs.exists(self.path)

    def _open(self, mode: OpenMode) -> FileSystemTargetHandle:  # type: ignore
        if mode in ["r", "rb"]:
            return _RemoteReadFileHandle(self.path, mode, self.rfs)  # type: ignore
        if mode in ["w", "wb"]:
            return _RemoteWriteFileHandle(self.path, mode, self.rfs)  # type: ignore

        raise ValueError(f"Invalid mode {mode}")  # pragma: no cover


class _RemoteReadFileHandle(
    ReadableFileSystemTargetHandle[StreamT_co],
    typing.Generic[StreamT_co],
):
    def __init__(
        self,
        path: str,
        mode: typing.Literal["r", "rb"],
        rfs: RemoteFileSystemABC,
    ) -> None:
        self.path = path
        self.rfs = rfs
        if mode not in ["r", "rb"]:
            raise ValueError(f"Invalid mode {mode}")
        self.mode = mode
        self._local_path = self.rfs.get_local_readable_path(self.path)
        self._local_handle = self._local_path.open(mode)

    def read(self, size: int = -1) -> StreamT_co:
        return self._local_handle.read(size)

    def close(self):
        try:
            self._local_handle.close()
        finally:
            self.rfs.cleanup_local_readable_path(self._local_path)

    def __enter__(self) -> Self:
        self._local_handle.__enter__()  # type: ignore
        return self

    def __exit__(
        self,
        type: type[BaseException] | None,
        value: BaseException | None,
        traceback: TracebackType | None,
        /,
    ) -> None:
        try:
            self._local_handle.__exit__(type, value, traceback)  # type: ignore
        finally:
            self.rfs.cleanup_local_readable_path(self._local_path)


class _RemoteWriteFileHandle(
    WritableFileSystemTargetHandle[StreamT_contra],
    typing.Generic[StreamT_contra],
):
    def __init__(
        self,
        path: str,
        mode: typing.Literal["w", "wb"],
        rfs: RemoteFileSystemABC,
    ) -> None:
        self.path = path
        self.rfs = rfs
        self._tmp_path = Path(f"/tmp/{uuid6.uuid7()}")
        self._tmp_handle = self._tmp_path.open(mode)

    def write(self, data: StreamT_contra) -> None:
        self._tmp_handle.write(data)

    def close(self):
        try:
            self._tmp_handle.close()
            self.rfs.upload(self._tmp_path, self.path)
        finally:
            if self._tmp_path.exists():
                self._tmp_path.unlink()

    def __enter__(self) -> Self:
        self._tmp_handle.__enter__()  # type: ignore
        return self

    def __exit__(
        self,
        type: type[BaseException] | None,
        value: BaseException | None,
        traceback: TracebackType | None,
        /,
    ) -> None:
        try:
            self._tmp_handle.__exit__(type, value, traceback)  # type: ignore
            if type is None:
                self.rfs.upload(self._tmp_path, self.path)
        finally:
            if self._tmp_path.exists():
                self._tmp_path.unlink()


class MockRemoteFileSystem(RemoteFileSystemABC):
    def __init__(self) -> None:
        self.uri_to_bytes = {}

    def exists(self, uri: str) -> bool:
        return uri in self.uri_to_bytes

    def download(self, uri: str, destination: Path):
        with open(destination, "wb") as f:
            f.write(self.uri_to_bytes[uri])

    def upload(self, source: Path, uri: str):
        with open(source, "rb") as f:
            self.uri_to_bytes[uri] = f.read()
