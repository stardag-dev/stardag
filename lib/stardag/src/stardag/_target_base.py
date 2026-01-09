from typing import Protocol, TypeVar, runtime_checkable


@runtime_checkable
class Target(Protocol):
    """Protocol for targets that can report their existence."""

    def exists(self) -> bool:
        """Check if the target exists."""
        ...

    async def exists_aio(self) -> bool:
        """Asynchronously check if the target exists.

        Default implementation delegates to sync exists() method.
        Subclasses can override for true async I/O.
        """
        return self.exists()


TargetType = TypeVar("TargetType", bound=Target, covariant=True)
