"""Validation framework for Task load/save operations.

Provides ``LoadValidator``, an abstract base class for validators that run
automatically when data passes through ``Task._save()`` and ``Task.load()``.

Validators are attached to the ``LoadedT`` type parameter via
``typing.Annotated``::

    from stardag import Task, LoadValidator

    class NonEmpty(LoadValidator[str]):
        def validate(self, value: str) -> str:
            if not value:
                raise ValueError("Value must not be empty")
            return value

    class MyTask(Task[Annotated[str, NonEmpty()]]):
        def run(self):
            self._save("hello")

Multiple validators in the same ``Annotated`` are executed in order.
"""

import abc
import typing

LoadedT = typing.TypeVar("LoadedT")


class LoadValidator(typing.Generic[LoadedT], abc.ABC):
    """Abstract base class for validators on ``Task[Annotated[T, ...]]``.

    Subclass this and implement :meth:`validate` to create a validator.
    Instances placed in ``typing.Annotated`` metadata are automatically
    discovered and executed on :meth:`Task._save` and :meth:`Task.load`.

    The ``validate`` method receives the value and must return the
    (possibly transformed) value, or raise an exception to reject it.

    Example::

        class HasPrefix(LoadValidator[str]):
            def __init__(self, prefix: str) -> None:
                self.prefix = prefix

            def validate(self, value: str) -> str:
                if not value.startswith(self.prefix):
                    raise ValueError(
                        f"Expected value to start with {self.prefix!r}, "
                        f"got {value!r}"
                    )
                return value

        class MyTask(Task[Annotated[str, HasPrefix("x")]]):
            ...
    """

    @abc.abstractmethod
    def validate(self, value: LoadedT) -> LoadedT:
        """Validate and optionally transform the value.

        Args:
            value: The data being saved or loaded.

        Returns:
            The validated (and optionally transformed) value.

        Raises:
            Exception: If validation fails.
        """
        ...


def get_validators(
    annotation: typing.Any,
) -> tuple[LoadValidator, ...]:
    """Extract ``LoadValidator`` instances from a ``typing.Annotated`` type.

    Args:
        annotation: A type annotation, possibly ``Annotated[T, ...]``.

    Returns:
        A tuple of ``LoadValidator`` instances found in the annotation
        metadata, in the order they appear. Returns an empty tuple if
        the annotation is not ``Annotated`` or contains no validators.
    """
    origin = typing.get_origin(annotation)
    if origin is not typing.Annotated:
        return ()
    args = typing.get_args(annotation)
    return tuple(arg for arg in args[1:] if isinstance(arg, LoadValidator))


def run_validators(
    validators: tuple[LoadValidator, ...],
    value: LoadedT,
) -> LoadedT:
    """Run a sequence of validators on a value.

    Args:
        validators: The validators to apply, in order.
        value: The data to validate.

    Returns:
        The value after all validators have been applied.

    Raises:
        Exception: If any validator rejects the value.
    """
    for validator in validators:
        value = validator.validate(value)
    return value
