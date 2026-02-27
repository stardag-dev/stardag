import typing

from stardag._core.task import Task
from stardag.polymorphic import Polymorphic, SubClass
from stardag.target._base import LoadedT_co


class _TaskLoads:
    def __class_getitem__(cls, item):
        return SubClass[Task[item]]


if typing.TYPE_CHECKING:
    # For static type checking, this is a generic type alias that can be subscripted
    TaskLoads = typing.Annotated[Task[LoadedT_co], Polymorphic()]
else:
    # At runtime, use a class that properly constructs the Annotated type
    TaskLoads = _TaskLoads
