from typing import Protocol, TypeVar, runtime_checkable


@runtime_checkable
class Target(Protocol):
    def exists(self) -> bool: ...

    async def exists_aio(self) -> bool:
        """Asynchronously check if the target exists."""
        ...
        return self.exists()


TargetType = TypeVar("TargetType", bound=Target, covariant=True)
