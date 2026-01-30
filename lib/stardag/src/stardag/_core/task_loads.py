import typing

from stardag._core.task import Task
from stardag.polymorphic import Polymorphic, SubClass
from stardag.target._base import LoadableTarget, LoadedT_co


class _TaskLoads:
    def __class_getitem__(cls, item):
        return SubClass[Task[LoadableTarget[item]]]


if typing.TYPE_CHECKING:
    # For static type checking, this is a generic type alias that can be subscripted
    TaskLoads = typing.Annotated[Task[LoadableTarget[LoadedT_co]], Polymorphic()]
else:
    # At runtime, use a class that properly constructs the Annotated type
    TaskLoads = _TaskLoads
