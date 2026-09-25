import abc
import asyncio
import base64
import functools
import inspect
import json
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
    from stardag.artifact import Artifact
    from stardag.registry import RegistryABC

from pickle import loads as pickle_loads
from uuid import UUID

from pydantic import ConfigDict, Field, SerializationInfo, model_validator
from typing_extensions import TypeAlias, Union

from stardag._core.target_base import TargetType
from stardag._core.task_id import _get_task_id_from_jsonable, instance_hash_of_body
from stardag.base_model import CONTEXT_MODE_KEY
from stardag.polymorphic import PolymorphicRoot
from stardag.target._base import LoadedT_co

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
    """The base of every task class.

    A subclass declares a task's parameters as pydantic fields. An object of
    it is a **task object**; what the registry stores is an **instance** — a
    row holding one construction of a task under a deterministic scope. One
    task object planned under two scopes is two instances; one instance
    rehydrates into any number of task objects.

    A task has two identities:

    - :attr:`id`, the **task id**: a hash of the class (namespace, name),
      ``version`` and every *significant* field
      (``StardagField(significant=True)``, the default). It is the promise
      about output — completion and the execution claim are global on it —
      and it names the target.
    - :attr:`instance_hash`: the hash of the canonical instance body, i.e.
      of **all** fields, defaults included. It answers "how exactly was this
      task constructed", and is meaningful only together with the scope it
      is registered under.

    Two task objects with one task id and different instance hashes are two
    ways of asking for one completion; a plan may hold only one of them.
    """

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

    @model_validator(mode="before")
    @classmethod
    def _set_default_version(cls, data: Any) -> Any:
        """Default ``version`` to ``cls.__version__`` when not explicitly provided.

        This eliminates the boilerplate of declaring ``version: str = __version__``
        in every subclass. Existing serialized tasks that carry an explicit
        ``version`` value are unaffected — the validator only fires when the key
        is absent from the input data.
        """
        if isinstance(data, dict) and "version" not in data:
            return {**data, "version": cls.__version__}
        return data

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

        # Skip validation for Pydantic parametrized generics (e.g., Task[str])
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
            tasks with dynamic dependencies (See Dynamic Dependencies Contract below).

        Raises:
            RuntimeError: If called from within an existing event loop when
                only run_aio() is implemented. In that case, call run_aio()
                directly instead.
            NotImplementedError: If run_aio() is an async generator (dynamic deps).
                Async generators cannot be automatically converted to sync generators.


        Dynamic Dependencies Contract:
        When a task yields dynamic dependencies via a generator, the BUILD
        SYSTEM guarantees that ALL yielded tasks are COMPLETE before the
        generator is resumed. The task can rely on this contract:

        ```python
        def run(self):
            # Do some initial work to get info about what additional dependencies
            # are needed
            initial_data = "..."

            # Yield deps we need to be built first
            task_a = TaskA(input=initial_data)
            task_b = TaskB(input=initial_data)
            yield [task_a, task_b]

            # CONTRACT: When we reach here, ALL deps are complete.
            # We can safely access their outputs.
            result_a = task_a.target().load()
            result_b = task_b.target().load()

            # Yield more deps if needed
            task_c = TaskC(input=result_a)
            yield task_c

            # Again, TaskC is complete when we reach here
            self.target().save(task_c.target().load() + result_b)
        ```

        This contract is essential for correctness - tasks can depend on
        previously yielded tasks being complete before continuing execution.
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

    def artifacts(self) -> Sequence["Artifact"]:
        """Return artifacts to be stored in the registry after task completion.

        Override this method to expose rich outputs (reports, summaries,
        structured data) that will be viewable in the registry UI.

        This method is called after the task completes successfully. It should
        be stateless - loading any required data from the task's target rather
        than relying on in-memory state.

        Returns:
            Sequence of artifacts (MarkdownArtifact, JSONArtifact, etc.)
        """
        return []

    async def artifacts_aio(self) -> Sequence["Artifact"]:
        """Asynchronously return artifacts to be stored in the registry after task
        completion.
        """
        return self.artifacts()

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
        """The task id: the completion identity.

        uuid5 over the canonical hash-mode dump — the class discriminators,
        ``version`` and every significant field; a field whose value equals
        its ``compat_default`` is dropped, and a nested task contributes its
        own task id. Custom serializers may special-case the ``"hash"`` mode:
        that is the user's control over completion identity.
        """
        return UUID(
            self.model_dump(
                mode="json",
                context={CONTEXT_MODE_KEY: "hash"},
            )["id"]
        )

    @cached_property
    def _instance_body_json(self) -> str:
        from stardag._core.instance import canonical_instance_body_json

        return canonical_instance_body_json(self)

    def instance_body(self) -> dict[str, Any]:
        """The instance body: the registry-mode dump of every field, defaults
        included, nested tasks as their full bodies — parsed from the same
        canonical JSON that :attr:`instance_hash` hashes, so what is sent is
        exactly what was hashed. A fresh dict on every call.
        """
        return json.loads(self._instance_body_json)

    @cached_property
    def instance_hash(self) -> UUID:
        """The hash of the canonical instance body (all parameters).

        Not an identifier on its own: an instance is a registry row keyed by
        its scope *and* this hash, so the same hash under two scopes is two
        instances. Use :attr:`id` for "which task", and the scope together
        with this for "which instance". Has no hash mode of its own; custom
        serializers affect it only through ordinary serialization, which
        must be stable (see
        ``stardag._core.instance.check_serialization_stability``).
        """
        return instance_hash_of_body(self._instance_body_json)

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

            loads_type_pickled_b64 = aliased.get("loads_type")
            if loads_type_pickled_b64 is None:
                raise ValueError(
                    "Missing 'loads_type' in '__aliased' data for AliasTask "
                    "deserialization. Ensure that the serialized data includes the "
                    "'loads_type' field."
                )
            loads_type = pickle_loads(base64.b64decode(loads_type_pickled_b64))
            return AliasTask[loads_type]

        return super().resolve(namespace, name, extra)

    @classmethod
    def from_registry(
        cls,
        id: UUID | str,
        registry: Union["RegistryABC", None] = None,
    ) -> "BaseTask":
        """Instantiate the task from the registry.

        A task id may have several instances — constructions under
        different scopes — any of which rehydrates into this completion. A
        task has no parameters, an instance does, so without a scope to
        narrow by, this takes the newest instance's body (``TaskInfo``
        orders them newest first).

        Validated in compat mode, same as ``task_from_registry_data``: the
        recomputed task id is checked against the requested one, so a
        removed or renamed significant field — dropped by compat mode's
        lenient rules for non-significant fields — cannot silently return a
        task with a different completion identity than the one asked for.

        Args:
            id: The UUID (or string representation) of the task to load.
            registry: An optional registry instance to use for loading metadata. If not
                provided, the default registry from `registry_provider` will be used.
        Returns:
            The reconstructed task object.

        Raises:
            TaskRehydrationError: The recomputed task id does not match the
                requested one.
        """
        # Local import: rehydrate.py imports base_task at module level, so
        # this must stay a lazy, in-method import to avoid a circular import
        # at module load time.
        from stardag._core.rehydrate import TaskRehydrationError
        from stardag.registry import registry_provider

        if isinstance(id, str):
            id = UUID(id)

        registry = registry or registry_provider.get()
        info = registry.task_get(str(id))
        if info.body is None:
            raise TaskRehydrationError(
                f"The registry holds no instance body for task {id}."
            )

        task = cls.model_validate(info.body, context={CONTEXT_MODE_KEY: "compat"})
        if task.id != id:
            raise TaskRehydrationError(
                f"Rehydrated task id {task.id} does not match the requested "
                f"id {id} — a field's serialization is likely not "
                "losslessly round-trippable, or a significant field the "
                "class no longer declares was silently dropped by compat "
                "mode's lenient rules for non-significant fields."
            )
        return task


def auto_namespace(scope: str):
    """Set the task namespace for the module to the module import path.

    Args:
        scope: The module scope, typically passed as `__name__`.

    Usage:

    ```python
    import stardag as sd

    sd.auto_namespace(__name__)

    class MyAutoNamespacedTask(sd.Task[int]):
        a: int

        def run(self):
            self._save(self.a)

    assert MyAutoNamespacedTask.get_namespace() == __name__
    ```
    """
    module = scope
    BaseTask._registry().add_namespace(module, module)


def namespace(namespace: str, scope: str):
    """Set the task namespace for the module and any submodules.

    Args:
        namespace: The namespace to set for the module.
        scope: The module scope, typically passed as `__name__`.

    Usage:

    ```python
    import stardag as sd
    sd.namespace("my_custom_namespace", __name__)

    class MyNamespacedTask(sd.Task[int]):
        a: int

        def run(self):
            self._save(self.a)

    assert MyNamespacedTask.get_namespace() == "my_custom_namespace"
    """
    BaseTask._registry().add_namespace(scope, namespace)


def _has_custom_load(task: "LoadableTask") -> bool:  # type: ignore[type-arg]
    """Check if task has overridden load() (not using default delegation)."""
    return type(task).load is not LoadableTask.load


def _has_custom_load_aio(task: "LoadableTask") -> bool:  # type: ignore[type-arg]
    """Check if task has overridden load_aio() (not using default delegation)."""
    return type(task).load_aio is not LoadableTask.load_aio


class LoadableTask(BaseTask, abc.ABC, Generic[LoadedT_co]):
    """A task that can load its output as a typed value.

    This is the minimal interface required by :class:`~stardag.TaskLoads`: any
    ``BaseTask`` subclass that implements ``load() -> T`` is compatible with
    ``TaskLoads[T]``.

    Both :class:`~stardag.Task` (via diamond inheritance) and bare subclasses
    of ``LoadableTask`` satisfy ``TaskLoads[T]``.

    Subclasses must implement at least one of ``load()`` or ``load_aio()``.
    The missing method will delegate to the other automatically (mirroring
    the ``run``/``run_aio`` pattern on ``BaseTask``).
    """

    __stardag_abstract__: ClassVar[bool] = True

    def load(self) -> LoadedT_co:
        """Load the output of this task (sync).

        If only ``load_aio()`` is implemented, this delegates via
        ``asyncio.run()``. Raises ``RuntimeError`` if called from within
        an existing event loop.
        """
        if _has_custom_load_aio(self) and not _has_custom_load(self):
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                return asyncio.run(self.load_aio())
            else:
                raise RuntimeError(
                    f"Cannot call {type(self).__name__}.load() from within an async "
                    f"context when only load_aio() is implemented. "
                    f"Use 'await task.load_aio()' instead."
                )
        raise NotImplementedError(
            f"{type(self).__name__} must implement either load() or load_aio()"
        )

    async def load_aio(self) -> LoadedT_co:
        """Asynchronously load the output of this task.

        If only ``load()`` is implemented, this delegates to it.
        """
        if _has_custom_load(self) and not _has_custom_load_aio(self):
            return self.load()
        raise NotImplementedError(
            f"{type(self).__name__} must implement either load() or load_aio()"
        )


class TargetTask(BaseTask, Generic[TargetType]):
    """Base class for tasks that produce a target output.

    Extends BaseTask with a typed ``target()`` method and a default ``complete()``
    implementation that checks whether the target exists.

    Most users should subclass :class:`~stardag.Task` (which extends this class
    with automatic serialization and filesystem target management) rather than
    using ``TargetTask`` directly.
    """

    __stardag_abstract__: ClassVar[bool] = True

    def complete(self) -> bool:
        """Check if the task is complete."""
        return self.target().exists()

    async def complete_aio(self) -> bool:
        """Asynchronously check if the task is complete."""
        return await self.target().exists_aio()

    @abstractmethod
    def target(self) -> TargetType:
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

    raise ValueError(f"Unsupported task struct type: {task_struct!r}")


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
