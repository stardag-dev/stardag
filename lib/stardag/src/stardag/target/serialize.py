import abc
import contextlib
import pickle
import typing
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator

try:
    from typing import Self
except ImportError:
    from typing_extensions import Self

from pydantic import PydanticSchemaGenerationError, TypeAdapter

from stardag.target._base import (
    AIOFileSystemTargetHandle,
    DirectoryTarget,
    FileSystemTarget,
    FileSystemTargetHandle,
    FileTarget,
    LoadableSaveableFileSystemTarget,
    LoadedT,
    OpenMode,
    ReadableAIOFileSystemTargetHandle,
    ReadableFileSystemTargetHandle,
    WritableAIOFileSystemTargetHandle,
    WritableFileSystemTargetHandle,
)

from stardag.utils.resource_provider import resource_provider

TargetT = typing.TypeVar("TargetT", bound=FileSystemTarget)

try:
    from pandas import DataFrame as DataFrame  # type: ignore
    from pandas import read_csv as pd_read_csv  # type: ignore
except ImportError:

    class DataFrame: ...

    def pd_read_csv(*args, **kwargs): ...


@typing.runtime_checkable
class Serializer(typing.Generic[LoadedT, TargetT], typing.Protocol):  # pyright: ignore[reportInvalidTypeVarUse]
    """Protocol for serializers that dump/load objects to/from targets.

    Parameterized by:
        LoadedT: The Python type that is serialized/deserialized.
        TargetT: The target type (``FileTarget`` or ``DirectoryTarget``).

    Concrete serializer base classes:
        - ``FileSerializer[LoadedT]``: For file-backed serialization (most common).
        - ``DirectorySerializer[LoadedT]``: For directory-backed serialization
          (e.g. zarr datasets, ML model checkpoints).
    """

    def dump(
        self,
        obj: LoadedT,
        target: TargetT,
    ) -> None: ...

    def load(
        self,
        target: TargetT,
    ) -> LoadedT: ...

    async def dump_aio(
        self,
        obj: LoadedT,
        target: TargetT,
    ) -> None: ...

    async def load_aio(
        self,
        target: TargetT,
    ) -> LoadedT: ...


# Convenience aliases for the two target-specialized serializer protocols.

FileSerializer = Serializer[LoadedT, FileTarget]
"""Serializer that operates on file targets (the common case)."""

DirectorySerializer = Serializer[LoadedT, DirectoryTarget]
"""Serializer that operates on directory targets (e.g. zarr, ML checkpoints)."""


# Type variable for stream type (str or bytes)
StreamT = typing.TypeVar("StreamT", str, bytes)


class _DumpsLoadsSerializer(typing.Generic[LoadedT, StreamT], abc.ABC):
    """Base class for file serializers that use a dumps/loads pattern.

    Subclasses implement ``dumps()`` and ``loads()`` to convert between
    ``LoadedT`` and a stream type (``str`` or ``bytes``). The base class
    provides ``dump``/``load`` (and async variants) that open the
    ``FileTarget`` in the appropriate mode.
    """

    stream_type: type[StreamT]

    @abc.abstractmethod
    def dumps(self, obj: LoadedT) -> StreamT:
        """Serialize object to string or bytes."""
        ...

    @abc.abstractmethod
    def loads(self, data: StreamT) -> LoadedT:
        """Deserialize object from string or bytes."""
        ...

    @property
    def read_mode(self) -> typing.Literal["r", "rb"]:
        if self.stream_type is str:
            return "r"
        return "rb"

    @property
    def write_mode(self) -> typing.Literal["w", "wb"]:
        if self.stream_type is str:
            return "w"
        return "wb"

    def dump(
        self,
        obj: LoadedT,
        target: FileTarget,
    ) -> None:
        with target.open(self.write_mode) as handle:
            handle.write(self.dumps(obj))  # type: ignore[arg-type]

    def load(self, target: FileTarget) -> LoadedT:
        with target.open(self.read_mode) as handle:
            return self.loads(handle.read())  # type: ignore[arg-type]

    async def dump_aio(
        self,
        obj: LoadedT,
        target: FileTarget,
    ) -> None:
        async with target.open_aio(self.write_mode) as handle:
            await handle.write(self.dumps(obj))  # type: ignore[arg-type]

    async def load_aio(self, target: FileTarget) -> LoadedT:
        async with target.open_aio(self.read_mode) as handle:
            return self.loads(await handle.read())  # type: ignore[arg-type]


class FileSerializable(
    LoadableSaveableFileSystemTarget[LoadedT],
    typing.Generic[LoadedT],
):
    """A file target wrapped with a serializer, providing ``load()``/``save()``."""

    def __init__(
        self,
        wrapped: FileTarget,
        serializer: Serializer[LoadedT, FileTarget],
    ) -> None:
        self.serializer = serializer
        self.wrapped = wrapped

    @property
    def uri(self) -> str:  # type: ignore
        return self.wrapped.uri

    def load(self) -> LoadedT:
        return self.serializer.load(self.wrapped)

    def save(self, obj: LoadedT) -> None:
        self.serializer.dump(obj, self.wrapped)

    def exists(self) -> bool:
        return self.wrapped.exists()

    @typing.overload
    def open(
        self, mode: typing.Literal["r"]
    ) -> ReadableFileSystemTargetHandle[str]: ...

    @typing.overload
    def open(
        self, mode: typing.Literal["rb"]
    ) -> ReadableFileSystemTargetHandle[bytes]: ...

    @typing.overload
    def open(
        self, mode: typing.Literal["w"]
    ) -> WritableFileSystemTargetHandle[str]: ...

    @typing.overload
    def open(
        self, mode: typing.Literal["wb"]
    ) -> WritableFileSystemTargetHandle[bytes]: ...

    def open(self, mode: OpenMode) -> FileSystemTargetHandle:
        return self.wrapped.open(mode)

    @typing.overload
    def open_aio(
        self, mode: typing.Literal["r"]
    ) -> ReadableAIOFileSystemTargetHandle[str]: ...

    @typing.overload
    def open_aio(
        self, mode: typing.Literal["rb"]
    ) -> ReadableAIOFileSystemTargetHandle[bytes]: ...

    @typing.overload
    def open_aio(
        self, mode: typing.Literal["w"]
    ) -> WritableAIOFileSystemTargetHandle[str]: ...

    @typing.overload
    def open_aio(
        self, mode: typing.Literal["wb"]
    ) -> WritableAIOFileSystemTargetHandle[bytes]: ...

    def open_aio(self, mode: OpenMode) -> AIOFileSystemTargetHandle:
        return self.wrapped.open_aio(mode)

    @contextlib.contextmanager
    def _readable_proxy_path(self) -> typing.Generator[Path, None, None]:
        with self.wrapped._readable_proxy_path() as path:
            yield path

    @contextlib.contextmanager
    def _writable_proxy_path(self) -> typing.Generator[Path, None, None]:
        with self.wrapped._writable_proxy_path() as path:
            yield path

    # Async implementations

    async def exists_aio(self) -> bool:
        """Async check - delegates to wrapped target."""
        return await self.wrapped.exists_aio()

    async def load_aio(self) -> LoadedT:
        """Async load - delegates to serializer."""
        return await self.serializer.load_aio(self.wrapped)

    async def save_aio(self, obj: LoadedT) -> None:
        """Async save - delegates to serializer."""
        await self.serializer.dump_aio(obj, self.wrapped)

    @asynccontextmanager
    async def _readable_proxy_path_aio(self) -> AsyncGenerator[Path, None]:
        """Async readable proxy path - delegates to wrapped target."""
        async with self.wrapped._readable_proxy_path_aio() as path:
            yield path

    @asynccontextmanager
    async def _writable_proxy_path_aio(self) -> AsyncGenerator[Path, None]:
        """Async writable proxy path - delegates to wrapped target."""
        async with self.wrapped._writable_proxy_path_aio() as path:
            yield path


class DirectorySerializable(
    LoadableSaveableFileSystemTarget[LoadedT],
    typing.Generic[LoadedT],
):
    """A directory target wrapped with a directory serializer, providing ``load()``/``save()``.

    The serializer's ``dump()`` is responsible for writing sub-targets and
    calling ``target.mark_done()`` when complete.
    """

    def __init__(
        self,
        wrapped: DirectoryTarget,
        serializer: Serializer[LoadedT, DirectoryTarget],
    ) -> None:
        self.serializer = serializer
        self.wrapped = wrapped

    @property
    def uri(self) -> str:  # type: ignore
        return self.wrapped.uri

    def load(self) -> LoadedT:
        return self.serializer.load(self.wrapped)

    def save(self, obj: LoadedT) -> None:
        self.serializer.dump(obj, self.wrapped)
        if not self.wrapped.exists():
            raise RuntimeError(
                f"Directory serializer {type(self.serializer).__name__}.dump() "
                f"did not call target.mark_done(). A directory serializer's "
                f"dump() must call target.mark_done() after writing all "
                f"sub-targets to signal completion."
            )

    def exists(self) -> bool:
        return self.wrapped.exists()

    async def exists_aio(self) -> bool:
        return await self.wrapped.exists_aio()

    async def load_aio(self) -> LoadedT:
        return await self.serializer.load_aio(self.wrapped)

    async def save_aio(self, obj: LoadedT) -> None:
        await self.serializer.dump_aio(obj, self.wrapped)
        if not await self.wrapped.exists_aio():
            raise RuntimeError(
                f"Directory serializer {type(self.serializer).__name__}.dump_aio() "
                f"did not call target.mark_done_aio(). A directory serializer's "
                f"dump_aio() must call target.mark_done_aio() (or target.mark_done()) "
                f"after writing all sub-targets to signal completion."
            )


def is_directory_serializer(
    serializer: Serializer,  # type: ignore[type-arg]
) -> bool:
    """Check if a serializer operates on directory targets.

    Uses the ``target_type`` attribute if present (preferred), otherwise
    inspects the ``dump`` method's ``target`` parameter type hint.
    """
    # Explicit attribute check (preferred)
    target_type = getattr(serializer, "target_type", None)
    if target_type is not None:
        return target_type is DirectoryTarget or (
            isinstance(target_type, type) and issubclass(target_type, DirectoryTarget)
        )

    # Fallback: inspect type hints on dump method
    try:
        hints = typing.get_type_hints(type(serializer).dump)
    except Exception:
        return False

    target_hint = hints.get("target")
    if target_hint is not None:
        origin = typing.get_origin(target_hint)
        if origin is None and isinstance(target_hint, type):
            return issubclass(target_hint, DirectoryTarget)

    return False


class PlainTextSerializer(_DumpsLoadsSerializer[str, str]):
    stream_type = str

    @classmethod
    def type_checked_init(cls, annotation: typing.Type[str]) -> Self:
        if strip_annotation(annotation) != str:  # noqa: E721
            raise ValueError(f"{annotation} must be str.")
        return cls()

    def dumps(self, obj: str) -> str:
        return obj

    def loads(self, data: str) -> str:
        return data

    def get_default_extension(self) -> str:
        return "txt"

    def __eq__(self, value: object) -> bool:
        return type(self) == type(value)  # noqa: E721

    def __hash__(self) -> int:
        return hash(type(self))


class JSONSerializer(_DumpsLoadsSerializer[LoadedT, bytes]):
    stream_type = bytes

    @classmethod
    def type_checked_init(cls, annotation: typing.Type[LoadedT]) -> Self:
        return cls(annotation)

    def __init__(self, annotation: typing.Type[LoadedT]) -> None:
        try:
            self.type_adapter = TypeAdapter(annotation)
        except PydanticSchemaGenerationError as e:
            raise ValueError(f"Failed to generate schema for {annotation}") from e

    def dumps(self, obj: LoadedT) -> bytes:
        return self.type_adapter.dump_json(obj)

    def loads(self, data: bytes) -> LoadedT:
        return self.type_adapter.validate_json(data)

    def get_default_extension(self) -> str:
        return "json"

    def __eq__(self, value: object) -> bool:
        return (
            type(self) == type(value)  # noqa: E721
            and isinstance(value, JSONSerializer)
            and self.type_adapter.core_schema == value.type_adapter.core_schema
        )

    def __hash__(self) -> int:
        return hash((type(self), repr(self.type_adapter.core_schema)))


class PickleSerializer(_DumpsLoadsSerializer[LoadedT, bytes]):
    stream_type = bytes

    @classmethod
    def type_checked_init(cls, annotation: typing.Type[LoadedT]) -> Self:
        # always ok
        return cls()

    def dumps(self, obj: LoadedT) -> bytes:
        return pickle.dumps(obj)

    def loads(self, data: bytes) -> LoadedT:
        return pickle.loads(data)

    def get_default_extension(self) -> str:
        return "pkl"

    def __eq__(self, value: object) -> bool:
        return type(self) == type(value)  # noqa: E721

    def __hash__(self) -> int:
        return hash(type(self))


class PandasDataFrameCSVSerializer(_DumpsLoadsSerializer[DataFrame, str]):
    """Serializer for pandas.DataFrame to CSV.

    NOTE this is mainly a proof of concept. Other formats are recommended for large
    data frames. See e.g.
        https://matthewrocklin.com/blog/work/2015/03/16/Fast-Serialization
    """

    stream_type = str

    @classmethod
    def type_checked_init(cls, annotation: typing.Type[DataFrame]) -> Self:
        if strip_annotation(annotation) != DataFrame:  # noqa: E721
            raise ValueError(f"{annotation} must be DataFrame.")
        return cls()

    def dumps(self, obj: DataFrame) -> str:
        return obj.to_csv(index=True)  # type: ignore

    def loads(self, data: str) -> DataFrame:
        import io

        return pd_read_csv(io.StringIO(data), index_col=0)  # type: ignore

    def get_default_extension(self) -> str:
        return "csv"

    def __eq__(self, value: object) -> bool:
        return type(self) == type(value)  # noqa: E721

    def __hash__(self) -> int:
        return hash(type(self))


@typing.runtime_checkable
class SelfFileSerializing(typing.Protocol):
    """Protocol for objects that know how to serialize/deserialize themselves to a file."""

    def dump(self, target: FileTarget) -> None: ...
    @classmethod
    def load(cls, target: FileTarget) -> Self: ...


@typing.runtime_checkable
class SelfDirectorySerializing(typing.Protocol):
    """Protocol for objects that serialize/deserialize themselves to a directory."""

    def dump(self, target: DirectoryTarget) -> None: ...
    @classmethod
    def load(cls, target: DirectoryTarget) -> Self: ...


class SelfFileSerializer(Serializer[SelfFileSerializing, FileTarget]):
    """Serializer for objects that implement ``SelfFileSerializing``."""

    target_type = FileTarget

    @classmethod
    def type_checked_init(cls, annotation: typing.Type[SelfFileSerializing]) -> Self:
        return cls(strip_annotation(annotation))

    def __init__(self, class_) -> None:
        try:
            is_subclass_ = issubclass(class_, SelfFileSerializing)
        except TypeError:
            raise ValueError(f"{class_} must be a class.")

        if not is_subclass_:
            raise ValueError(
                f"{class_} must comply with the SelfFileSerializing protocol."
            )
        self.class_ = class_

    def dump(
        self,
        obj: SelfFileSerializing,
        target: FileTarget,
    ) -> None:
        obj.dump(target)

    def load(self, target: FileTarget) -> SelfFileSerializing:
        return self.class_.load(target)

    async def dump_aio(
        self,
        obj: SelfFileSerializing,
        target: FileTarget,
    ) -> None:
        obj.dump(target)

    async def load_aio(self, target: FileTarget) -> SelfFileSerializing:
        return self.class_.load(target)

    def get_default_extension(self) -> str | None:
        return getattr(self.class_, "default_serialized_extension", None)

    def __eq__(self, value: object) -> bool:
        return (
            type(self) == type(value)  # noqa: E721
            and isinstance(value, SelfFileSerializer)
            and self.class_ == value.class_
        )

    def __hash__(self) -> int:
        return hash((type(self), self.class_))


class SelfDirectorySerializer(Serializer[SelfDirectorySerializing, DirectoryTarget]):
    """Serializer for objects that implement ``SelfDirectorySerializing``."""

    target_type = DirectoryTarget

    @classmethod
    def type_checked_init(
        cls, annotation: typing.Type[SelfDirectorySerializing]
    ) -> Self:
        stripped = strip_annotation(annotation)
        # Runtime protocol check + verify that dump() actually expects DirectoryTarget
        # (not just any target), to distinguish from SelfFileSerializing.
        try:
            is_subclass_ = issubclass(stripped, SelfDirectorySerializing)
        except TypeError:
            raise ValueError(f"{stripped} must be a class.")
        if not is_subclass_:
            raise ValueError(
                f"{stripped} must comply with the SelfDirectorySerializing protocol."
            )
        # Check type hints to disambiguate from SelfFileSerializing
        try:
            hints = typing.get_type_hints(stripped.dump)
        except Exception as e:
            raise ValueError(
                f"Cannot inspect type hints on {stripped}.dump(). "
                f"Ensure the `target` parameter is annotated as `DirectoryTarget`: {e}"
            ) from e
        target_hint = hints.get("target")
        if target_hint is None or not (
            target_hint is DirectoryTarget
            or (
                isinstance(target_hint, type)
                and issubclass(target_hint, DirectoryTarget)
            )
        ):
            raise ValueError(
                f"{stripped}.dump() target parameter must be DirectoryTarget."
            )
        return cls(stripped)

    def __init__(self, class_) -> None:
        self.class_ = class_

    def dump(
        self,
        obj: SelfDirectorySerializing,
        target: DirectoryTarget,
    ) -> None:
        obj.dump(target)

    def load(self, target: DirectoryTarget) -> SelfDirectorySerializing:
        return self.class_.load(target)

    async def dump_aio(
        self,
        obj: SelfDirectorySerializing,
        target: DirectoryTarget,
    ) -> None:
        obj.dump(target)

    async def load_aio(self, target: DirectoryTarget) -> SelfDirectorySerializing:
        return self.class_.load(target)

    def get_default_extension(self) -> str | None:
        return None

    def __eq__(self, value: object) -> bool:
        return (
            type(self) == type(value)  # noqa: E721
            and isinstance(value, SelfDirectorySerializer)
            and self.class_ == value.class_
        )

    def __hash__(self) -> int:
        return hash((type(self), self.class_))


def strip_annotation(annotation: typing.Type[LoadedT]) -> typing.Type[LoadedT]:
    # TODO complete?
    origin = typing.get_origin(annotation)
    if origin is None:
        return annotation

    if origin == typing.Annotated:
        return typing.get_args(annotation)[0]

    return annotation


class SerializerFactoryProtocol(typing.Protocol):
    @abc.abstractmethod
    def __call__(
        self, annotation: typing.Type[LoadedT]
    ) -> Serializer[LoadedT, typing.Any]: ...


def get_explicitly_annotated_serializer(
    annotation: typing.Type[LoadedT],
) -> Serializer[LoadedT, typing.Any]:
    origin = typing.get_origin(annotation)
    if origin == typing.Annotated:
        args = typing.get_args(annotation)
        for arg in args[1:]:  # NOTE important to skip the first arg
            if isinstance(arg, Serializer):
                return arg

    raise ValueError(f"No explicit serializer found for {annotation}")


_DEFAULT_SERIALIZER_CANDIDATES: tuple[SerializerFactoryProtocol, ...] = (
    get_explicitly_annotated_serializer,
    SelfDirectorySerializer.type_checked_init,  # type: ignore
    SelfFileSerializer.type_checked_init,  # type: ignore
    # specific type serializers
    PandasDataFrameCSVSerializer.type_checked_init,
    PlainTextSerializer.type_checked_init,
    # generic serializers
    JSONSerializer.type_checked_init,
    # fallback
    PickleSerializer.type_checked_init,
)


class SerializerFactory(SerializerFactoryProtocol):
    def __init__(
        self,
        candidates: typing.Iterable[
            SerializerFactoryProtocol
        ] = _DEFAULT_SERIALIZER_CANDIDATES,
    ) -> None:
        self.candidates = candidates

    def __call__(
        self, annotation: typing.Type[LoadedT]
    ) -> Serializer[LoadedT, typing.Any]:
        for candidate in self.candidates:
            try:
                return candidate(annotation)
            except ValueError:
                pass
        raise ValueError(f"No serializer found for {annotation}")


serializer_factory_provider = resource_provider(
    SerializerFactoryProtocol,
    default_factory=SerializerFactory,
    doc_str="Provides a factory for serializers based on type annotations.",
)


def get_serializer(
    annotation: typing.Type[LoadedT],
) -> Serializer[LoadedT, typing.Any]:
    """Get a serializer for the given type annotation.

    Returns a ``Serializer[LoadedT, FileTarget]`` for most types, or a
    ``Serializer[LoadedT, DirectoryTarget]`` when the annotation specifies
    a directory-based serializer (via ``typing.Annotated``).
    """
    return serializer_factory_provider.get()(annotation)
