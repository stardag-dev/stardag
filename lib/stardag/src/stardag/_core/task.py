import abc
import asyncio
import functools
import inspect
import logging
from abc import abstractmethod
from collections import abc as collections_abc
from dataclasses import dataclass
from functools import cached_property, total_ordering
from typing import (
    TYPE_CHECKING,
    Any,
    ClassVar,
    Generator,
    Generic,
    Mapping,
    Sequence,
)

if TYPE_CHECKING:
    from stardag.registry_asset import RegistryAsset

from uuid import UUID

from pydantic import ConfigDict, Field, SerializationInfo
from typing_extensions import TypeAlias, Union

from stardag._core.target_base import TargetType
from stardag._core.task_id import _get_task_id_from_jsonable
from stardag.base_model import CONTEXT_MODE_KEY
from stardag.polymorphic import PolymorphicRoot

logger = logging.getLogger(__name__)


TaskStruct: TypeAlias = Union[
    "BaseTask", Sequence["TaskStruct"], Mapping[str, "TaskStruct"]
]


class TaskImplementationError(Exception):
    """Raised when a task class has invalid run/run_aio implementation."""

    pass


def _has_custom_run(task: "BaseTask") -> bool:
    """Check if task has overridden run() (not using default delegation).

    Used to detect whether a task has a custom sync implementation.
    """
    return type(task).run is not BaseTask.run


def _has_custom_run_aio(task: "BaseTask") -> bool:
    """Check if task has overridden run_aio() (not using default delegation).

    Used to detect whether a task has a custom async implementation.
    """
    return type(task).run_aio is not BaseTask.run_aio


def _is_precheck_wrapped(func) -> bool:
    """Check if a function has been wrapped with precheck."""
    return getattr(func, "__precheck_wrapped__", False)


def _wrap_run_with_precheck(func):
    """Wrap run() method with precheck call."""
    if _is_precheck_wrapped(func):
        return func

    @functools.wraps(func)
    def wrapped(self, *args, **kwargs):
        self._check_before_run()
        return func(self, *args, **kwargs)

    wrapped.__precheck_wrapped__ = True  # type: ignore[attr-defined]
    return wrapped


def _wrap_run_aio_with_precheck(func):
    """Wrap run_aio() method with precheck call.

    Handles both regular async functions and async generators.
    """
    if _is_precheck_wrapped(func):
        return func

    if inspect.isasyncgenfunction(func):
        # For async generators, we need an async generator wrapper
        @functools.wraps(func)
        async def wrapped_gen(self, *args, **kwargs):
            self._check_before_run()
            async for item in func(self, *args, **kwargs):
                yield item

        wrapped_gen.__precheck_wrapped__ = True  # type: ignore[attr-defined]
        return wrapped_gen
    else:
        # For regular async functions
        @functools.wraps(func)
        async def wrapped(self, *args, **kwargs):
            self._check_before_run()
            return await func(self, *args, **kwargs)

        wrapped.__precheck_wrapped__ = True  # type: ignore[attr-defined]
        return wrapped


@total_ordering
class BaseTask(
    PolymorphicRoot,
    metaclass=abc.ABCMeta,
):
    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        frozen=True,
        validate_default=True,
    )

    __version__: ClassVar[str] = ""

    version: str = Field(
        default="",
        description="Version of the task run implementation.",
    )

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Validate that subclasses implement either run() or run_aio().

        Also wraps run() and run_aio() methods with precheck validation.
        """
        super().__init_subclass__(**kwargs)

        # Skip validation for abstract classes:
        # - inspect.isabstract() catches classes with unimplemented @abstractmethod
        # - abc.ABC in __bases__ catches classes explicitly marked as abstract base classes
        #   (for intermediate classes meant to be subclassed, not instantiated)
        if inspect.isabstract(cls) or abc.ABC in cls.__bases__:
            return

        # Skip validation for Pydantic parametrized generics (e.g., AutoTask[str])
        # These are internal types created by Pydantic, not user-defined task classes.
        # The heuristic is: class names with '[' are generic specializations.
        if "[" in cls.__name__:
            return

        # Check if any class in the MRO (excluding BaseTask) has run or run_aio
        # This handles inheritance chains and Pydantic's generic class creation
        has_run = any("run" in c.__dict__ and c is not BaseTask for c in cls.__mro__)
        has_run_aio = any(
            "run_aio" in c.__dict__ and c is not BaseTask for c in cls.__mro__
        )

        if not has_run and not has_run_aio:
            raise TaskImplementationError(
                f"Task class '{cls.__name__}' must implement either run() or run_aio(). "
                f"Implement run() for synchronous tasks, or run_aio() for async tasks."
            )

        # Wrap run() and run_aio() with precheck if this class defines them
        if "run" in cls.__dict__:
            attr = cls.__dict__["run"]
            if isinstance(attr, (classmethod, staticmethod)):
                raise TypeError("run() must be an instance method")
            setattr(cls, "run", _wrap_run_with_precheck(attr))

        if "run_aio" in cls.__dict__:
            attr = cls.__dict__["run_aio"]
            if isinstance(attr, (classmethod, staticmethod)):
                raise TypeError("run_aio() must be an instance method")
            setattr(cls, "run_aio", _wrap_run_aio_with_precheck(attr))

    @abstractmethod
    def complete(self) -> bool:
        """Declare if the task is complete."""
        ...

    async def complete_aio(self) -> bool:
        """Asynchronously declare if the task is complete."""
        return self.complete()

    def run(self) -> None | Generator[TaskStruct, None, None]:
        """Execute the task logic (sync).

        Override this method for synchronous tasks. If you only override
        run_aio(), this method will automatically run it via asyncio.run().

        Returns:
            None for simple tasks, or a Generator yielding TaskStruct for
            tasks with dynamic dependencies.

        Dynamic Dependencies Contract:
            When a task yields dynamic dependencies via a generator, the BUILD
            SYSTEM guarantees that ALL yielded tasks are COMPLETE before the
            generator is resumed. The task can rely on this contract:

            ```python
            def run(self):
                # Yield deps we need to be built first
                deps = [TaskA(), TaskB()]
                yield deps

                # CONTRACT: When we reach here, ALL deps are complete.
                # We can safely access their outputs.
                result_a = deps[0].output().load()
                result_b = deps[1].output().load()

                # Yield more deps if needed
                yield [TaskC(input=result_a)]

                # Again, TaskC is complete when we reach here
                self.output().save(final_result)
            ```

            This contract is essential for correctness - tasks can depend on
            previously yielded tasks being complete before continuing execution.

        Raises:
            RuntimeError: If called from within an existing event loop when
                only run_aio() is implemented. In that case, call run_aio()
                directly instead.
            NotImplementedError: If run_aio() is an async generator (dynamic deps).
                Async generators cannot be automatically converted to sync generators.
        """
        if _has_custom_run_aio(self) and not _has_custom_run(self):
            # User only implemented run_aio - run it synchronously
            # Check if it's an async generator (dynamic deps) - can't auto-convert
            # Use unwrapped function since run_aio may be wrapped with precheck
            run_aio_func = getattr(
                type(self).run_aio, "__wrapped__", type(self).run_aio
            )
            if inspect.isasyncgenfunction(run_aio_func):
                raise NotImplementedError(
                    f"{type(self).__name__}.run_aio() is an async generator (uses "
                    f"'yield' for dynamic dependencies), which cannot be automatically "
                    f"converted to a sync run() method. Either:\n"
                    f"  1. Use an async executor that calls run_aio() directly\n"
                    f"  2. Implement run() as a sync generator for sync execution"
                )

            try:
                asyncio.get_running_loop()
            except RuntimeError:
                # No running loop - safe to use asyncio.run()
                return asyncio.run(self.run_aio())
            else:
                # Already in an event loop - can't use asyncio.run()
                raise RuntimeError(
                    f"Cannot call {type(self).__name__}.run() from within an async "
                    f"context. This task only implements run_aio(), which cannot be "
                    f"run synchronously when an event loop is already running. "
                    f"Either:\n"
                    f"  1. Call 'await task.run_aio()' directly instead of 'task.run()'\n"
                    f"  2. Implement run() in your task class for sync execution\n"
                    f"  3. Use 'await asyncio.to_thread(task.run)' from outside this "
                    f"task's async context"
                )
        raise NotImplementedError(
            f"{type(self).__name__} must implement either run() or run_aio()"
        )

    async def run_aio(self) -> None | Generator[TaskStruct, None, None]:
        """Execute the task logic (async).

        Override this method for asynchronous tasks. If you only override
        run(), this method will automatically delegate to it.

        For dynamic dependencies, you can use 'yield' which makes this an
        async generator. Note that async generator methods have different
        type signatures that may require type: ignore comments.

        Returns:
            None for simple tasks, or a Generator/AsyncGenerator for
            tasks with dynamic dependencies.

        Dynamic Dependencies Contract:
            Same as run() - the build system guarantees that ALL yielded tasks
            are COMPLETE before the generator is resumed. See run() docstring
            for detailed documentation and examples.
        """
        if _has_custom_run(self) and not _has_custom_run_aio(self):
            # User only implemented run - delegate to it
            return self.run()
        raise NotImplementedError(
            f"{type(self).__name__} must implement either run() or run_aio()"
        )

    def _check_before_run(self) -> None:
        """Called before run() or run_aio() to validate task state.

        Override this method to add custom pre-run validation.
        By default, validates that the task's version matches the class version.
        """
        if self.version != self.__version__:
            raise ValueError(
                f"Task version mismatch: task instance has version='{self.version}' "
                f"but class {type(self).__name__} has __version__='{self.__version__}'"
            )

    def requires(self) -> TaskStruct | None:
        return None

    def registry_assets(self) -> list["RegistryAsset"]:
        """Return assets to be stored in the registry after task completion.

        Override this method to expose rich outputs (reports, summaries,
        structured data) that will be viewable in the registry UI.

        This method is called after the task completes successfully. It should
        be stateless - loading any required data from the task's target rather
        than relying on in-memory state.

        Returns:
            List of registry assets (MarkdownRegistryAsset, JSONRegistryAsset, etc.)
        """
        return []

    def registry_assets_aio(self) -> list["RegistryAsset"]:
        """Asynchronously return assets to be stored in the registry after task
        completion.
        """
        return self.registry_assets()

    @classmethod
    def has_dynamic_deps(cls) -> bool:
        # Check unwrapped functions since run/run_aio may be wrapped with precheck
        run_func = getattr(cls.run, "__wrapped__", cls.run)
        run_aio_func = getattr(cls.run_aio, "__wrapped__", cls.run_aio)
        return inspect.isgeneratorfunction(run_func) or inspect.isasyncgenfunction(
            run_aio_func
        )

    @cached_property
    def id(self) -> UUID:
        return UUID(
            self.model_dump(
                mode="json",
                context={CONTEXT_MODE_KEY: "hash"},
            )["id"]
        )

    def _hash_mode_finalize(self, data: dict[str, Any], info: SerializationInfo) -> Any:
        """Make hash mode serialization of tasks a container of just their ID."""
        # NOTE: UUID is stringified to match serialization mode "json"
        return {"id": str(_get_task_id_from_jsonable(data))}

    def __lt__(self, other: "BaseTask") -> bool:
        return self.id < other.id

    @classmethod
    def resolve(
        cls: type["BaseTask"],
        namespace: str,
        name: str,
        extra: dict[str, Any],
    ) -> type["BaseTask"]:
        """Override PolymorphicRoot.resolve to handle AliasTask deserialization."""
        aliased = extra.get("__aliased")
        if aliased is not None:
            from stardag._core.alias_task import AliasTask

            return AliasTask

        return super().resolve(namespace, name, extra)


def auto_namespace(scope: str):
    """Set the task namespace for the module to the module import path.

    Usage:
    ```python
    import stardag as sd

    sd.auto_namespace(__name__)

    class MyTask(Task):
        ...
    ```
    """
    module = scope
    BaseTask._registry().add_namespace(module, module)


def namespace(namespace: str, scope: str):
    BaseTask._registry().add_namespace(scope, namespace)


class Task(BaseTask, Generic[TargetType]):
    def complete(self) -> bool:
        """Check if the task is complete."""
        return self.output().exists()

    async def complete_aio(self) -> bool:
        """Asynchronously check if the task is complete."""
        return await self.output().exists_aio()

    @abstractmethod
    def output(self) -> TargetType:
        """The task output target."""
        ...


def flatten_task_struct(task_struct: TaskStruct | None) -> list[BaseTask]:
    """Flatten a TaskStruct into a list of Tasks.

    TaskStruct: TypeAlias = Union[
        "TaskBase", Sequence["TaskStruct"], Mapping[str, "TaskStruct"]
    ]
    """
    if task_struct is None:
        return []

    if isinstance(task_struct, BaseTask):
        return [task_struct]

    if isinstance(task_struct, collections_abc.Sequence):
        return [
            task
            for sub_task_struct in task_struct
            for task in flatten_task_struct(sub_task_struct)
        ]

    if isinstance(task_struct, collections_abc.Mapping):
        return [
            task
            for sub_task_struct in task_struct.values()
            for task in flatten_task_struct(sub_task_struct)
        ]

    ValueError(f"Unsupported task struct type: {task_struct!r}")


@dataclass(frozen=True)
class TaskRef:
    name: str
    version: str | None
    id: UUID

    @classmethod
    def from_task(cls, task: BaseTask) -> "TaskRef":
        return cls(
            name=task.get_name(),
            version=task.version,
            id=task.id,
        )

    @property
    def slug(self) -> str:
        version_slug = f"v{self.version}" if self.version else ""
        return f"{self.name}-{version_slug}-{str(self.id)[:8]}"

    def __str__(self) -> str:
        return self.slug
