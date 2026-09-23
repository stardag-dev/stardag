"""Instance bodies: the canonical body, its stability check, and conflict
detection within one discovery pass.

Vocabulary, kept apart everywhere: an **instance** is a registry row — a
construction of a task under a deterministic scope, stored as its *body*
(the registry-mode dump: every field, defaults included). The Python object
is a **task object**. One task object planned under two scopes is two
instances; one instance rehydrates into any number of task objects. The
instance hash is the hash of the body and is never a public identifier on
its own: the registry keys the row by the scope *and* the hash.

- :func:`canonical_instance_body_json` is the one place the body is dumped;
  ``BaseTask.instance_hash`` and the registration payload both read it, so
  the hash is the hash of the bytes that are stored.
- :func:`check_serialization_stability` is the round trip the driver runs
  once per distinct instance at registration.
- :class:`SeenInstances` tracks instances by task id during a discovery pass
  and raises :class:`~stardag.exceptions.InstanceConflictError` on a second,
  different construction of one task id.
"""

from __future__ import annotations

import json
import math
from typing import TYPE_CHECKING, Any
from uuid import UUID

from pydantic import ValidationError

from stardag._core.task_id import canonical_body_json
from stardag.base_model import CONTEXT_MODE_KEY
from stardag.exceptions import InstanceConflictError, UnstableSerializationError

if TYPE_CHECKING:
    from stardag._core.base_task import BaseTask


def _class_label(task: "BaseTask") -> str:
    return f"{task.get_namespace()}.{task.get_name()}".lstrip(".")


def canonical_instance_body_json(task: "BaseTask") -> str:
    """The canonical JSON of ``task``'s instance body.

    The body is the registry-mode dump (``stardag.base_model``): every
    field, defaults included, sets sorted, nested tasks as their full
    bodies. Canonical: sorted keys, compact separators, UTF-8.

    Raises:
        UnstableSerializationError: The body holds a ``NaN`` or an infinity,
            which is not JSON and could not be stored.
    """
    body = task.model_dump(mode="json", context={CONTEXT_MODE_KEY: "registry"})
    try:
        return canonical_body_json(body)
    except ValueError as e:
        fields = tuple(_non_finite_paths(body))
        raise UnstableSerializationError(
            f"{_class_label(task)}: the instance body holds a non-finite float "
            f"at {', '.join(fields) or '?'}; NaN and infinities are not JSON "
            "and cannot be stored. Use None or a finite sentinel instead.",
            task_class=_class_label(task),
            fields=fields,
        ) from e


def check_serialization_stability(task: "BaseTask") -> None:
    """Check that ``task``'s body is a fixed point of its own round trip.

    ``dump(validate(dump(x))) == dump(x)``, where ``dump`` is the canonical
    instance body and ``validate`` is rehydration (compat mode, through the
    polymorphic adapter, as ``task_from_registry_data`` does). Also checks
    that the rehydrated task object has the same task id, since
    rehydration is strict on it. The driver calls this once per distinct
    instance at registration (so a trigger fails instead of two processes
    registering two instances of one construction later).

    Raises:
        UnstableSerializationError: naming the field(s) whose value moved.
    """
    # Local import: rehydrate imports base_task, which imports this module
    # lazily.
    from stardag._core.rehydrate import _TASK_ADAPTER

    label = _class_label(task)
    body_json = canonical_instance_body_json(task)
    body = json.loads(body_json)
    try:
        rebuilt = _TASK_ADAPTER.validate_python(
            body, context={CONTEXT_MODE_KEY: "compat"}
        )
    except ValidationError as e:
        fields = tuple(
            dict.fromkeys(".".join(str(p) for p in err["loc"]) for err in e.errors())
        )
        raise UnstableSerializationError(
            f"{label}: the instance body does not validate back into the task "
            f"(fields {', '.join(fields)}): {e}",
            task_class=label,
            fields=fields,
        ) from e

    rebuilt_json = canonical_instance_body_json(rebuilt)
    if rebuilt_json != body_json:
        fields = tuple(body_diff(body, json.loads(rebuilt_json)))
        raise UnstableSerializationError(
            f"{label}: the instance body moves when re-read — field(s) "
            f"{', '.join(fields)} serialize differently after a round trip. "
            "A custom serializer or validator is not idempotent (a naive vs "
            "aware datetime is the usual cause).",
            task_class=label,
            fields=fields,
        )

    if rebuilt.id != task.id:
        fields = tuple(
            name
            for name in type(task).model_fields
            if _differs(getattr(task, name), getattr(rebuilt, name, None))
        )
        raise UnstableSerializationError(
            f"{label}: the task id moves when the instance body is re-read "
            f"({task.id} -> {rebuilt.id}); field(s) "
            f"{', '.join(fields) or '(unidentified)'} lose information in the "
            "body that the task id depends on (a serializer dropping "
            "precision, or a hash-mode serializer that disagrees with the "
            "ordinary one).",
            task_class=label,
            fields=fields,
        )


def _differs(a: Any, b: Any) -> bool:
    try:
        return bool(a != b)
    except Exception:  # pragma: no cover - exotic __eq__
        return True


def extend_path(parent_path: str | None, task: "BaseTask") -> str:
    """The construction path of ``task`` reached from ``parent_path``:
    ``"Root[1a2b3c4d] -> Mid[5e6f7a8b] -> Leaf[9c0d1e2f]"`` (task name and
    the first eight hex digits of its task id, root first)."""
    label = f"{task.get_name()}[{task.id.hex[:8]}]"
    return label if parent_path is None else f"{parent_path} -> {label}"


class SeenInstances:
    """The instances seen in one discovery pass, keyed by task id.

    A plan may hold only one instance per task id. :meth:`observe` is called
    for every task object the walk reaches; the first construction of a task
    id is recorded, a later one with the same instance hash is a no-op, and
    a later one with a different instance hash raises
    :class:`~stardag.exceptions.InstanceConflictError` naming the fields that
    differ between the two bodies and, when given, both construction paths.
    """

    def __init__(self) -> None:
        self._first: dict[UUID, tuple["BaseTask", str | None]] = {}

    def __len__(self) -> int:
        return len(self._first)

    def __contains__(self, task_id: object) -> bool:
        return task_id in self._first

    def path_of(self, task_id: UUID) -> str | None:
        """The construction path recorded for ``task_id``'s first
        construction, if it was observed with one."""
        seen = self._first.get(task_id)
        return None if seen is None else seen[1]

    def observe(self, task: "BaseTask", path: str | None = None) -> bool:
        """Record ``task``; return True the first time its task id is seen.

        Args:
            task: The task object reached by the walk.
            path: How it was reached, root first (see :func:`extend_path`),
                for the error message. Optional.

        Raises:
            InstanceConflictError: ``task`` has the task id of an earlier
                construction and a different instance hash.
        """
        seen = self._first.get(task.id)
        if seen is None:
            self._first[task.id] = (task, path)
            return True
        first, first_path = seen
        if first is task or first.instance_hash == task.instance_hash:
            return False
        fields = tuple(body_diff(first.instance_body(), task.instance_body()))
        paths_note = ""
        if first_path is not None or path is not None:
            paths_note = (
                f" First reached via {first_path or '?'}; then via {path or '?'}."
            )
        raise InstanceConflictError(
            f"Task {_class_label(task)} {task.id} is constructed twice in one "
            f"build with different parameters ({', '.join(fields)}). A build "
            "plans one instance per task id: the two constructions ask for one "
            "completion in two ways. Pass the same values of these "
            "(non-significant) fields to every construction, or make a field "
            f"that should separate them significant.{paths_note}",
            task_id=str(task.id),
            fields=fields,
            paths=(first_path, path),
        )


def body_diff(a: Any, b: Any, prefix: str = "") -> list[str]:
    """Dotted paths at which two JSON bodies differ.

    Dicts recurse by key, equal-length lists by index (``items[2]``); a
    differing leaf, a key present on one side only, or lists of different
    length are reported at their own path.
    """
    if isinstance(a, dict) and isinstance(b, dict):
        out: list[str] = []
        for key in sorted(set(a) | set(b)):
            path = f"{prefix}.{key}" if prefix else str(key)
            if key not in a or key not in b:
                out.append(path)
            else:
                out.extend(body_diff(a[key], b[key], path))
        return out
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        out = []
        for i, (x, y) in enumerate(zip(a, b)):
            out.extend(body_diff(x, y, f"{prefix}[{i}]"))
        return out
    if type(a) is not type(b) or a != b:
        return [prefix or "<root>"]
    return []


def _non_finite_paths(obj: Any, prefix: str = "") -> list[str]:
    if isinstance(obj, float) and not math.isfinite(obj):
        return [prefix or "<root>"]
    if isinstance(obj, dict):
        return [
            p
            for key, value in obj.items()
            for p in _non_finite_paths(value, f"{prefix}.{key}" if prefix else key)
        ]
    if isinstance(obj, (list, tuple)):
        return [
            p
            for i, value in enumerate(obj)
            for p in _non_finite_paths(value, f"{prefix}[{i}]")
        ]
    return []
