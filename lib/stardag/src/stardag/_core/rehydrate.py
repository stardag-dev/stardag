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

from pydantic import TypeAdapter

from stardag._core.base_task import BaseTask
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


# Validates any dict carrying the polymorphic discriminator keys into the
# registered concrete BaseTask subclass (same machinery as nested task
# fields). Module-level: TypeAdapter construction is not free.
_TASK_ADAPTER: TypeAdapter[BaseTask] = TypeAdapter(SubClass[BaseTask])


def task_from_registry_data(
    task_data: typing.Mapping[str, typing.Any],
    *,
    expected_task_id: str | UUID | None = None,
) -> BaseTask:
    """Reconstruct a task object from an instance body.

    The body is the payload stored at registration for one instance (also
    available as ``TaskMetadata.body`` from ``task_get_metadata``):
    ``task.instance_body()``, including the polymorphic discriminator keys.
    Validated in compat mode: strict for significant fields through the
    task id check, lenient for non-significant ones (an unknown key is
    dropped with a warning, a missing one takes the class default).

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
        raise TaskRehydrationError(
            f"Failed to validate task_data for {namespace!r}.{name!r}: {e}"
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
        if str(rehydrated_id) != str(expected_task_id):
            raise TaskRehydrationError(
                f"Rehydrated task id {rehydrated_id} does not match the "
                f"expected id {expected_task_id} — a field's serialization "
                "is likely not losslessly round-trippable."
            )
    return task
