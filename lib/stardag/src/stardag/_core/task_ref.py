"""Task references: a typed, lazily hydrated handle to a registered task.

``MyTask.ref(task_id, instance_id=...)`` names a task the registry already
holds — usually one already materialized, such as a trained model — by its
task id, instead of re-composing it from factories and defaults that keep
evolving. The registry's stored instance body, rehydrated in compat mode,
is the record; code holds a :class:`TaskRef` to it.

A reference is a **driver-side placeholder**:

- Constructing one makes no registry call. Its task id is the pinned one,
  and its hash-mode dump is ``{"id": task_id}`` — exactly what a real task
  contributes to a parent's hash — so a downstream task has the same id
  whether it is given the reference or the real task.
- :meth:`TaskRef.hydrate` reads the instance body from the registry (as
  ``from_registry`` does, a pinned reference from the local body cache,
  but refusing ``AliasTask`` payloads, which a build-time load must never
  unpickle) and returns the real task object, checked against the
  referenced class and the task id. ``complete()``, ``target()`` and ``load()``
  delegate to it.
- Every build replaces every reference in its DAG with the hydrated task
  before discovery (:func:`resolve_refs`), so the registry only ever stores
  real bodies, and workers and ``run()`` only ever see real tasks. A
  reference that reaches an instance body unhydrated is refused
  (:class:`TaskRefNotHydratedError`).
"""

from __future__ import annotations

import asyncio
import threading
import typing
import warnings
import weakref
from collections.abc import Mapping
from functools import cached_property
from typing import TYPE_CHECKING, Any, ClassVar, Generic, TypeVar
from uuid import UUID

from pydantic import BaseModel, SerializationInfo, field_serializer, field_validator

from stardag._core.base_task import BaseTask, LoadableTask, TargetTask, TaskSlug
from stardag._core.task import LoadedT, Task
from stardag.base_model import CONTEXT_MODE_KEY
from stardag.exceptions import StardagError
from stardag.target import LoadableSaveableFileSystemTarget

if TYPE_CHECKING:
    from stardag.registry import RegistryABC

TaskT = TypeVar("TaskT", bound=BaseTask)


class TaskRefNotHydratedError(StardagError):
    """A :class:`TaskRef` was used where only a real task can go: dumped
    into an instance body (registration, a Modal payload) or run.

    Builds hydrate every reference before discovery, so reaching this means
    a reference got past that step; call ``.hydrate()`` on it first, or
    pass it to ``sd.build``.
    """


class TaskRefHydrationError(StardagError):
    """A :class:`TaskRef` could not be hydrated: the registry has no such
    task or instance, the stored body is of another class, or it no longer
    recomputes to the referenced task id."""


def unhydrated_ref_error(task: BaseTask) -> TaskRefNotHydratedError:
    """The refusal to dump ``task`` (a reference, or a task holding one)
    into an instance body."""
    where = (
        f"{task!r}"
        if isinstance(task, TaskRef)
        else f"{type(task).__name__} {task.id} holds a TaskRef that"
    )
    return TaskRefNotHydratedError(
        f"{where} reached an instance body unhydrated. A registry stores "
        "real task bodies only, so builds replace every reference with its "
        "hydrated task before registering anything. Pass the task to "
        "sd.build, or resolve it first: stardag.resolve_refs(task) "
        "(or .hydrate() on the reference)."
    )


def _class_label(cls: type) -> str:
    return f"{cls.__module__}.{cls.__qualname__}"


class TaskRef(Task[LoadedT], Generic[TaskT, LoadedT]):
    """A typed reference to a registered task. Create one with
    :meth:`Task.ref <stardag.Task.ref>`, not directly::

        model = TrainedModel.ref("6f1c...", instance_id="0193...")
        evaluation = Evaluate(model=model)  # same id as with the real task
        sd.build(evaluation)                # hydrates `model` first

    ``TaskRef[TrainedModel, Model]`` is a ``Task[Model]``, so it is accepted
    wherever an ``sd.TaskLoads[Model]`` is, and ``.hydrate()`` returns a
    ``TrainedModel``.

    Attributes:
        task_class: The referenced task class. The hydrated task is an
            instance of it (or of a subclass).
        task_id: The referenced task id: this reference's :attr:`id`.
        instance_id: The pinned instance, or ``None`` for the newest one
            at hydration time (which always asks the registry).
    """

    __stardag_abstract__: ClassVar[bool] = True

    task_class: type
    task_id: UUID
    instance_id: UUID | None = None

    @field_validator("task_class")
    @classmethod
    def _task_class_is_a_task(cls, value: type) -> type:
        if not (isinstance(value, type) and issubclass(value, BaseTask)):
            raise TypeError(f"task_class must be a task class, got {value!r}")
        if issubclass(value, TaskRef):
            raise TypeError("A TaskRef cannot reference another TaskRef")
        return value

    @field_serializer("task_class")
    def _serialize_task_class(self, value: type) -> str:
        return _class_label(value)

    @classmethod
    def __map_generic_args_to_ancestor__(
        cls, ancestor_origin: type, args: tuple
    ) -> tuple | None:
        """``TaskRef[TaskT, L]`` is a ``Task[L]``: map its loaded type (the
        second argument) onto the ancestors ``TaskLoads`` checks against."""
        if len(args) != 2:
            return None
        loaded = args[1]
        if ancestor_origin is LoadableTask or ancestor_origin is Task:
            return (loaded,)
        if ancestor_origin is TargetTask:
            return (LoadableSaveableFileSystemTarget[loaded],)
        return None

    @classmethod
    def from_task(cls, task: BaseTask) -> TaskSlug:
        """Deprecated: the former ``stardag.TaskRef`` dataclass (name,
        version, id and a slug) is now :class:`stardag.TaskSlug`; this
        returns one."""
        warnings.warn(
            "stardag.TaskRef is now the typed task reference (Task.ref); the "
            "former name/version/id dataclass is stardag.TaskSlug. Use "
            "stardag.TaskSlug.from_task(task).",
            DeprecationWarning,
            stacklevel=2,
        )
        return TaskSlug.from_task(task)

    # -- identity -------------------------------------------------------------

    @cached_property
    def id(self) -> UUID:
        """The referenced task id."""
        return self.task_id

    @cached_property
    def _contains_task_refs(self) -> bool:
        return True

    def _hash_mode_finalize(self, data: dict[str, Any], info: SerializationInfo) -> Any:
        # What a real task contributes to its parent's hash.
        return {"id": str(self.task_id)}

    def _serialize_extra(self, data: Any, info: SerializationInfo) -> Any:
        mode = info.context.get(CONTEXT_MODE_KEY) if info.context else None
        if mode == "registry":
            # A backstop: ``canonical_instance_body_json`` refuses first,
            # with this error unwrapped (pydantic wraps one raised here).
            raise unhydrated_ref_error(self)
        # Hash mode: ``_hash_mode_finalize`` replaces the dump with the id.
        return data

    def __reduce__(self) -> tuple[Any, ...]:
        # A runtime-parametrized ``TaskRef[X, L]`` is not importable by
        # name, so rebuild through ``ref`` (pickled by reference to the
        # referenced class) rather than by class.
        return (_rebuild_ref, (self.task_class, self.task_id, self.instance_id))

    def __str__(self) -> str:
        return repr(self)

    def __repr__(self) -> str:
        pinned = f", instance_id={str(self.instance_id)!r}" if self.instance_id else ""
        return f"{self.task_class.__name__}.ref({str(self.task_id)!r}{pinned})"

    # -- hydration ------------------------------------------------------------

    def hydrate(self, registry: RegistryABC | None = None) -> TaskT:
        """The referenced task object, read from the registry.

        Pinned (``instance_id`` given), the instance's body is used and is
        served from the local body cache when present, so it works offline
        after one online read. Unpinned, the newest instance's body is
        read, which always asks the registry. Either way the result is
        checked to be an instance of :attr:`task_class` with this
        reference's task id.

        Memoized per process and registry: hydrating the same reference
        twice reads once (for an unpinned reference, the newest instance
        at the first hydration is kept).

        Args:
            registry: Default: the configured registry.

        Raises:
            TaskRefHydrationError: No registry is configured, the registry
                holds no such task or instance, or the body is of another
                class or no longer recomputes to the task id.
            TaskRehydrationError: A task class in the body (nested ones
                included) is not imported.
        """
        from stardag.registry import is_noop_registry, registry_provider

        registry = registry if registry is not None else registry_provider.get()
        if is_noop_registry(registry):
            raise TaskRefHydrationError(
                f"Cannot hydrate {self!r}: no registry is configured. A task "
                "reference is read from the registry that holds the task (run "
                "'stardag auth login', or configure a profile or API key)."
            )
        key = (self.task_class, self.task_id, self.instance_id)
        memo = _memo_for(registry)
        if memo is not None:
            with _MEMO_LOCK:
                cached = memo.get(key)
            if cached is not None:
                return typing.cast(TaskT, cached)
        task = self._read(registry)
        if memo is not None:
            with _MEMO_LOCK:
                memo.setdefault(key, task)
        return typing.cast(TaskT, task)

    async def hydrate_aio(self, registry: RegistryABC | None = None) -> TaskT:
        """:meth:`hydrate`, off the event loop (in a worker thread)."""
        return await asyncio.to_thread(self.hydrate, registry)

    def _read(self, registry: RegistryABC) -> BaseTask:
        from stardag._core.rehydrate import TaskRehydrationError, load_from_registry
        from stardag.exceptions import NotFoundError

        try:
            # Not ``from_registry``: that is the explicit, trusted path, and a
            # build hydrates references with no user asking for this load, so
            # an AliasTask payload (pickled bytes) is refused, not unpickled.
            task = load_from_registry(
                typing.cast(type[BaseTask], self.task_class),
                self.task_id,
                registry,
                instance_id=self.instance_id,
                allow_aliased=False,
            )
        except NotFoundError as e:
            what = (
                f"instance {self.instance_id} of task {self.task_id}"
                if self.instance_id
                else f"task {self.task_id}"
            )
            raise TaskRefHydrationError(
                f"Cannot hydrate {self!r}: the registry holds no {what}. Check "
                "the active profile points at the registry and environment "
                f"the task was registered in. ({e})"
            ) from e
        except TaskRehydrationError as e:
            raise TaskRefHydrationError(f"Cannot hydrate {self!r}: {e}") from e
        if not isinstance(task, self.task_class):
            raise TaskRefHydrationError(
                f"Cannot hydrate {self!r}: the registry's body is a "
                f"{_class_label(type(task))}, not a "
                f"{_class_label(self.task_class)}."
            )
        if task.id != self.task_id:
            raise TaskRefHydrationError(
                f"Cannot hydrate {self!r}: the stored body recomputes to task id "
                f"{task.id}."
            )
        return task

    # -- delegation -----------------------------------------------------------

    def complete(self) -> bool:
        return self.hydrate().complete()

    async def complete_aio(self) -> bool:
        return await (await self.hydrate_aio()).complete_aio()

    def target(self) -> Any:
        return typing.cast(Any, self.hydrate()).target()

    def load(self) -> LoadedT:
        return typing.cast(Any, self.hydrate()).load()

    async def load_aio(self) -> LoadedT:
        return await typing.cast(Any, await self.hydrate_aio()).load_aio()

    def run(self) -> None:
        raise TaskRefNotHydratedError(
            f"{self!r} cannot run: a reference stands for a task the registry "
            "already holds. Builds hydrate references before running anything; "
            "call .hydrate() to get the task itself."
        )


def _rebuild_ref(task_class: Any, task_id: UUID, instance_id: UUID | None) -> Any:
    return parametrized_ref_class(task_class)(
        task_class=task_class, task_id=task_id, instance_id=instance_id
    )


def _generic_meta(klass: type) -> tuple[Any, tuple[Any, ...], tuple[Any, ...]]:
    meta = getattr(klass, "__pydantic_generic_metadata__", None) or {}
    return meta.get("origin"), meta.get("args", ()), meta.get("parameters", ())


def _loaded_type(task_class: type) -> Any:
    """The ``LoadedT`` a ``Task`` subclass binds (``class X(Task[int])`` ->
    ``int``), or ``None`` when it cannot be read. Follows one generic
    subclass in between, such as the class ``@sd.task`` builds on
    (``_FunctionTask[int, ...]`` -> its ``Task[LoadedT]`` base -> ``int``)."""
    for klass in task_class.__mro__:
        origin, args, _ = _generic_meta(klass)
        if origin is None or not args:
            continue
        if origin is Task:
            loaded = args[0]
        else:
            _, _, parameters = _generic_meta(origin)
            substitution = dict(zip(parameters, args))
            loaded = next(
                (
                    _generic_meta(base)[1][0]
                    for base in origin.__mro__
                    if _generic_meta(base)[0] is Task and _generic_meta(base)[1]
                ),
                None,
            )
            if isinstance(loaded, TypeVar):
                loaded = substitution.get(loaded, loaded)
        if loaded is not None and not isinstance(loaded, TypeVar):
            return loaded
    return None


_PARAMETRIZED: dict[type, type] = {}


def parametrized_ref_class(task_class: type) -> type[TaskRef[Any, Any]]:
    """``TaskRef[task_class, <its loaded type>]``, so that the polymorphic
    validator's generic check (``TaskLoads[T]`` given a reference of
    another loaded type) applies at runtime too, as it does to the real
    task. Plain ``TaskRef`` when the loaded type cannot be read."""
    cached = _PARAMETRIZED.get(task_class)
    if cached is not None:
        return cached
    loaded = _loaded_type(task_class)
    result: type[TaskRef[Any, Any]] = (
        TaskRef if loaded is None else TaskRef[task_class, loaded]  # type: ignore[valid-type]
    )
    _PARAMETRIZED[task_class] = result
    return result


# -----------------------------------------------------------------------------
# The per-process hydration memo
# -----------------------------------------------------------------------------

_MEMO_LOCK = threading.Lock()
_MEMO: weakref.WeakKeyDictionary[Any, dict[tuple[Any, UUID, UUID | None], BaseTask]]
_MEMO = weakref.WeakKeyDictionary()


def _memo_for(registry: Any) -> dict[tuple[Any, UUID, UUID | None], BaseTask] | None:
    """The memo of hydrated tasks read from ``registry`` (``None`` when the
    registry cannot be weakly referenced: no memo then)."""
    with _MEMO_LOCK:
        try:
            return _MEMO.setdefault(registry, {})
        except TypeError:
            return None


def clear_hydration_memo() -> None:
    """Forget every hydrated reference (tests, or a long-lived process that
    wants an unpinned reference to read the newest instance again)."""
    with _MEMO_LOCK:
        _MEMO.clear()


# -----------------------------------------------------------------------------
# Build-start hydration
# -----------------------------------------------------------------------------


def contains_refs(value: Any) -> bool:
    """Whether a :class:`TaskRef` is anywhere in ``value``: a task, a
    model, or a list / tuple / set / dict of them, through every field.
    No I/O. Each task's answer is cached on it (tasks are frozen), so
    asking again for a DAG's members costs nothing."""
    return _contains(value, set())


def _contains(value: Any, seen: set[int]) -> bool:
    if isinstance(value, TaskRef):
        return True
    if isinstance(value, BaseTask):
        # Cached on the task (a cached_property, so a ``model_copy`` that
        # may swap a field drops it).
        return value._contains_task_refs is True
    key = id(value)
    if key in seen:
        return False
    seen.add(key)
    if isinstance(value, BaseModel):
        return fields_contain_refs(value, seen)
    if isinstance(value, Mapping):
        return any(_contains(v, seen) for v in value.values())
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(_contains(v, seen) for v in value)
    return False


def fields_contain_refs(model: BaseModel, seen: set[int] | None = None) -> bool:
    """Whether any field of ``model`` holds a :class:`TaskRef` (the body
    of ``BaseTask._contains_task_refs``)."""
    seen = set() if seen is None else seen
    values = vars(model)
    return any(
        _contains(values[name], seen)
        for name in type(model).model_fields
        if name in values
    )


_Struct = TypeVar("_Struct")


def resolve_refs(value: _Struct, registry: RegistryABC | None = None) -> _Struct:
    """``value`` with every :class:`TaskRef` in it replaced by its hydrated
    task: the roots of a build, or any structure of tasks.

    References are found in nested task fields too, at any depth, and in
    lists, tuples, sets and dicts of tasks. A task holding one is rebuilt
    (``model_copy``) with the hydrated task in its place; since a
    reference's id is the referenced task id, every id is unchanged.
    Anything without a reference is returned as the same object.

    Args:
        value: A task, or a list / tuple / dict of them.
        registry: Where references are read from. Default: the configured
            registry.

    Raises:
        TaskRefHydrationError: A reference could not be hydrated.
    """
    if not contains_refs(value):
        return value
    return typing.cast(_Struct, _resolve(value, registry, {}))


async def resolve_refs_aio(
    value: _Struct, registry: RegistryABC | None = None
) -> _Struct:
    """:func:`resolve_refs`, with registry reads off the event loop. Costs no
    thread when there is no reference to hydrate."""
    if not contains_refs(value):
        return value
    return await asyncio.to_thread(resolve_refs, value, registry)


def _resolve(value: Any, registry: Any, memo: dict[int, Any]) -> Any:
    key = id(value)
    if key in memo:
        return memo[key]
    result = value
    if isinstance(value, TaskRef):
        result = value.hydrate(registry)
    elif isinstance(value, BaseModel):
        if contains_refs(value):
            updates = {}
            for name in type(value).model_fields:
                if name not in value.__dict__:
                    continue
                current = value.__dict__[name]
                replaced = _resolve(current, registry, memo)
                if replaced is not current:
                    updates[name] = replaced
            if updates:
                result = value.model_copy(update=updates)
    elif type(value) in (list, tuple, set, frozenset):
        items = [_resolve(v, registry, memo) for v in value]
        if any(new is not old for new, old in zip(items, value)):
            result = type(value)(items)
    elif type(value) is dict:
        items = {k: _resolve(v, registry, memo) for k, v in value.items()}
        if any(items[k] is not v for k, v in value.items()):
            result = items
    memo[key] = result
    return result
