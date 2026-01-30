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
    FileSystemTarget,
    FileSystemTargetHandle,
    LoadableSaveableFileSystemTarget,
    LoadedT,
    OpenMode,
    ReadableAIOFileSystemTargetHandle,
    ReadableFileSystemTargetHandle,
    WritableAIOFileSystemTargetHandle,
    WritableFileSystemTargetHandle,
)
from stardag.utils.resource_provider import resource_provider

try:
    from pandas import DataFrame as DataFrame  # type: ignore
    from pandas import read_csv as pd_read_csv  # type: ignore
except ImportError:

    class DataFrame: ...

    def pd_read_csv(*args, **kwargs): ...


@typing.runtime_checkable
class Serializer(typing.Generic[LoadedT], typing.Protocol):
    """Protocol for serializers that can dump/load objects to/from targets."""

    def dump(
        self,
        obj: LoadedT,
        target: FileSystemTarget,
    ) -> None: ...

    def load(
        self,
        target: FileSystemTarget,
    ) -> LoadedT: ...

    async def dump_aio(
        self,
        obj: LoadedT,
        target: FileSystemTarget,
    ) -> None: ...

    async def load_aio(
        self,
        target: FileSystemTarget,
    ) -> LoadedT: ...


# Type variable for stream type (str or bytes)
StreamT = typing.TypeVar("StreamT", str, bytes)


class _DumpsLoadsSerializer(typing.Generic[LoadedT, StreamT], abc.ABC):
    """Base class for serializers that use dumps/loads pattern.

    Provides default implementations of dump/load and dump_aio/load_aio
    using abstract dumps/loads methods.
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
        target: FileSystemTarget,
    ) -> None:
        with target.open(self.write_mode) as handle:
            handle.write(self.dumps(obj))  # type: ignore[arg-type]

    def load(self, target: FileSystemTarget) -> LoadedT:
        with target.open(self.read_mode) as handle:
            return self.loads(handle.read())  # type: ignore[arg-type]

    async def dump_aio(
        self,
        obj: LoadedT,
        target: FileSystemTarget,
    ) -> None:
        async with target.open_aio(self.write_mode) as handle:
            await handle.write(self.dumps(obj))  # type: ignore[arg-type]

    async def load_aio(self, target: FileSystemTarget) -> LoadedT:
        async with target.open_aio(self.read_mode) as handle:
            return self.loads(await handle.read())  # type: ignore[arg-type]


class Serializable(
    LoadableSaveableFileSystemTarget[LoadedT],
    typing.Generic[LoadedT],
):
    def __init__(
        self,
        wrapped: FileSystemTarget,
        serializer: Serializer[LoadedT],
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


@typing.runtime_checkable
class SelfSerializing(typing.Protocol):
    def dump(self, target: FileSystemTarget) -> None: ...
    @classmethod
    def load(cls, target: FileSystemTarget) -> Self: ...


class SelfSerializer(Serializer[SelfSerializing]):
    """Serializer for objects that themselves implement the SelfSerializing protocol."""

    @classmethod
    def type_checked_init(cls, annotation: typing.Type[SelfSerializing]) -> Self:
        return cls(strip_annotation(annotation))

    def __init__(self, class_) -> None:
        try:
            is_subclass_ = issubclass(class_, SelfSerializing)
        except TypeError:
            raise ValueError(f"{class_} must be a class.")

        if not is_subclass_:
            raise ValueError(f"{class_} must comply with the SelfSerializing protocol.")
        self.class_ = class_

    def dump(
        self,
        obj: SelfSerializing,
        target: FileSystemTarget,
    ) -> None:
        obj.dump(target)

    def load(self, target: FileSystemTarget) -> SelfSerializing:
        return self.class_.load(target)

    async def dump_aio(
        self,
        obj: SelfSerializing,
        target: FileSystemTarget,
    ) -> None:
        # Delegate to sync - SelfSerializing doesn't define async methods
        obj.dump(target)

    async def load_aio(self, target: FileSystemTarget) -> SelfSerializing:
        # Delegate to sync - SelfSerializing doesn't define async methods
        return self.class_.load(target)

    def get_default_extension(self) -> str | None:
        return getattr(self.class_, "default_serialized_extension", None)

    def __eq__(self, value: object) -> bool:
        return (
            type(self) == type(value)  # noqa: E721
            and isinstance(value, SelfSerializer)
            and self.class_ == value.class_
        )


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
    def __call__(self, annotation: typing.Type[LoadedT]) -> Serializer[LoadedT]: ...


def get_explicitly_annotated_serializer(
    annotation: typing.Type[LoadedT],
) -> Serializer[LoadedT]:
    origin = typing.get_origin(annotation)
    if origin == typing.Annotated:
        args = typing.get_args(annotation)
        for arg in args[1:]:  # NOTE important to skip the first arg
            if isinstance(arg, Serializer):
                return arg

    raise ValueError(f"No explicit serializer found for {annotation}")


_DEFAULT_SERIALIZER_CANDIDATES: tuple[SerializerFactoryProtocol] = (
    get_explicitly_annotated_serializer,
    SelfSerializer.type_checked_init,  # type: ignore
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

    def __call__(self, annotation: typing.Type[LoadedT]) -> Serializer[LoadedT]:
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


def get_serializer(annotation: typing.Type[LoadedT]) -> Serializer[LoadedT]:
    return serializer_factory_provider.get()(annotation)
