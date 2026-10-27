"""Reconstruct task objects from registry-stored instance bodies — pickle-free.

An **instance** is a registry row: one construction of a task under a
deterministic scope, stored as its *body* — the registry-mode dump of every
field, significant or not, defaults included (``BaseTask.instance_body``).
The Python object rebuilt from it is a **task object**; one instance
rehydrates into any number of them.

The body is self-describing: the polymorphic serializer embeds the
``__namespace`` / ``__name`` discriminator keys, and nested task fields
carry their own (as full bodies). That makes it sufficient to rebuild the
concrete task object via the polymorphic validator — provided the module
defining the task class has been imported (class registration happens at
class-definition time).

Rehydration is **strict for significant fields and lenient for the rest**:
the recomputed task id must match the expected one (``expected_task_id``),
while a stored key the class no longer declares is dropped with a warning
and a missing non-significant field takes the class default. That is what
lets an old plan's root bodies be re-read under new code.

Known limitations:

- The defining module must be imported in the reconstructing process (a
  clear error is raised otherwise).
- ``AliasTask`` embeds a pickled ``loads_type`` and is therefore not
  pickle-free — payloads containing ``__aliased`` data (at any nesting
  level) are **rejected**: unpickling registry-supplied bytes without
  user action would let a compromised registry execute code in the
  reconstructing process. Use ``BaseTask.from_registry`` for the explicit,
  user-invoked path.
- Fields whose custom serializers are not losslessly round-trippable
  reconstruct to a *different* task identity — guarded by the optional
  ``expected_task_id`` check, and caught before registration by
  ``check_serialization_stability``.
- Nested task fields must use the polymorphic annotations
  (``sd.TaskLoads[T]`` / ``sd.SubClass[...]``) — a plain task-typed
  annotation validates children into the abstract base class.
"""

from __future__ import annotations

import typing
from uuid import UUID

if typing.TYPE_CHECKING:
    from stardag.registry import RegistryABC

from pydantic import TypeAdapter

from stardag._core.base_task import BaseTask
from stardag._core.task_id import same_task_id
from stardag.base_model import CONTEXT_MODE_KEY
from stardag.exceptions import StardagError
from stardag.polymorphic import NAME_KEY, NAMESPACE_KEY, SubClass, TypeId


class TaskRehydrationError(StardagError):
    """A task could not be reconstructed from its registry data."""


# Marker for AliasTask payloads (see BaseTask.resolve): their ``loads_type``
# field is pickled bytes, which must never be unpickled automatically from
# registry-supplied data.
_ALIASED_KEY = "__aliased"


def _contains_aliased(value: typing.Any) -> bool:
    if isinstance(value, typing.Mapping):
        return _ALIASED_KEY in value or any(
            _contains_aliased(v) for v in value.values()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_aliased(v) for v in value)
    return False


def _unregistered_task_types(value: typing.Any) -> set[tuple[str, str]]:
    """Every ``(namespace, name)`` pair in ``value`` (nested dicts and lists
    included) that names no registered task class.

    Read only after validation has failed, as candidates for the class
    behind it; the caller keeps only those the validation error names.
    """
    found: set[tuple[str, str]] = set()
    if isinstance(value, typing.Mapping):
        ns, n = value.get(NAMESPACE_KEY), value.get(NAME_KEY)
        if isinstance(ns, str) and isinstance(n, str):
            try:
                BaseTask._registry().get_class(TypeId(namespace=ns, name=n))
            except KeyError:
                found.add((ns, n))
        for v in value.values():
            found |= _unregistered_task_types(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            found |= _unregistered_task_types(v)
    return found


def _label(namespace: str, name: str) -> str:
    """``namespace.Name``, or just ``Name`` for the root namespace."""
    return f"{namespace}.{name}" if namespace else name


_EVOLVE_DOCS = "https://stardag-dev.github.io/stardag/how-to/evolve-dags/"
_COMPAT_DEFAULT_DOCS = "https://stardag-dev.github.io/stardag/concepts/parameters/"


def _id_mismatch_message(
    task: BaseTask, rehydrated_id: typing.Any, expected_id: typing.Any
) -> str:
    return (
        f"The stored body of {type(task).__name__} recomputes to task id "
        f"{rehydrated_id}, which does not match the expected {expected_id}: "
        "the class's "
        "significant fields no longer match the ones the body was stored "
        "with. Common causes: a significant field was added without a "
        "StardagField(compat_default=...) (see "
        f"{_COMPAT_DEFAULT_DOCS}), or one was removed or renamed; less "
        "often, a field whose serialization does not round-trip. See "
        f"'Evolve a DAG Safely': {_EVOLVE_DOCS}"
    )


# Validates any dict carrying the polymorphic discriminator keys into the
# registered concrete BaseTask subclass (same machinery as nested task
# fields). Module-level: TypeAdapter construction is not free.
_TASK_ADAPTER: TypeAdapter[BaseTask] = TypeAdapter(SubClass[BaseTask])


def task_from_body(
    body: typing.Mapping[str, typing.Any],
    *,
    expected_task_id: str | UUID | None = None,
) -> BaseTask:
    """Reconstruct a task object from a stored instance body.

    The supported way to validate a body kept anywhere: one read from the
    registry (``stardag tasks show <id> --json``, ``registry.task_get``,
    ``registry.task_instance_get``) or a copy of ``task.instance_body()``
    saved by the user. The body must carry the polymorphic discriminator
    keys (``__namespace`` / ``__name``), as every instance body does.

    Validated in **compat mode**: strict for significant fields through the
    task id check, lenient for non-significant ones (an unknown key is
    dropped with a warning, a missing one takes the class default). That is
    what lets a body stored by older code be re-read under newer code.

    Every task class in the body, nested ones included, must be importable
    and imported: classes register when they are defined. A missing one is
    reported by namespace and name.

    Args:
        body: The instance body.
        expected_task_id: When given, the reconstructed task object's
            (recomputed) task id must match — catching lossy
            serialization round-trips, or a significant field the class
            no longer declares, that would otherwise silently yield a
            different completion identity. Pass it whenever the id is
            known.

    Returns:
        The reconstructed task object, of the concrete class the body names.

    Raises:
        TaskRehydrationError: If the task class is not registered (module
            not imported), the body doesn't validate, or the identity check
            fails.
    """
    return task_from_registry_data(body, expected_task_id=expected_task_id)


def task_from_registry_data(
    task_data: typing.Mapping[str, typing.Any],
    *,
    expected_task_id: str | UUID | None = None,
) -> BaseTask:
    """Reconstruct a task object from an instance body.

    The engine-side name of :func:`task_from_body`, kept for existing
    callers; the two are the same function. The body is the payload stored
    at registration for one instance (also served on the frontier for every
    member the tick acts on): ``task.instance_body()``, including the
    polymorphic discriminator keys.

    Args:
        task_data: The instance body.
        expected_task_id: When given, the reconstructed task object's
            (recomputed) task id must match — catching lossy
            serialization round-trips, or a significant field the class
            no longer declares, that would otherwise silently yield a
            different completion identity.

    Raises:
        TaskRehydrationError: If the task class is not registered (module
            not imported), the payload doesn't validate, or the identity
            check fails.
    """
    namespace = task_data.get(NAMESPACE_KEY)
    name = task_data.get(NAME_KEY)
    if name is None or namespace is None:
        # Both keys are always present in a full dump (the root namespace
        # serializes as "", not as an absent key).
        raise TaskRehydrationError(
            "task_data is missing the polymorphic discriminator keys "
            f"({NAMESPACE_KEY!r}/{NAME_KEY!r}) — it must be the full "
            "payload stored at registration, not a partial dump."
        )
    # Security: AliasTask payloads carry a pickled loads_type that the
    # polymorphic resolve path would unpickle — from registry-supplied
    # bytes, with no user action when called from scheduler ticks. Reject
    # them anywhere in the payload (nested task fields resolve through the
    # same path, so a top-level check is not enough).
    if _contains_aliased(task_data):
        raise TaskRehydrationError(
            "task_data contains AliasTask ('__aliased') payloads, which "
            "embed pickled bytes and cannot be rehydrated from registry "
            "data. AliasTask is pickle-bound; use the explicit "
            "BaseTask.from_registry path if you trust the source."
        )
    # Pre-resolve for a clear module-not-imported error (the adapter's own
    # KeyError is less actionable).
    try:
        BaseTask._registry().get_class(TypeId(namespace=namespace, name=name))
    except KeyError as e:
        raise TaskRehydrationError(
            f"No task class registered for namespace={namespace!r}, "
            f"name={name!r}. The module defining the task class must be "
            "imported before rehydration (task classes register at "
            "definition time)."
        ) from e

    try:
        task = _TASK_ADAPTER.validate_python(
            dict(task_data), context={CONTEXT_MODE_KEY: "compat"}
        )
    except Exception as e:
        # Only the candidates the error itself names: a nested polymorphic
        # value that is not a task (a ``SubClass`` of another model family)
        # lives in its own registry, so the scan alone would report it as
        # missing when some unrelated field is what failed.
        cause = str(e)
        missing = {
            (ns, n)
            for ns, n in _unregistered_task_types(task_data)
            if f"name={n!r}" in cause and f"namespace={ns!r}" in cause
        }
        if missing:
            listed = ", ".join(
                f"{n} (namespace {ns or '<root>'})" for ns, n in sorted(missing)
            )
            raise TaskRehydrationError(
                f"Cannot rehydrate {_label(namespace, name)}: no task class is "
                f"registered for {listed}, used in its parameters. Import the "
                "module that defines each of these classes before loading "
                "the task (task classes register when they are defined)."
            ) from e
        raise TaskRehydrationError(
            f"Cannot rehydrate {_label(namespace, name)}: the stored body does "
            f"not validate against the class: {e}"
        ) from e

    if expected_task_id is not None:
        try:
            rehydrated_id = task.id
        except Exception as e:
            # Typically a nested task field with a PLAIN task annotation
            # (e.g. ``deps: tuple[Task, ...]``): the child validates into
            # the abstract base instead of the concrete class and fails on
            # serialization. Polymorphic annotations (``sd.TaskLoads[T]`` /
            # ``sd.SubClass[...]``) reconstruct correctly.
            raise TaskRehydrationError(
                f"Rehydrated {namespace!r}.{name!r} is not serializable "
                f"({e}). Nested task fields must use polymorphic "
                "annotations (sd.TaskLoads / sd.SubClass) to be "
                "reconstructable from registry data."
            ) from e
        if not same_task_id(rehydrated_id, expected_task_id):
            raise TaskRehydrationError(
                _id_mismatch_message(task, rehydrated_id, expected_task_id)
            )
    return task


def _validate_for(
    cls: type[BaseTask],
    body: typing.Mapping[str, typing.Any],
    *,
    allow_aliased: bool = True,
) -> BaseTask:
    """Validate ``body`` into the class it names, which must be ``cls`` or a
    subclass of it (a body without discriminator keys validates into
    ``cls``).

    The class is resolved from the body rather than taken from ``cls``:
    ``Base.from_registry`` then returns the concrete subclass the task was
    registered as, a body of an unrelated class is refused by name rather
    than as a task id mismatch, and a task class (nested ones included)
    whose module is not imported is named.

    ``allow_aliased=False`` refuses an ``AliasTask`` payload anywhere in the
    body before anything is validated: its pickled ``loads_type`` would be
    unpickled by validation. For loads no user asked for explicitly (a
    ``TaskRef`` hydrated at build start), as in
    :func:`task_from_registry_data`.
    """
    if not allow_aliased and _contains_aliased(body):
        raise TaskRehydrationError(
            "The stored body contains AliasTask ('__aliased') payloads, which "
            "embed pickled bytes and are not rehydrated automatically. Load it "
            "explicitly with from_registry if you trust the source."
        )
    target: type[BaseTask] = cls
    namespace, name = body.get(NAMESPACE_KEY), body.get(NAME_KEY)
    own_type_id = getattr(cls, "__type_id__", None)
    if (
        isinstance(namespace, str)
        and isinstance(name, str)
        and own_type_id != TypeId(namespace=namespace, name=name)
    ):
        try:
            named = BaseTask._registry().get_class(
                TypeId(namespace=namespace, name=name)
            )
        except KeyError as e:
            raise TaskRehydrationError(
                f"No task class registered for namespace={namespace!r}, "
                f"name={name!r}. The module defining the task class must be "
                "imported before rehydration (task classes register at "
                "definition time)."
            ) from e
        if not (isinstance(named, type) and issubclass(named, cls)):
            raise TaskRehydrationError(
                f"The stored body is a {named.__module__}.{named.__qualname__} "
                f"(namespace={namespace!r}, name={name!r}), not a "
                f"{cls.__module__}.{cls.__qualname__}."
            )
        target = named
    try:
        return target.model_validate(body, context={CONTEXT_MODE_KEY: "compat"})
    except Exception as e:
        missing = _unregistered_task_types(body)
        if not missing:
            raise
        listed = ", ".join(f"namespace={ns!r}, name={n!r}" for ns, n in sorted(missing))
        raise TaskRehydrationError(
            f"Failed to validate the stored body of {target.__qualname__}: no "
            f"task class registered for the nested {listed}. The module "
            "defining each task class in the body must be imported before "
            "rehydration (task classes register at definition time)."
        ) from e


def _id_mismatch(task: BaseTask, task_id: UUID) -> TaskRehydrationError:
    return TaskRehydrationError(_id_mismatch_message(task, task.id, task_id))


def load_from_registry(
    cls: type[BaseTask],
    task_id: UUID,
    registry: "RegistryABC",
    *,
    instance_id: UUID | str | None = None,
    allow_aliased: bool = True,
) -> BaseTask:
    """``BaseTask.from_registry``: see there. ``allow_aliased=False``
    refuses ``AliasTask`` payloads (see :func:`_validate_for`)."""
    if instance_id is None:
        info = registry.task_get(str(task_id))
        if info.body is None:
            raise TaskRehydrationError(
                f"The registry holds no instance body for task {task_id}."
            )
        task = _validate_for(cls, info.body, allow_aliased=allow_aliased)
        if task.id != task_id:
            raise _id_mismatch(task, task_id)
        return task

    from stardag.registry import _instance_cache

    iid = UUID(str(instance_id))
    partition = _instance_cache.registry_partition(registry)
    for attempt in (1, 2):
        # Whether this read is served from the cache: only a cached copy
        # is worth reading again when it fails, since a body fresh from the
        # registry would come back the same.
        cached = partition is not None and _instance_cache.has_entry(partition, iid)
        instance = registry.task_instance_get(iid, task_id=str(task_id))
        if instance.task_id is not None and not same_task_id(instance.task_id, task_id):
            raise TaskRehydrationError(
                f"Instance {iid} belongs to task {instance.task_id}, "
                f"not to the requested task {task_id}."
            )
        error: TaskRehydrationError
        try:
            task = _validate_for(cls, instance.body, allow_aliased=allow_aliased)
            rehydrated_id = task.id
        except Exception as e:
            error = TaskRehydrationError(
                f"Failed to rehydrate instance {iid} of task {task_id}: {e}"
            )
            error.__cause__ = e
        else:
            if rehydrated_id == task_id:
                return task
            error = _id_mismatch(task, task_id)
        # A body that does not validate, or no longer recomputes to its
        # task id, is not kept. If it was a cached copy, the copy may be
        # what is wrong: read the registry's once more.
        if partition is not None:
            _instance_cache.discard(partition, iid)
        if not cached or attempt == 2:
            raise error
    raise AssertionError("unreachable")  # pragma: no cover
