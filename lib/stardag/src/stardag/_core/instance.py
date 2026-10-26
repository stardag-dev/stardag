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
- :func:`state_outside_body` finds state a task object holds that the task
  rebuilt from its body does not (a private attribute set at runtime, an
  excluded field). Whatever is rebuilt from the body runs without it: the
  reactive pre-flight refuses such a task, and the Modal payload keeps it
  pickled. :func:`reachable_models` collects every model in a task.
"""

from __future__ import annotations

import dataclasses
import json
import math
from collections import deque
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from uuid import UUID

from pydantic import BaseModel, TypeAdapter, ValidationError

from stardag._core.task_id import canonical_body_json
from stardag.base_model import CONTEXT_MODE_KEY
from stardag.exceptions import InstanceConflictError, UnstableSerializationError

if TYPE_CHECKING:
    from stardag._core.base_task import BaseTask


def reachable_models(value: Any) -> list[BaseModel]:
    """Every pydantic model instance reachable from ``value`` through its
    fields, and through dataclasses and containers holding models.

    ``value`` itself comes first when it is a model, so a task's nested
    tasks and parameter models are all in the result.
    """
    found: list[BaseModel] = []
    seen: set[int] = set()
    stack = [value]
    while stack:
        item = stack.pop()
        if id(item) in seen:
            continue
        seen.add(id(item))
        if isinstance(item, BaseModel):
            found.append(item)
            # Declared fields only: ``__dict__`` may also hold cached values
            # that are not part of the body.
            stack.extend(
                item.__dict__[name]
                for name in type(item).model_fields
                if name in item.__dict__
            )
        elif dataclasses.is_dataclass(item) and not isinstance(item, type):
            stack.extend(getattr(item, f.name, None) for f in dataclasses.fields(item))
        elif isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, (list, tuple, deque, set, frozenset)):
            stack.extend(item)
    return found


def state_outside_body(original: BaseModel, rebuilt: Any) -> str | None:
    """A description of state ``original`` holds that ``rebuilt`` lost, if any.

    ``rebuilt`` is ``original`` rehydrated from its instance body, the way a
    reactive tick or a Modal receiver rebuilds it. The body carries neither
    private attributes nor fields declared with ``exclude=True``, so those
    are compared between the two, in ``original`` and every model nested in
    it through fields, dataclasses and containers. Comparing against the
    rebuilt object rather than the declared defaults is what makes the
    answer exact: a default that validation normalises, a ``compat_default``
    that rehydration fills in for the missing key, and a private attribute
    ``model_post_init`` derives from fields are all reproduced by the
    rebuild, or not, exactly as they will be on the far side.

    **Where the rebuild has no counterpart, the declared default stands in
    for it.** A model in an ``Any`` field comes back as a ``dict``, a
    subclass in a field annotated with its base comes back as the base, and
    a container may not pair up item by item. Such a model's state is lost
    in the rebuild unless it was at its default anyway, so that is what it
    is compared against: conservative, never silent.

    The description names the class and the attribute, e.g.
    ``"MyTask._cache is runtime state"``. A comparison that raises counts
    as a difference: the answer has to err toward "the rebuild would lose
    something".
    """
    # Keyed by the pair, not the original alone: one object shared by two
    # fields can be rebuilt twice, once faithfully and once lossily (as a
    # dict in an ``Any`` field), and each rebuild has to be checked.
    seen: set[tuple[int, int]] = set()
    stack: list[tuple[Any, Any]] = [(original, rebuilt)]
    while stack:
        item, other = stack.pop()
        if (id(item), id(other)) in seen:
            continue
        seen.add((id(item), id(other)))
        if isinstance(item, BaseModel):
            state = _model_state_lost(item, other)
            if state is not None:
                return state
            # Excluded fields too: one whose value compares equal to its
            # counterpart (or default) can still hold a model whose own
            # private state the rebuild does not reproduce.
            stack.extend(
                (item.__dict__[name], _field_of(other, name))
                for name in type(item).model_fields
                if name in item.__dict__
            )
        elif dataclasses.is_dataclass(item) and not isinstance(item, type):
            stack.extend(
                (getattr(item, f.name, None), getattr(other, f.name, _MISSING))
                for f in dataclasses.fields(item)
            )
        elif isinstance(item, dict):
            theirs = other if isinstance(other, dict) else {}
            stack.extend((v, theirs.get(k, _MISSING)) for k, v in item.items())
        elif isinstance(item, (list, tuple, deque)):
            if isinstance(other, (list, tuple, deque)) and len(other) == len(item):
                stack.extend(zip(item, other))
            else:
                stack.extend((v, _MISSING) for v in item)
        elif isinstance(item, (set, frozenset)):
            # No order to pair by: pair the members by a canonical key of
            # their fields, which the rebuild reproduces (the body is what it
            # was built from), and which leaves out exactly the state
            # compared here.
            theirs_by_key = (
                {_member_key(v): v for v in other}
                if isinstance(other, (set, frozenset))
                else {}
            )
            stack.extend((v, theirs_by_key.get(_member_key(v), _MISSING)) for v in item)
    return None


_MISSING: Any = object()
"""No counterpart in the rebuild: compare against the declared default."""

_ANY_ADAPTER: TypeAdapter[Any] = TypeAdapter(Any)


def _model_state_lost(item: BaseModel, other: Any) -> str | None:
    cls = type(item)
    mine = getattr(item, "__pydantic_private__", None) or {}
    theirs = (
        getattr(other, "__pydantic_private__", None) or {}
        if isinstance(other, BaseModel)
        else {}
    )
    for name, attribute in cls.__private_attributes__.items():
        if name not in mine:
            continue
        if name in theirs:
            lost = _differs_from(mine[name], lambda: theirs[name])
        else:
            lost = _differs_from(mine[name], attribute.get_default)
        if lost:
            return f"{cls.__qualname__}.{name} is runtime state"
    for name, info in cls.model_fields.items():
        if not info.exclude or name not in item.__dict__:
            continue
        counterpart = _field_of(other, name)
        if counterpart is _MISSING:
            lost = _differs_from(
                item.__dict__[name],
                lambda: info.get_default(call_default_factory=True),
            )
        else:
            lost = _differs_from(item.__dict__[name], lambda: counterpart)
        if lost:
            return f"{cls.__qualname__}.{name} is excluded from the body"
    return None


def _field_of(other: Any, name: str) -> Any:
    """``other``'s value for field ``name``: from a model, or from the dict
    a model in an untyped field is rebuilt as."""
    if isinstance(other, BaseModel):
        return other.__dict__.get(name, _MISSING)
    if isinstance(other, dict):
        return other.get(name, _MISSING)
    return _MISSING


def _differs_from(value: Any, reference: Callable[[], Any]) -> bool:
    try:
        return bool(value != reference())
    except Exception:
        return True


def _member_key(value: Any) -> tuple[Any, ...]:
    """A canonical, order-free key for a set member.

    Built from the value's structure rather than one dump of it, so a set
    nested in the member pairs whatever order it iterates in, and a model
    is keyed by its body fields only: its private attributes and excluded
    fields are what is being compared, and must not stop it pairing.
    """
    if isinstance(value, BaseModel):
        cls = type(value)
        return (
            "model",
            f"{cls.__module__}.{cls.__qualname__}",
            tuple(
                (name, _member_key(value.__dict__[name]))
                for name, info in cls.model_fields.items()
                if not info.exclude and name in value.__dict__
            ),
        )
    if isinstance(value, (set, frozenset)):
        return ("set", tuple(sorted((_member_key(v) for v in value), key=repr)))
    if isinstance(value, (list, tuple, deque)):
        return ("seq", tuple(_member_key(v) for v in value))
    if isinstance(value, dict):
        items = ((repr(_member_key(k)), _member_key(v)) for k, v in value.items())
        return ("dict", tuple(sorted(items, key=lambda kv: kv[0])))
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return (
            "dataclass",
            f"{type(value).__module__}.{type(value).__qualname__}",
            tuple(
                (f.name, _member_key(getattr(value, f.name, None)))
                for f in dataclasses.fields(value)
            ),
        )
    try:
        dumped = _ANY_ADAPTER.dump_json(
            value, context={CONTEXT_MODE_KEY: "registry"}
        ).decode()
    except Exception:
        dumped = repr(value)
    return ("leaf", f"{type(value).__module__}.{type(value).__qualname__}", dumped)


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
        UnstableSerializationError: naming the field(s) whose value moved, or
            reporting an ``AliasTask`` payload that registry rehydration
            would refuse.
    """
    # Local import: rehydrate imports base_task, which imports this module
    # lazily.
    from stardag._core.rehydrate import _TASK_ADAPTER

    label = _class_label(task)
    body_json = canonical_instance_body_json(task)
    body = json.loads(body_json)

    # Validate through the same gate as registry rehydration
    # (task_from_registry_data): an AliasTask body embeds pickled bytes and
    # is refused there, even though it round-trips fine through the
    # polymorphic adapter directly (which unpickles it). Without this, such
    # a body would pass this check and then be un-rehydratable from the
    # registry.
    aliased_fields = tuple(_aliased_paths(body))
    if aliased_fields:
        raise UnstableSerializationError(
            f"{label}: the instance body contains an AliasTask payload "
            f"(field(s) {', '.join(aliased_fields)}) — AliasTask embeds "
            "pickled bytes, which registry rehydration (task_from_registry_"
            "data) refuses to unpickle from stored data. Such an instance "
            "can be registered but never rehydrated back; use a task class "
            "that does not carry an AliasTask in its body.",
            task_class=label,
            fields=aliased_fields,
        )

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


def _aliased_paths(obj: Any, prefix: str = "") -> list[str]:
    """Dotted paths at which an ``__aliased`` key appears, at any nesting
    level (mirrors ``rehydrate._contains_aliased``, but reports *where*)."""
    from stardag._core.rehydrate import _ALIASED_KEY

    if isinstance(obj, dict):
        out: list[str] = []
        if _ALIASED_KEY in obj:
            out.append(prefix or "<root>")
        for key, value in obj.items():
            out.extend(_aliased_paths(value, f"{prefix}.{key}" if prefix else str(key)))
        return out
    if isinstance(obj, (list, tuple)):
        return [
            p
            for i, value in enumerate(obj)
            for p in _aliased_paths(value, f"{prefix}[{i}]")
        ]
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
