# -*- coding: utf-8 -*-
import logging
from contextlib import contextmanager
from typing import Callable, Generator, Generic, Type, TypeVar

__all__ = ["ResourceProvider"]

logger = logging.getLogger(__name__)


_ResourceType = TypeVar("_ResourceType")

_ResourceUnset = object()  # Sentinel for unset resource, distinct from None


class ResourceProvider(Generic[_ResourceType]):
    """A generic resource provider that supports lazy initialization."""

    def __init__(self):
        self._resource: _ResourceType = _ResourceUnset  # type: ignore
        self._externally_set_default_factory: Callable[[], _ResourceType] | None = None

    def get(self) -> _ResourceType:
        if self._resource is _ResourceUnset:
            if self._externally_set_default_factory is not None:
                self._resource = self._externally_set_default_factory()
            else:
                self._resource = self.default_factory()
        return self._resource

    def set(self, resource: _ResourceType):
        self._resource = resource

    def is_initialized(self) -> bool:
        """Check if a resource has been set or lazily initialized."""
        return self._resource is not _ResourceUnset

    def clear(self):
        self._resource = _ResourceUnset  # type: ignore

    def __getstate__(self) -> dict:
        # A provider is a process-local handle: its resource (a registry
        # client, config, target factory, ...) must not travel across a
        # process/pickle boundary. This matters most when a provider is
        # captured by a cloudpickled function — e.g. a ``serialized=True``
        # Modal function that references ``registry_provider`` — and
        # deserialized in a fresh container. Serialize *without* the live
        # resource so it re-initializes lazily from the new process's own
        # environment/config.
        state = self.__dict__.copy()
        state.pop("_resource", None)
        return state

    def __setstate__(self, state: dict) -> None:
        self.__dict__.update(state)
        # Bind to THIS process's sentinel so ``get()``'s identity check
        # treats the resource as unset. (A pickled ``object()`` sentinel
        # deserializes to a *distinct* object, so carrying it across would
        # make ``get()`` return the bare sentinel instead of the resource.)
        self._resource = _ResourceUnset  # type: ignore

    def default_factory(self, **kwargs) -> _ResourceType:
        """Needs to be implemented by subclasses.

        NOTE when called by the constructor, kwargs will be empty.
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} does not implement a default_factory"
        )

    def set_default_factory(self, factory: Callable[[], _ResourceType]):
        """Override the default factory for this provider.

        This allows tweaking the default resource creation logic without having to
        instantiate the resource first (keeping it lazily initialized).
        """
        self._externally_set_default_factory = factory

    @contextmanager
    def override(
        self, resource: _ResourceType, context: str | None = None
    ) -> Generator[_ResourceType, None, None]:
        initial = self._resource
        logger.debug(
            f"Overriding resource. Initial: {initial}, new: {resource}, "
            f"context: {context}"
        )
        try:
            self.set(resource)
            yield resource
        finally:
            logger.debug(f"Restoring resource: {initial}, context: {context}")
            self._resource = initial


def resource_provider(
    type_: Type[_ResourceType],
    default_factory: Callable[[], _ResourceType] | None = None,
    doc_str: str = "",
) -> ResourceProvider[_ResourceType]:
    """Functional creation of a ResourceProvider for a specific type.

    Reduces boilerplate for simple default_factory implementations.

    Example:

    ```python
    from stardag.utils.resource_provider import resource_provider

    provider = resource_provider(str, default_factory=lambda: "default")

    # Initialized lazily from default factory
    assert provider.get() == "default"

    # Can be updated persistently
    provider.set("updated")
    assert provider.get() == "updated"

    # Can be overridden temporarily in a context
    with provider.override("overridden"):
        assert provider.get() == "overridden"

    # After the context, the previous resource is restored
    assert provider.get() == "updated"
    ```
    """

    class FunctionalResourceProvider(ResourceProvider[type_]):
        __doc__ = doc_str

        def default_factory(self) -> type_:  # type: ignore
            if default_factory is None:
                raise NotImplementedError(
                    f"{self.__class__.__name__} does not implement a default_factory"
                )
            return default_factory()

    return FunctionalResourceProvider()
