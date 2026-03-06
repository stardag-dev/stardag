import typing

import pytest

from stardag.target._base import FileTarget
from stardag.target.serialize import (
    DataFrame,
    JSONSerializer,
    PandasDataFrameCSVSerializer,
    PickleSerializer,
    PlainTextSerializer,
    SelfFileSerializer,
    SelfFileSerializing,
    Serializer,
    get_serializer,
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
