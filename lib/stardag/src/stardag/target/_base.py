import abc
import contextlib
import shutil
import tempfile
import typing
from contextlib import asynccontextmanager

import aiofiles
import aiofiles.os
from pydantic_settings import BaseSettings

try:
    from typing import Self
except ImportError:
    from typing_extensions import Self

from pathlib import Path
from types import TracebackType

import uuid6

from stardag._target_base import Target

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

    async def load_aio(self) -> LoadedT_co:
        """Asynchronously load the target content.

        Default implementation delegates to sync load() method.
        """
        return self.load()


@typing.runtime_checkable
class SaveableTarget(
    Target,
    typing.Generic[LoadedT_contra],
    typing.Protocol,
):
    def save(self, obj: LoadedT_contra) -> None: ...

    async def save_aio(self, obj: LoadedT_contra) -> None:
        """Asynchronously save content to the target.

        Default implementation delegates to sync save() method.
        """
        self.save(obj)


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

    @contextlib.contextmanager
    def proxy_path(
        self,
        mode: typing.Literal["r", "w"],
    ) -> typing.Generator[Path, None, None]:
        """Returns a temporary path on the local file system.

        Args:
            mode:
              "r": In read mode, the content of the target is downloaded/transfered to
                the returned local path.
              "w": In write mode, the content of the local path on closing of the
                context is uploaded to the target.
        """
        if mode == "r":
            with self._readable_proxy_path() as path:
                yield path
        elif mode == "w":
            with self._writable_proxy_path() as path:
                yield path
        else:
            raise ValueError(f"Invalid mode {mode}")

    @contextlib.contextmanager
    def _readable_proxy_path(self) -> typing.Generator[Path, None, None]: ...

    @contextlib.contextmanager
    def _writable_proxy_path(self) -> typing.Generator[Path, None, None]: ...

    # Async versions

    async def exists_aio(self) -> bool:
        """Asynchronously check if the target exists.

        Default implementation delegates to sync exists() method.
        Subclasses can override for true async I/O.
        """
        return self.exists()

    @asynccontextmanager
    async def proxy_path_aio(
        self,
        mode: typing.Literal["r", "w"],
    ) -> typing.AsyncGenerator[Path, None]:
        """Async version of proxy_path().

        Returns a temporary path on the local file system.
        """
        if mode == "r":
            async with self._readable_proxy_path_aio() as path:
                yield path
        elif mode == "w":
            async with self._writable_proxy_path_aio() as path:
                yield path
        else:
            raise ValueError(f"Invalid mode {mode}")

    @asynccontextmanager
    async def _readable_proxy_path_aio(self) -> typing.AsyncGenerator[Path, None]:
        """Async version of _readable_proxy_path().

        Default implementation delegates to sync version.
        """
        with self._readable_proxy_path() as path:
            yield path

    @asynccontextmanager
    async def _writable_proxy_path_aio(self) -> typing.AsyncGenerator[Path, None]:
        """Async version of _writable_proxy_path().

        Default implementation delegates to sync version.
        """
        with self._writable_proxy_path() as path:
            yield path

    def __repr__(self):
        return f"{self.__class__.__name__}({self.path})"


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
            return _AtomicWriteFileHandle(  # type: ignore
                self._path,
                mode,
                self._post_write_hook,
            )

        raise ValueError(f"Invalid mode {mode}")

    @contextlib.contextmanager
    def _readable_proxy_path(self) -> typing.Generator[Path, None, None]:
        yield self._path

    @contextlib.contextmanager
    def _writable_proxy_path(self) -> typing.Generator[Path, None, None]:
        tmp_path = self._path.with_suffix(f".tmp-{uuid6.uuid7()}")
        tmp_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            yield tmp_path
            tmp_path.rename(self._path)  # type: ignore
            self._post_write_hook()
        finally:
            if tmp_path.exists():
                tmp_path.unlink()

    def _post_write_hook(self) -> None:
        pass

    # Async implementations

    async def exists_aio(self) -> bool:
        """Asynchronously check if the local file exists."""
        return await aiofiles.os.path.exists(self._path)

    @asynccontextmanager
    async def _readable_proxy_path_aio(self) -> typing.AsyncGenerator[Path, None]:
        """For local files, the proxy path is the file itself."""
        yield self._path

    @asynccontextmanager
    async def _writable_proxy_path_aio(self) -> typing.AsyncGenerator[Path, None]:
        """Async atomic write via temporary file."""
        tmp_path = self._path.with_suffix(f".tmp-{uuid6.uuid7()}")
        await aiofiles.os.makedirs(tmp_path.parent, exist_ok=True)
        try:
            yield tmp_path
            await aiofiles.os.rename(tmp_path, self._path)
            self._post_write_hook()
        finally:
            if await aiofiles.os.path.exists(tmp_path):
                await aiofiles.os.remove(tmp_path)


class _AtomicWriteFileHandle(
    WritableFileSystemTargetHandle[StreamT_contra],
    typing.Generic[StreamT_contra],
):
    def __init__(
        self,
        path: Path,
        mode: OpenMode,
        post_write_hook: typing.Callable[[], None],
    ) -> None:
        self.path = path
        self.mode = mode
        self._tmp_path = self.path.with_suffix(f".tmp-{uuid6.uuid7()}")
        self._tmp_path.parent.mkdir(parents=True, exist_ok=True)
        self._tmp_handle = self._tmp_path.open(self.mode)
        self._post_write_hook = post_write_hook

    def write(self, data: StreamT_contra) -> None:
        self._tmp_handle.write(data)

    def close(self):
        self._tmp_handle.close()
        try:
            self._tmp_path.rename(self.path)  # type: ignore
            self._post_write_hook()
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
                self._post_write_hook()
        finally:
            if self._tmp_path.exists():
                self._tmp_path.unlink()


class RemoteFileSystemABC(metaclass=abc.ABCMeta):
    """*Minimal* interface for a remote file system."""

    URI_PREFIX: str

    @abc.abstractmethod
    def exists(self, uri: str) -> bool: ...

    @abc.abstractmethod
    def download(self, uri: str, destination: Path): ...

    @abc.abstractmethod
    def upload(self, source: Path, uri: str, ok_remove: bool = False): ...

    def enter_readable_proxy_path(self, uri: str) -> Path:
        """Provides a local path for reading the file.

        Exposed only that a cached wrapper can be implemented.
        """
        tmp_dir_path = Path(tempfile.mkdtemp())
        local_path = tmp_dir_path / Path(uri).name
        self.download(uri, local_path)
        return local_path

    def exit_readable_proxy_path(
        self,
        local_path: Path,
    ) -> None:
        if local_path.exists():
            local_path.unlink()
            local_path.parent.rmdir()

    # Async versions - default implementations delegate to sync

    async def exists_aio(self, uri: str) -> bool:
        """Asynchronously check if the file exists.

        Default implementation delegates to sync exists() method.
        Subclasses can override for true async I/O (e.g., aiobotocore for S3).
        """
        return self.exists(uri)

    async def download_aio(self, uri: str, destination: Path) -> None:
        """Asynchronously download a file.

        Default implementation delegates to sync download() method.
        """
        self.download(uri, destination)

    async def upload_aio(self, source: Path, uri: str, ok_remove: bool = False) -> None:
        """Asynchronously upload a file.

        Default implementation delegates to sync upload() method.
        """
        self.upload(source, uri, ok_remove=ok_remove)

    async def enter_readable_proxy_path_aio(self, uri: str) -> Path:
        """Async version of enter_readable_proxy_path().

        Default implementation delegates to sync version.
        """
        tmp_dir_path = Path(tempfile.mkdtemp())
        local_path = tmp_dir_path / Path(uri).name
        await self.download_aio(uri, local_path)
        return local_path

    async def exit_readable_proxy_path_aio(self, local_path: Path) -> None:
        """Async version of exit_readable_proxy_path()."""
        if await aiofiles.os.path.exists(local_path):
            await aiofiles.os.remove(local_path)
            await aiofiles.os.rmdir(local_path.parent)


class RemoteFileSystemTarget(FileSystemTarget):
    def __init__(self, path: str, rfs: RemoteFileSystemABC) -> None:
        if not path.startswith(rfs.URI_PREFIX):
            raise ValueError(
                f"Unexpected URI {path}, expected format {rfs.URI_PREFIX}..."
            )
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

    @contextlib.contextmanager
    def _readable_proxy_path(self) -> typing.Generator[Path, None, None]:
        proxy_path = self.rfs.enter_readable_proxy_path(self.path)
        try:
            yield proxy_path
        finally:
            self.rfs.exit_readable_proxy_path(proxy_path)

    @contextlib.contextmanager
    def _writable_proxy_path(self) -> typing.Generator[Path, None, None]:
        tmp_path = Path(tempfile.mkdtemp()) / Path(self.path).name
        try:
            yield tmp_path
            self.rfs.upload(tmp_path, self.path, ok_remove=True)
        finally:
            if tmp_path.exists():
                tmp_path.unlink()
                tmp_path.parent.rmdir()

    # Async implementations

    async def exists_aio(self) -> bool:
        """Asynchronously check if the remote file exists."""
        return await self.rfs.exists_aio(self.path)

    @asynccontextmanager
    async def _readable_proxy_path_aio(self) -> typing.AsyncGenerator[Path, None]:
        """Async version of _readable_proxy_path()."""
        proxy_path = await self.rfs.enter_readable_proxy_path_aio(self.path)
        try:
            yield proxy_path
        finally:
            await self.rfs.exit_readable_proxy_path_aio(proxy_path)

    @asynccontextmanager
    async def _writable_proxy_path_aio(self) -> typing.AsyncGenerator[Path, None]:
        """Async version of _writable_proxy_path()."""
        tmp_path = Path(tempfile.mkdtemp()) / Path(self.path).name
        try:
            yield tmp_path
            await self.rfs.upload_aio(tmp_path, self.path, ok_remove=True)
        finally:
            if await aiofiles.os.path.exists(tmp_path):
                await aiofiles.os.remove(tmp_path)
                await aiofiles.os.rmdir(tmp_path.parent)


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
        self._proxy_path = self.rfs.enter_readable_proxy_path(self.path)
        self._proxy_handle = self._proxy_path.open(mode)

    def read(self, size: int = -1) -> StreamT_co:
        return self._proxy_handle.read(size)

    def close(self):
        try:
            self._proxy_handle.close()
        finally:
            self.rfs.exit_readable_proxy_path(self._proxy_path)

    def __enter__(self) -> Self:
        self._proxy_handle.__enter__()  # type: ignore
        return self

    def __exit__(
        self,
        type: type[BaseException] | None,
        value: BaseException | None,
        traceback: TracebackType | None,
        /,
    ) -> None:
        try:
            self._proxy_handle.__exit__(type, value, traceback)  # type: ignore
        finally:
            self.rfs.exit_readable_proxy_path(self._proxy_path)


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
        self._tmp_path = Path(tempfile.mkdtemp()) / Path(path).name
        self._tmp_handle = self._tmp_path.open(mode)

    def write(self, data: StreamT_contra) -> None:
        self._tmp_handle.write(data)

    def close(self):
        try:
            self._tmp_handle.close()
            self.rfs.upload(self._tmp_path, self.path, ok_remove=True)
        finally:
            if self._tmp_path.exists():
                self._tmp_path.unlink()
                self._tmp_path.parent.rmdir()

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
                self.rfs.upload(self._tmp_path, self.path, ok_remove=True)
        finally:
            if self._tmp_path.exists():
                self._tmp_path.unlink()
                self._tmp_path.parent.rmdir()


class InMemoryRemoteFileSystem(RemoteFileSystemABC):
    """Mock in-memory implementation of a remote file system."""

    URI_PREFIX = "in-memory://"

    def __init__(self) -> None:
        self.uri_to_bytes = {}

    def exists(self, uri: str) -> bool:
        return uri in self.uri_to_bytes

    def download(self, uri: str, destination: Path):
        with open(destination, "wb") as f:
            f.write(self.uri_to_bytes[uri])

    def upload(self, source: Path, uri: str, ok_remove: bool = False):
        with open(source, "rb") as f:
            self.uri_to_bytes[uri] = f.read()


class CachedRemoteFileSystemConfig(BaseSettings):
    root: str
    root_by_prefix: typing.Dict[str, str] = {}
    allow_cache_check_exists: bool = True


class CachedRemoteFileSystem(RemoteFileSystemABC):
    def __init__(
        self,
        wrapped: RemoteFileSystemABC,
        root: str,
        root_by_prefix: typing.Dict[str, str] | None = None,
        allow_cache_check_exists: bool = True,
    ) -> None:
        self.wrapped = wrapped
        self.root = Path(root)
        self.root_by_prefix = (
            {prefix: Path(root) for prefix, root in root_by_prefix.items()}
            if root_by_prefix is not None
            else {}
        )
        for prefix in self.root_by_prefix:
            if not prefix.startswith(wrapped.URI_PREFIX):
                raise ValueError(
                    f"Unexpected URI prefix {prefix}, "
                    f"expected format {wrapped.URI_PREFIX}..."
                )
        self.allow_cache_check_exists = allow_cache_check_exists

    @property
    def URI_PREFIX(self) -> str:  # type: ignore  # TODO
        return self.wrapped.URI_PREFIX

    def get_cache_path(self, uri: str) -> Path:
        if not uri.startswith(self.wrapped.URI_PREFIX):
            raise ValueError(
                f"Unexpected URI {uri}, expected format {self.wrapped.URI_PREFIX}..."
            )
        relative_key = uri[len(self.wrapped.URI_PREFIX) :]
        for prefix, root in self.root_by_prefix.items():
            if uri.startswith(prefix):
                return root / relative_key

        return self.root / relative_key

    def exists(self, uri: str) -> bool:
        if self.allow_cache_check_exists:
            local_path = self.get_cache_path(uri)
            if local_path.exists():
                return True

        return self.wrapped.exists(uri)

    def download(self, uri: str, destination: Path):
        cache_path = self.get_cache_path(uri)
        if not cache_path.exists():
            self._load_to_cache_atomically(uri, cache_path)

        shutil.copy(cache_path, destination)

    def upload(self, source: Path, uri: str, ok_remove: bool = False):
        self.wrapped.upload(source, uri, ok_remove=False)
        # NOTE only cache the file if the upload was successful!
        cache_path = self.get_cache_path(uri)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        if ok_remove:
            # NOTE faster to rename than to copy!
            source.rename(cache_path)
        else:
            shutil.copy(source, cache_path)

    def enter_readable_proxy_path(self, uri: str) -> Path:
        cache_path = self.get_cache_path(uri)
        if not cache_path.exists():
            self._load_to_cache_atomically(uri, cache_path)

        return cache_path

    def exit_readable_proxy_path(self, local_path: Path) -> None:
        pass

    def _load_to_cache_atomically(self, uri: str, cache_path: Path):
        tmp_cache_path = cache_path.with_suffix(f".tmp-{uuid6.uuid7()}")
        tmp_cache_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.wrapped.download(uri, tmp_cache_path)
            tmp_cache_path.rename(cache_path)  # type: ignore
        finally:
            if tmp_cache_path.exists():
                tmp_cache_path.unlink()

    # Async implementations

    async def exists_aio(self, uri: str) -> bool:
        """Async check with cache optimization."""
        if self.allow_cache_check_exists:
            local_path = self.get_cache_path(uri)
            if await aiofiles.os.path.exists(local_path):
                return True
        return await self.wrapped.exists_aio(uri)

    async def download_aio(self, uri: str, destination: Path) -> None:
        """Async download with cache."""
        cache_path = self.get_cache_path(uri)
        if not await aiofiles.os.path.exists(cache_path):
            await self._load_to_cache_atomically_aio(uri, cache_path)
        await aiofiles.os.makedirs(destination.parent, exist_ok=True)
        shutil.copy(cache_path, destination)  # Local copy is fast, keep sync

    async def upload_aio(self, source: Path, uri: str, ok_remove: bool = False) -> None:
        """Async upload with cache update."""
        await self.wrapped.upload_aio(source, uri, ok_remove=False)
        cache_path = self.get_cache_path(uri)
        await aiofiles.os.makedirs(cache_path.parent, exist_ok=True)
        if ok_remove:
            await aiofiles.os.rename(source, cache_path)
        else:
            shutil.copy(source, cache_path)

    async def enter_readable_proxy_path_aio(self, uri: str) -> Path:
        """Async version returns cached path."""
        cache_path = self.get_cache_path(uri)
        if not await aiofiles.os.path.exists(cache_path):
            await self._load_to_cache_atomically_aio(uri, cache_path)
        return cache_path

    async def exit_readable_proxy_path_aio(self, local_path: Path) -> None:
        """Cache persists, no cleanup needed."""
        pass

    async def _load_to_cache_atomically_aio(self, uri: str, cache_path: Path) -> None:
        """Async atomic download to cache."""
        tmp_cache_path = cache_path.with_suffix(f".tmp-{uuid6.uuid7()}")
        await aiofiles.os.makedirs(tmp_cache_path.parent, exist_ok=True)
        try:
            await self.wrapped.download_aio(uri, tmp_cache_path)
            await aiofiles.os.rename(tmp_cache_path, cache_path)
        finally:
            if await aiofiles.os.path.exists(tmp_cache_path):
                await aiofiles.os.remove(tmp_cache_path)


_FSTargetType = typing.TypeVar("_FSTargetType", bound=FileSystemTarget)


class DirectoryTarget(Target, typing.Generic[_FSTargetType]):
    """A target representing a directory."""

    def __init__(
        self,
        path: str,
        prototype: typing.Type[_FSTargetType] | typing.Callable[[str], _FSTargetType],
    ) -> None:
        self.path = path.removesuffix("/") + "/"
        self.prototype = prototype
        self._flag_target = prototype(self.path[:-1] + "._DONE")
        self._sub_keys = set()

    def exists(self) -> bool:
        return self._flag_target.exists()

    def mark_done(self):
        with self.sub_keys_target().open("w") as f:
            f.write("\n".join(sorted(self._sub_keys)))
        with self._flag_target.open("w") as f:
            f.write("")  # empty file

    def get_sub_target(self, relpath: str) -> _FSTargetType:
        if relpath.startswith("/"):
            raise ValueError(
                f"Invalid relpath {relpath}, not allowed to start with '/'"
            )
        self._sub_keys.add(relpath)
        return self.prototype(self.path + relpath)

    def __truediv__(self, relpath: str) -> _FSTargetType:
        return self.get_sub_target(relpath)

    def sub_keys_target(self) -> _FSTargetType:
        return self.prototype(self.path[:-1] + "._SUB_KEYS")

    def __repr__(self):
        return f"{self.__class__.__name__}({self.path})"

    # Async implementations

    async def exists_aio(self) -> bool:
        """Async check if directory is marked as done."""
        return await self._flag_target.exists_aio()

    async def mark_done_aio(self) -> None:
        """Async version of mark_done()."""
        async with self.sub_keys_target().proxy_path_aio("w") as path:
            async with aiofiles.open(path, "w") as f:
                await f.write("\n".join(sorted(self._sub_keys)))
        async with self._flag_target.proxy_path_aio("w") as path:
            async with aiofiles.open(path, "w") as f:
                await f.write("")  # empty file
