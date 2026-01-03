from typing import Protocol, TypeVar, runtime_checkable


@runtime_checkable
class Target(Protocol):
    def exists(self) -> bool: ...


TargetType = TypeVar("TargetType", bound=Target, covariant=True)
