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
import collections
import dataclasses
import threading
import typing
import warnings
import weakref
from collections.abc import Callable, Mapping
from functools import cached_property
from typing import TYPE_CHECKING, Any, ClassVar, Generic, TypeVar
from uuid import UUID

import httpx
from typing_extensions import Self

from pydantic import (
    BaseModel,
    ConfigDict,
    SerializationInfo,
    field_serializer,
    field_validator,
    model_validator,
)

from stardag._core.base_task import BaseTask, LoadableTask, TargetTask, TaskSlug
from stardag._core.task import LoadedT, Task
from stardag._core.rehydrate import TaskRehydrationError
from stardag.base_model import CONTEXT_MODE_KEY
from stardag.exceptions import StardagError
from stardag.target import LoadableSaveableFileSystemTarget

if TYPE_CHECKING:
    from stardag.registry import RegistryABC

TaskT = TypeVar("TaskT", bound=BaseTask, covariant=True)


class TaskRefNotHydratedError(StardagError):
    """A :class:`TaskRef` was used where only a real task can go: dumped
    into an instance body (registration, a Modal payload) or run.

    Builds hydrate every reference before discovery, so reaching this means
    a reference got past that step; call ``.hydrate()`` on it first, or
    pass it to ``sd.build``.
    """


class TaskRefHydrationError(TaskRehydrationError):
    """A :class:`TaskRef` could not be hydrated: no registry is configured
    or it cannot be reached, it has no such task or instance, the stored
    body is of another class, a class in it is not imported, or it no
    longer recomputes to the referenced task id.

    A :class:`~stardag.TaskRehydrationError`, so code handling failed
    rehydration in general handles this too."""


class TaskRefRegistryUnavailableError(TaskRefHydrationError):
    """Hydration failed because the registry could not be reached or
    answered with an outage (5xx, 429), after the client's own retries: a
    transient condition, not a verdict on the reference. A reactive tick
    retries it rather than excluding the task that built the reference."""


class _ReferencedClassMethod:
    """A class-level accessor (``get_name``, ``get_namespace``) that, read
    off a reference, answers for the referenced class. ``TaskRef`` itself
    is not a registered task class and has no identity of its own."""

    def __init__(self, name: str) -> None:
        self.name = name

    def __get__(self, obj: Any, owner: Any = None) -> Callable[[], str]:
        if obj is None:
            name = self.name

            def unbound() -> str:
                raise TypeError(
                    f"TaskRef.{name}() answers for the referenced class: call "
                    "it on a reference, or on the task class itself."
                )

            return unbound
        return getattr(obj.task_class, self.name)


def unhydrated_ref_error(task: BaseTask) -> TaskRefNotHydratedError:
    """The refusal to dump ``task`` (a reference, or a task holding one)
    into an instance body."""
    where = (
        f"{task!r}"
        if isinstance(task, TaskRef)
        else f"{type(task).__name__} {task.id} holds a TaskRef that"
    )
    return TaskRefNotHydratedError(
        f"{where} cannot be serialized unhydrated: a reference has no body "
        "(only its task id). The registry stores real task bodies only, so "
        "builds replace every reference with its hydrated task before "
        "registering anything. Pass the task to sd.build, or resolve it "
        "first: sd.resolve_refs(task) (or .hydrate() on the reference)."
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
        instance_id: The pinned instance, or ``None`` to choose one at
            hydration time (which always asks the registry): for a completed
            task the instance of the execution that completed it, else the
            newest.
    """

    __stardag_abstract__: ClassVar[bool] = True
    model_config = ConfigDict(ignored_types=(_ReferencedClassMethod,))

    task_class: type
    task_id: UUID
    instance_id: UUID | None = None

    @model_validator(mode="before")
    @classmethod
    def _refuse_the_former_task_ref(cls, data: Any) -> Any:
        """``stardag.TaskRef`` was a ``name``/``version``/``id`` dataclass
        before it became this; say so rather than "field required"."""
        if (
            isinstance(data, Mapping)
            and "task_class" not in data
            and ("name" in data or "id" in data)
        ):
            raise TypeError(
                "stardag.TaskRef is now the typed task reference "
                "(MyTask.ref(task_id, instance_id=...)); the former "
                "name/version/id dataclass is stardag.TaskSlug."
            )
        return data

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

    # Class-level identity, answered for the referenced class (no I/O):
    # generic code that walks a DAG and logs names works on a reference.
    get_name = _ReferencedClassMethod("get_name")  # type: ignore[assignment]
    get_namespace = _ReferencedClassMethod("get_namespace")  # type: ignore[assignment]

    def _hash_mode_finalize(self, data: dict[str, Any], info: SerializationInfo) -> Any:
        # What a real task contributes to its parent's hash.
        return {"id": str(self.task_id)}

    def _serialize_extra(self, data: Any, info: SerializationInfo) -> Any:
        """Only hash mode (a parent's task id) dumps a reference: to
        ``{"id": task_id}``, via ``_hash_mode_finalize``.

        Every other dump is refused, the plain ``model_dump()`` /
        ``model_dump_json()`` of a task holding one included. A reference
        has no body, and a dump of one could not be loaded back: there is
        no stored form of a reference, by design (the registry stores real
        bodies only). Refusing here rather than emitting a dump that cannot
        round-trip keeps one rule for every serialization: hydrate first.
        ``canonical_instance_body_json`` refuses the registry mode before
        this is reached, unwrapped; raised here, pydantic wraps the error in
        a ``PydanticSerializationError`` carrying its message.
        """
        mode = info.context.get(CONTEXT_MODE_KEY) if info.context else None
        if mode != "hash":
            raise unhydrated_ref_error(self)
        return data

    def model_copy(
        self, *, update: Mapping[str, Any] | None = None, deep: bool = False
    ) -> Self:
        """A copy of the reference; refused with an ``update``.

        A reference has no parameters to update: its id is the pinned task
        id whatever its fields say, so ``ref.model_copy(update=...)`` could
        only return the same reference and silently drop the update, which
        turns a ``model_copy`` derivation chain into copies of the
        referenced task. Derive from the real task instead:
        ``ref.hydrate().model_copy(update=...)``.
        """
        if update:
            raise TaskRefNotHydratedError(
                f"{self!r}.model_copy(update={dict(update)!r}): a task reference "
                "has no parameters to update, so the update would be silently "
                "dropped (the copy would still be the referenced task). Derive "
                "from the real task: ref.hydrate().model_copy(update=...)."
            )
        return self._copy_identity()

    def _copy_identity(self) -> Self:
        """A fresh reference with this one's identity and its anchor (the
        file of the code that created it, which decides its task store);
        not the registry a hydration bound."""
        from stardag._core.task_store_read import REF_ANCHOR_KEY

        copied = typing.cast(
            Self, _rebuild_ref(self.task_class, self.task_id, self.instance_id)
        )
        anchor = vars(self).get(REF_ANCHOR_KEY)
        if anchor is not None:
            vars(copied)[REF_ANCHOR_KEY] = anchor
        return copied

    def __deepcopy__(self, memo: dict[int, Any] | None = None) -> Self:
        """A fresh reference with the same identity. Only the identity is
        copied: not the registry a hydration bound, which may hold
        uncopyable state (a lock)."""
        copied = self._copy_identity()
        if memo is not None:
            memo[id(self)] = copied
        return copied

    def __reduce__(self) -> tuple[Any, ...]:
        # A runtime-parametrized ``TaskRef[X, L]`` is not importable by
        # name, so rebuild through ``ref`` (pickled by reference to the
        # referenced class) rather than by class. The anchor is not pickled:
        # a process that unpickles a reference (a remote worker) has no
        # checkout of the creating code to anchor at.
        return (_rebuild_ref, (self.task_class, self.task_id, self.instance_id))

    def __str__(self) -> str:
        return repr(self)

    if not TYPE_CHECKING:
        # Runtime only: a type checker must keep rejecting
        # ``ref.learning_rate``, which is the point.

        def __getattr__(self, name: str) -> Any:
            try:
                return super().__getattr__(name)
            except AttributeError:
                if name.startswith("_"):
                    raise
                raise AttributeError(
                    f"{self!r} has no attribute {name!r}: a task reference is "
                    "a placeholder; call .hydrate() to read its parameters."
                ) from None

    def __repr__(self) -> str:
        pinned = f", instance_id={str(self.instance_id)!r}" if self.instance_id else ""
        name = self.task_class.__name__
        if ref_is_shadowed(self.task_class):
            return f"sd.task_ref({name}, {str(self.task_id)!r}{pinned})"
        return f"{name}.ref({str(self.task_id)!r}{pinned})"

    # -- hydration ------------------------------------------------------------

    def hydrate(self, registry: RegistryABC | None = None) -> TaskT:
        """The referenced task object, read from the project's task store
        or the registry.

        The committed task store is consulted first (not when ``registry``
        is given, which asks for that registry): the store of the package
        whose code created the reference, else of the referenced class
        (``[tool.stardag] task_store``). A pinned reference is served from
        a body recorded under its instance id, an unpinned one only when
        the store holds exactly one body of the task. A reference hydrated
        inside a remote container (a Modal worker) has no store unless the
        image has it, and reads the registry.

        Pinned (``instance_id`` given), the instance's body is used and is
        served from the local body cache when present, so it works offline
        after one online read. Unpinned, the registry chooses: for a
        completed task the instance of the execution that completed it,
        else the newest instance; this always asks the registry. Either
        way the result is checked to be an instance of :attr:`task_class` with this
        reference's task id.

        Memoized per process and registry: hydrating the same reference
        twice reads once (for an unpinned reference, the instance chosen
        at the first hydration is kept).

        Args:
            registry: Default: the configured registry.

        Raises:
            TaskRefHydrationError: No registry is configured or it cannot be
                reached, it holds no such task or instance, a task class in
                the body (nested ones included) is not imported, or the body
                is of another class or no longer recomputes to the task id.
                A subclass of :class:`~stardag.TaskRehydrationError`.
        """
        return self._hydrate(registry, use_store=registry is None)

    def _hydrate(self, registry: RegistryABC | None, *, use_store: bool) -> TaskT:
        """:meth:`hydrate`. ``use_store``: consult the task store first --
        not for an explicit ``hydrate(registry=...)``, which asks for that
        registry, but always for build-start hydration, which passes the
        build's registry only as the fallback."""
        from stardag._core.anchor import anchored
        from stardag._core.task_store_read import ref_anchors

        # The reference's own anchors (the code that created it, then its
        # class) decide its task store and the task_modules imported on a
        # class miss, also when it is read from the registry.
        with anchored(*ref_anchors(self)):
            return self._hydrate_anchored(registry, use_store=use_store)

    def _hydrate_anchored(
        self, registry: RegistryABC | None, *, use_store: bool
    ) -> TaskT:
        from stardag._core.task_store_read import hydrate_from_store, store_note
        from stardag.registry import is_noop_registry, registry_provider

        # The package's committed task store, when one applies and holds the
        # body, comes first: no registry, no network.
        if use_store:
            stored = hydrate_from_store(self)
            if stored is not None:
                return typing.cast(TaskT, stored)
        if registry is None:
            # The registry this very reference was last hydrated from, if
            # any: a build hydrates a reference a task yields from the
            # build's registry, and the task's own later ``ref.load()``
            # must read that one, not the configured default.
            bound: RegistryABC | None = vars(self).get(_BOUND_REGISTRY_KEY)
            registry = bound if bound is not None else registry_provider.get()
        else:
            vars(self)[_BOUND_REGISTRY_KEY] = registry
        if is_noop_registry(registry):
            raise TaskRefHydrationError(
                f"Cannot hydrate {self!r}: no registry is configured. A task "
                "reference is read from the registry that holds the task (run "
                "'stardag auth login', or configure a profile or API key)."
                + (store_note(self) if use_store else "")
            )
        key = (self.task_class, self.task_id, self.instance_id)
        memo = _memo_for(registry)
        if memo is None:
            return typing.cast(TaskT, self._read(registry))
        # One read per registry and key, however many threads hydrate the
        # same reference at once (a build's walk resolves in parallel):
        # every caller gets the same task object, so an unpinned reference
        # cannot hydrate to two different bodies within one process.
        with _key_lock(registry, key):
            with _MEMO_LOCK:
                cached = memo.get(key)
            if cached is None:
                cached = self._read(registry)
                with _MEMO_LOCK:
                    cached = memo.setdefault(key, cached)
        return typing.cast(TaskT, cached)

    async def hydrate_aio(self, registry: RegistryABC | None = None) -> TaskT:
        """:meth:`hydrate`, off the event loop (in a worker thread)."""
        return await asyncio.to_thread(self.hydrate, registry)

    def _read(self, registry: RegistryABC) -> BaseTask:
        from stardag._core.rehydrate import TaskRehydrationError, load_from_registry
        from stardag.exceptions import APIError, NotFoundError

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
        except httpx.TransportError as e:
            # Raised once the client's own retries are spent: offline, or
            # the registry unreachable.
            raise TaskRefRegistryUnavailableError(self._offline_message(e)) from e
        except APIError as e:
            if e.status_code is not None and (
                e.status_code >= 500 or e.status_code == 429
            ):
                raise TaskRefRegistryUnavailableError(
                    f"Cannot hydrate {self!r}: the registry is unavailable ({e})."
                ) from e
            raise
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

    def _offline_message(self, error: Exception) -> str:
        reached = (
            f"Cannot hydrate {self!r}: the registry could not be reached "
            f"({type(error).__name__}: {error})."
        )
        if self.instance_id is None:
            return (
                f"{reached} An unpinned reference always asks the registry; "
                "pin it with instance_id= ('stardag tasks ref <task-id>' prints "
                "one) and it is served from the local body cache after one "
                "online read."
            )
        from stardag.registry import _instance_cache

        if not _instance_cache.cache_enabled():
            return (
                f"{reached} The local body cache is turned off "
                "(STARDAG_REGISTRY_CACHE=0), so a pinned reference reads the "
                "registry in every new process."
            )
        return (
            f"{reached} Instance {self.instance_id} is not in the local body "
            "cache yet: one online read is needed, after which it hydrates "
            "offline."
        )

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
        if origin is Task or origin is LoadableTask:
            loaded = args[0]
        else:
            _, _, parameters = _generic_meta(origin)
            substitution = dict(zip(parameters, args))
            loaded = next(
                (
                    _generic_meta(base)[1][0]
                    for base in origin.__mro__
                    if _generic_meta(base)[0] in (Task, LoadableTask)
                    and _generic_meta(base)[1]
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


# Kept in a reference's ``__dict__`` (not a field: outside every dump, the
# hash and equality, and not carried by ``model_copy`` or a pickle).
_BOUND_REGISTRY_KEY = "_stardag_bound_registry"


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


_KEY_LOCKS: weakref.WeakKeyDictionary[Any, dict[Any, threading.Lock]]
_KEY_LOCKS = weakref.WeakKeyDictionary()


def _key_lock(registry: Any, key: Any) -> threading.Lock:
    with _MEMO_LOCK:
        locks = _KEY_LOCKS.setdefault(registry, {})
        return locks.setdefault(key, threading.Lock())


def clear_hydration_memo() -> None:
    """Forget every hydrated reference (tests, or a long-lived process that
    wants an unpinned reference to choose its instance again)."""
    from stardag._core.task_store_read import clear_store_memo

    with _MEMO_LOCK:
        _MEMO.clear()
    clear_store_memo()


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
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return any(
            _contains(getattr(value, f.name, None), seen)
            for f in dataclasses.fields(value)
        )
    if isinstance(value, Mapping):
        return any(_contains(v, seen) for v in value.values())
    if isinstance(value, (list, tuple, set, frozenset, collections.deque)):
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
    lists, tuples, sets, mappings and dataclasses holding tasks, as well as
    inside a hydrated task (a field defaulting to a reference). A holder is
    rebuilt with the hydrated task in its place (``model_copy`` for a model,
    ``dataclasses.replace`` for a dataclass, its own type for a container);
    since a reference's id is the referenced task id, every id is
    unchanged. Anything without a reference is returned as the same object.
    A set in which two references hydrate to one task is refused, since
    collapsing them would change the set.

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
        # The hydrated task may itself hold references: a field added since
        # its body was stored, defaulting to one.
        result = _resolve(value._hydrate(registry, use_store=True), registry, memo)
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
                if (
                    isinstance(value, BaseTask)
                    and isinstance(result, BaseTask)
                    and result.id != value.id
                ):
                    raise TaskRefHydrationError(
                        f"Hydrating the references in {type(value).__name__} "
                        f"{value.id} would change its task id (to {result.id}): "
                        "a field whose compat_default is a task, or contains "
                        "tasks, compares the real task differently from its "
                        "reference. Pass the hydrated task (ref.hydrate()) "
                        "there, or drop the compat_default."
                    )
    elif dataclasses.is_dataclass(value) and not isinstance(value, type):
        if contains_refs(value):
            changes = {}
            for f in dataclasses.fields(value):
                current = getattr(value, f.name, None)
                replaced = _resolve(current, registry, memo)
                if replaced is not current:
                    if not f.init:
                        raise TaskRefNotHydratedError(
                            f"{type(value).__qualname__}.{f.name} holds a "
                            "TaskRef but is not an init field, so it cannot be "
                            "replaced by the hydrated task. Hydrate it yourself."
                        )
                    changes[f.name] = replaced
            if changes:
                result = dataclasses.replace(value, **changes)
    elif isinstance(value, (list, tuple, set, frozenset, collections.deque)):
        items = [_resolve(v, registry, memo) for v in value]
        if any(new is not old for new, old in zip(items, value)):
            result = _rebuilt(value, items)
            if isinstance(value, (set, frozenset)) and len(result) != len(value):
                raise TaskRefHydrationError(
                    "Hydrating the references in a set made two of its members "
                    "equal (e.g. a pinned and an unpinned reference to one "
                    "task), which would silently change the set, and so the "
                    "task holding it. Put each task in the set once."
                )
    elif isinstance(value, Mapping):
        items = {k: _resolve(v, registry, memo) for k, v in value.items()}
        if any(items[k] is not v for k, v in value.items()):
            result = _rebuilt(value, items)
    memo[key] = result
    return result


def _rebuilt(original: Any, items: Any) -> Any:
    """``items`` as a container of ``original``'s type (a ``list``,
    ``OrderedDict``, ``MappingProxyType``, ...), or refused when that type
    cannot be built from them."""
    kind = type(original)
    if kind in (list, tuple, set, frozenset, dict):
        return kind(items)
    if isinstance(original, collections.deque):
        return kind(items, maxlen=original.maxlen)
    try:
        return kind(items)
    except Exception as e:
        raise TaskRefNotHydratedError(
            f"A {kind.__qualname__} holding a TaskRef cannot be rebuilt with "
            f"the hydrated task ({e}). Hydrate the reference yourself "
            "(.hydrate()) before putting it there."
        ) from e


# -----------------------------------------------------------------------------
# The reference line
# -----------------------------------------------------------------------------


def reference_line(task_class: type) -> tuple[str | None, str]:
    """``(import line or None, expression prefix)`` for code referencing a
    task of ``task_class``: ``f"{prefix}.ref(...)"`` after the import.

    Read off the live class, so it is what the user's code would write:

    - the name the class is bound to in its module, which for a renamed
      ``@sd.task(name="Renamed") def add_one`` is ``add_one``;
    - a nested class through its outermost one (``from m import Outer``,
      ``Outer.Inner``);
    - no import for a class defined in ``__main__`` or one not importable by
      name from its module (a local class): just its name.

    Shared by ``stardag tasks ref`` and other snippet printers, so they
    agree on the line.
    """
    module = task_class.__module__
    if module == "__main__" or "<locals>" in task_class.__qualname__:
        return None, _bound_name(task_class, module) or task_class.__name__
    bound = _bound_name(task_class, module)
    if bound is None:
        return None, task_class.__name__
    return f"from {module} import {bound.partition('.')[0]}", bound


def ref_is_shadowed(task_class: type) -> bool:
    """Whether ``task_class`` declares a field named ``ref``, which hides
    the ``ref`` method: such a class is referenced with
    ``sd.task_ref(task_class, ...)``."""
    return "ref" in (getattr(task_class, "model_fields", None) or {})


def reference_call(
    prefix: str, task_class: type | None, task_id: str, instance_id: str | None
) -> str:
    """The expression referencing a task, given the expression ``prefix``
    naming its class (from :func:`reference_line`): ``Prefix.ref(...)``,
    or ``sd.task_ref(Prefix, ...)`` for a class whose ``ref`` is shadowed
    by a field."""
    pinned = f', instance_id="{instance_id}"' if instance_id else ""
    if task_class is not None and ref_is_shadowed(task_class):
        return f'sd.task_ref({prefix}, "{task_id}"{pinned})'
    return f'{prefix}.ref("{task_id}"{pinned})'


def _bound_name(task_class: type, module_name: str) -> str | None:
    """The name ``task_class`` is reachable under from its module: its
    qualified name when that resolves to it, else a public module
    attribute bound to it. ``None`` when neither does."""
    import importlib
    import sys

    module = sys.modules.get(module_name)
    if module is None:
        try:
            module = importlib.import_module(module_name)
        except Exception:
            return None
    target: object = module
    for part in task_class.__qualname__.split("."):
        target = getattr(target, part, None)
    if target is task_class:
        return task_class.__qualname__
    for attribute, value in vars(module).items():
        if value is task_class and not attribute.startswith("_"):
            return attribute
    return None


# -----------------------------------------------------------------------------
# The function form
# -----------------------------------------------------------------------------

_RefT_co = TypeVar("_RefT_co", bound=BaseTask, covariant=True)
_RefL_co = TypeVar("_RefL_co", covariant=True)
_RefB = TypeVar("_RefB", bound=BaseTask)


class _LoadableTaskClass(typing.Protocol[_RefT_co, _RefL_co]):
    """A task class seen statically: what it builds (``model_validate``
    returns an instance of it) and what it loads (its ``load``). How
    :func:`task_ref` reads both type arguments off one class argument."""

    @property
    def model_validate(self) -> Callable[..., _RefT_co]: ...

    @property
    def load(self) -> Callable[[Any], _RefL_co]: ...


@typing.overload
def task_ref(
    task_class: _LoadableTaskClass[_RefT_co, _RefL_co],
    task_id: UUID | str,
    instance_id: UUID | str | None = None,
) -> TaskRef[_RefT_co, _RefL_co]: ...


@typing.overload
def task_ref(
    task_class: type[_RefB],
    task_id: UUID | str,
    instance_id: UUID | str | None = None,
) -> TaskRef[_RefB, Any]: ...


def task_ref(
    task_class: Any,
    task_id: UUID | str,
    instance_id: UUID | str | None = None,
) -> Any:
    """``task_class.ref(task_id, instance_id)``, as a function.

    The same reference, typed the same way (``TaskRef[MyTask, LoadedT]``
    for a class that loads, ``TaskRef[MyTask, Any]`` for a plain
    ``BaseTask`` subclass), for a class on which ``.ref`` is not the method:
    one declaring a field named ``ref`` shadows it. Never reads
    ``task_class.ref``.

    Raises:
        TypeError: ``task_class`` is not a task class, or is itself a
            ``TaskRef``.
    """
    from stardag._core.base_task import _make_ref

    if not (isinstance(task_class, type) and issubclass(task_class, BaseTask)):
        raise TypeError(f"task_ref() needs a task class, got {task_class!r}")
    return _make_ref(task_class, task_id, instance_id)
