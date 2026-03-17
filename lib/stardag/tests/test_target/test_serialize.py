import typing

import pytest

from stardag.target._base import DirectoryTarget, FileTarget
from stardag.target.serialize import (
    DataFrame,
    DirectorySerializable,
    JSONSerializer,
    PandasDataFrameCSVSerializer,
    PickleSerializer,
    PlainTextSerializer,
    SelfDirectorySerializer,
    SelfDirectorySerializing,
    SelfFileSerializer,
    SelfFileSerializing,
    Serializer,
    get_serializer,
    is_directory_serializer,
)


class _SelfSerializing(SelfFileSerializing):
    def __init__(self, value: str) -> None:
        self.value = value

    def dump(self, target: FileTarget) -> None:
        with target.open("w") as f:
            f.write(str(self.value))

    @classmethod
    def load(cls, target: FileTarget) -> typing.Self:
        with target.open("r") as f:
            return cls(f.read())


class _SelfDirSerializing(SelfDirectorySerializing):
    def __init__(self, data: dict) -> None:
        self.data = data

    def dump(self, target: DirectoryTarget) -> None:
        import json

        with (target / "data.json").open("w") as f:
            f.write(json.dumps(self.data))
        target.mark_done()

    @classmethod
    def load(cls, target: DirectoryTarget) -> typing.Self:
        import json

        with (target / "data.json").open("r") as f:
            return cls(json.loads(f.read()))


class _NoDefaultSerializerType:
    def __init__(self, value: str) -> None:
        self.value = value


class CustomMockSerializer(Serializer[str, FileTarget]):
    def dump(self, obj: str, target: FileTarget) -> None:
        with target.open("w") as f:
            f.write(obj)

    def load(self, target: FileTarget) -> str:
        with target.open("r") as f:
            return f.read()

    async def dump_aio(self, obj: str, target: FileTarget) -> None:
        async with target.open_aio("w") as f:
            await f.write(obj)

    async def load_aio(self, target: FileTarget) -> str:
        async with target.open_aio("r") as f:
            return await f.read()

    def __eq__(self, value: object) -> bool:
        return isinstance(value, CustomMockSerializer)


class TestSerializerHashable:
    """All serializers must be hashable so they work as Annotated args in pydantic generics."""

    @pytest.mark.parametrize(
        "serializer",
        [
            PlainTextSerializer(),
            JSONSerializer(int),
            JSONSerializer(dict[str, int]),
            PickleSerializer(),
            PandasDataFrameCSVSerializer(),
            SelfFileSerializer(_SelfSerializing),
            SelfDirectorySerializer(_SelfDirSerializing),
        ],
    )
    def test_serializer_is_hashable(self, serializer):
        # Must not raise TypeError
        h = hash(serializer)
        assert isinstance(h, int)

    def test_equal_serializers_have_same_hash(self):
        assert hash(PlainTextSerializer()) == hash(PlainTextSerializer())
        assert hash(PickleSerializer()) == hash(PickleSerializer())
        assert hash(JSONSerializer(int)) == hash(JSONSerializer(int))
        assert hash(PandasDataFrameCSVSerializer()) == hash(
            PandasDataFrameCSVSerializer()
        )
        assert hash(SelfFileSerializer(_SelfSerializing)) == hash(
            SelfFileSerializer(_SelfSerializing)
        )
        assert hash(SelfDirectorySerializer(_SelfDirSerializing)) == hash(
            SelfDirectorySerializer(_SelfDirSerializing)
        )

    def test_serializer_usable_in_set_and_dict(self):
        s = PickleSerializer()
        assert s in {s}
        assert {s: 1}[s] == 1

    def test_annotated_with_serializer_is_hashable(self):
        """The motivating use-case: Annotated[T, Serializer()] as a type param."""
        ann = typing.Annotated[str, PickleSerializer()]
        # get_args must not raise when hashing args
        args = typing.get_args(ann)
        for arg in args:
            hash(arg)


@pytest.mark.parametrize(
    "annotation,expected_serializer",
    [
        (str, PlainTextSerializer()),
        (int, JSONSerializer(int)),
        (float, JSONSerializer(float)),
        (dict[str, int], JSONSerializer(dict[str, int])),
        (dict[str, str], JSONSerializer(dict[str, str])),
        (DataFrame, PandasDataFrameCSVSerializer()),
        (_SelfSerializing, SelfFileSerializer(_SelfSerializing)),
        (_SelfDirSerializing, SelfDirectorySerializer(_SelfDirSerializing)),
        (_NoDefaultSerializerType, PickleSerializer()),
        (typing.Annotated[str, CustomMockSerializer()], CustomMockSerializer()),
    ],
)
def test_get_serializer(annotation, expected_serializer):
    serializer = get_serializer(annotation)
    assert serializer == expected_serializer

    extra_annotation = typing.Annotated[annotation, "extra"]
    serializer_from_extra_annotated = get_serializer(extra_annotation)  # type: ignore
    assert serializer_from_extra_annotated == expected_serializer


class TestIsDirectorySerializer:
    def test_file_serializer_returns_false(self):
        assert is_directory_serializer(JSONSerializer(int)) is False

    def test_serializer_with_target_type_attribute(self):
        s = SelfDirectorySerializer(_SelfDirSerializing)
        assert is_directory_serializer(s) is True

    def test_serializer_with_file_target_type_attribute(self):
        s = SelfFileSerializer(_SelfSerializing)
        assert is_directory_serializer(s) is False

    def test_serializer_with_dump_type_hint(self):
        class HintedDirSerializer:
            def dump(self, obj: dict, target: DirectoryTarget) -> None: ...
            def load(self, target: DirectoryTarget) -> dict: ...
            async def dump_aio(self, obj: dict, target: DirectoryTarget) -> None: ...
            async def load_aio(self, target: DirectoryTarget) -> dict: ...

        assert is_directory_serializer(HintedDirSerializer()) is True

    def test_unresolvable_type_hints_returns_false(self):
        """get_type_hints() failure should return False, not crash."""

        class BadSerializer:
            def dump(self, obj, target: "NonExistentType") -> None: ...  # type: ignore  # noqa: F821
            def load(self, target) -> None: ...
            async def dump_aio(self, obj, target) -> None: ...
            async def load_aio(self, target) -> None: ...

        assert is_directory_serializer(BadSerializer()) is False


class TestDirectorySerializable:
    def test_save_and_load(self, default_in_memory_fs_target):
        from stardag.target import get_directory_target

        dt = get_directory_target("test-dir-serializable/")
        serializer = SelfDirectorySerializer(_SelfDirSerializing)
        ds = DirectorySerializable(wrapped=dt, serializer=serializer)

        obj = _SelfDirSerializing({"key": "value"})
        ds.save(obj)
        assert ds.exists()

        loaded = ds.load()
        assert isinstance(loaded, _SelfDirSerializing)
        assert loaded.data == {"key": "value"}

    def test_uri_delegates_to_wrapped(self, default_in_memory_fs_target):
        from stardag.target import get_directory_target

        dt = get_directory_target("test-dir-uri/")
        serializer = SelfDirectorySerializer(_SelfDirSerializing)
        ds = DirectorySerializable(wrapped=dt, serializer=serializer)
        assert ds.uri == dt.uri

    def test_save_raises_if_mark_done_not_called(self, default_in_memory_fs_target):
        from stardag.target import get_directory_target

        class BadSerializer:
            target_type = DirectoryTarget

            def dump(self, obj, target: DirectoryTarget) -> None:
                pass  # does NOT call target.mark_done()

            def load(self, target: DirectoryTarget):
                return None

            async def dump_aio(self, obj, target: DirectoryTarget) -> None:
                pass

            async def load_aio(self, target: DirectoryTarget):
                return None

        dt = get_directory_target("test-dir-no-mark-done/")
        ds = DirectorySerializable(wrapped=dt, serializer=BadSerializer())  # type: ignore[type-var]
        with pytest.raises(RuntimeError, match="did not call target.mark_done"):
            ds.save({"data": 1})  # type: ignore[arg-type]
