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
  rebuilt from its body does not (a private attribute, an excluded field),
  and says whether each difference is decidable. Whatever is rebuilt from
  the body runs without it: the reactive pre-flight refuses or warns on it
  (``plan_rehydration``), and the Modal payload keeps such a task pickled.
  :func:`reachable_models` collects every model in a task.
"""

from __future__ import annotations

import collections.abc
import dataclasses
import datetime
import decimal
import enum
import json
import math
import pathlib
import types
import typing
from collections import deque
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ValidationError

from stardag._core.task_id import canonical_body_json
from stardag.base_model import _UNSET, CONTEXT_MODE_KEY, StardagField
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


@dataclasses.dataclass(frozen=True)
class LostState:
    """One attribute whose value the task rebuilt from its body does not
    reproduce: a private attribute, or a field declared ``exclude=True``.

    ``decidable`` says whether the difference is a fact or a guess. It is a
    fact only when both values are plain data (see :func:`_is_plain_data`),
    are not the same object and compare unequal. Anything else (an object
    without value equality, such as a lock, a client or a fresh ``uuid4``;
    a comparison that raises, such as a numpy array's) may differ or not,
    and a caller must not refuse on it.
    """

    owner: str
    """``module.qualname`` of the model declaring the attribute."""
    attribute: str
    kind: Literal["private", "excluded"]
    decidable: bool
    compat_default: bool = False
    """An excluded field with a ``compat_default``: the rebuild takes that
    value, whatever the field held."""

    def describe(self) -> str:
        owner = self.owner.rpartition(".")[2] or self.owner
        if self.kind == "private":
            return f"{owner}.{self.attribute} is runtime state"
        return f"{owner}.{self.attribute} is excluded from the body"


def state_outside_body(original: BaseModel, rebuilt: Any) -> list[LostState]:
    """The state ``original`` holds that ``rebuilt`` does not reproduce.

    ``rebuilt`` is ``original`` rehydrated from its instance body, the way a
    reactive tick or a Modal receiver rebuilds it. The body carries neither
    private attributes nor fields declared with ``exclude=True``, so those
    are compared between the two, in ``original`` and every model nested in
    it. Comparing against the rebuilt object rather than the declared
    defaults is what keeps the answer honest: a default that validation
    normalises, a ``compat_default`` that rehydration fills in for the
    missing key, and a private attribute ``model_post_init`` derives from
    fields are all reproduced by the rebuild, or not, exactly as they will
    be on the far side.

    Where the rebuild has no counterpart to pair with (a model in an
    ``Any`` field comes back as a ``dict``, a subclass as its declared base,
    a set member), the declared default stands in, and a difference in an
    excluded field found that way is never decidable.

    Returns one :class:`LostState` per differing attribute, in walk order;
    what to do about each is the caller's policy. Cheap for the common
    task: a class declaring no private attribute and no excluded field, and
    no field that could hold a model, is not walked at all (see
    :func:`_class_plan`). Stardag's own class-level private attributes
    (``Task._load_validators``) are not task state and are ignored.
    """
    if isinstance(original, BaseModel) and _class_plan(type(original)).inert:
        return []
    found: list[LostState] = []
    seen: set[tuple[int, int]] = set()
    stack: list[tuple[Any, Any]] = [(original, rebuilt)]
    while stack:
        item, other = stack.pop()
        if type(item) in _LEAF_TYPES:
            continue
        key = (id(item), id(other))
        if key in seen:
            continue
        seen.add(key)
        if isinstance(item, BaseModel):
            plan = _class_plan(type(item))
            if plan.private or plan.excluded:
                found.extend(_model_state_lost(item, other, plan))
            stack.extend(
                (item.__dict__[name], _field_of(other, name))
                for name in plan.walked
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
            # No order to pair members by: each is judged against its
            # declared defaults.
            stack.extend((v, _MISSING) for v in item)
    return found


_MISSING: Any = object()
"""No counterpart in the rebuild: compare against the declared default."""

_LEAF_TYPES: frozenset[type] = frozenset(
    {str, int, float, bool, bytes, type(None), complex}
)

# Annotations that can hold no model or dataclass, so a field typed with
# one never needs walking. Anything else, ``Any`` and every model type
# included (a subclass value can add state), is walked.
_PLAIN_ANNOTATIONS: frozenset[Any] = _LEAF_TYPES | {
    datetime.datetime,
    datetime.date,
    datetime.time,
    datetime.timedelta,
    decimal.Decimal,
    pathlib.Path,
    pathlib.PurePath,
    UUID,
}
_PLAIN_CONTAINER_ORIGINS: frozenset[Any] = frozenset(
    {
        list,
        tuple,
        set,
        frozenset,
        dict,
        deque,
        collections.abc.Sequence,
        collections.abc.Mapping,
        collections.abc.Set,
        collections.abc.Iterable,
        collections.abc.Collection,
        typing.Union,
        types.UnionType,
    }
)


class _ClassPlan(typing.NamedTuple):
    """What :func:`state_outside_body` needs to know about a model class."""

    private: tuple[str, ...]
    """Private attributes declared outside stardag's own task classes."""
    excluded: tuple[str, ...]
    """Fields declared ``exclude=True``."""
    walked: tuple[str, ...]
    """Fields whose annotation could hold a model, so need walking."""

    @property
    def inert(self) -> bool:
        return not (self.private or self.excluded or self.walked)


_class_plans: dict[type, _ClassPlan] = {}


def _class_plan(cls: type[BaseModel]) -> _ClassPlan:
    """Cached per class: the common task class declares no state and no
    field that could hold a model, and is then not walked at all."""
    plan = _class_plans.get(cls)
    if plan is None:
        plan = _class_plans[cls] = _ClassPlan(
            private=tuple(
                name
                for name, attribute in cls.__private_attributes__.items()
                if not _declared_by_stardag(cls, name, attribute)
            ),
            excluded=tuple(
                name for name, info in cls.model_fields.items() if info.exclude
            ),
            walked=tuple(
                name
                for name, info in cls.model_fields.items()
                if not _annotation_is_plain(info.annotation)
            ),
        )
    return plan


def _declared_by_stardag(cls: type, name: str, attribute: Any) -> bool:
    """Whether private attribute ``name`` is inherited from one of stardag's
    own task classes (``Task._load_validators``): class-level machinery, set
    on the class rather than the instance, and never task state."""
    return any(
        klass.__module__.startswith("stardag._core.")
        and getattr(klass, "__private_attributes__", {}).get(name) is attribute
        for klass in cls.__mro__[1:]
    )


def _annotation_is_plain(annotation: Any, depth: int = 0) -> bool:
    if depth > 20:
        return False
    if annotation is None:
        return True
    try:
        if annotation in _PLAIN_ANNOTATIONS:
            return True
    except TypeError:
        # ``Annotated[int, {"unit": "count"}]``: unhashable metadata.
        pass
    if isinstance(annotation, type) and issubclass(annotation, enum.Enum):
        return True
    origin = typing.get_origin(annotation)
    if origin is typing.Literal:
        return True
    if origin is typing.Annotated:
        return _annotation_is_plain(typing.get_args(annotation)[0], depth + 1)
    if origin in _PLAIN_CONTAINER_ORIGINS:
        # A bare ``typing.List`` has no arguments, and can hold anything.
        return bool(typing.get_args(annotation)) and all(
            arg is Ellipsis or _annotation_is_plain(arg, depth + 1)
            for arg in typing.get_args(annotation)
        )
    return False


def _model_state_lost(item: BaseModel, other: Any, plan: _ClassPlan) -> list[LostState]:
    cls = type(item)
    owner = f"{cls.__module__}.{cls.__qualname__}"
    lost: list[LostState] = []
    mine = getattr(item, "__pydantic_private__", None) or {}
    theirs = (
        getattr(other, "__pydantic_private__", None) or {}
        if isinstance(other, BaseModel)
        else {}
    )
    for name in plan.private:
        attribute = cls.__private_attributes__[name]
        if name not in mine:
            continue
        reference = (
            (lambda n=name: theirs[n]) if name in theirs else attribute.get_default
        )
        verdict = _compare(mine[name], reference)
        if verdict is not None:
            lost.append(LostState(owner, name, "private", verdict))
    for name in plan.excluded:
        info = cls.model_fields[name]
        if name not in item.__dict__:
            continue
        counterpart = _field_of(other, name)
        if counterpart is _MISSING:
            # No rebuilt value to compare with, only the declared default,
            # which validation may normalise: a difference is a guess here,
            # never grounds to refuse.
            verdict = _compare(
                item.__dict__[name],
                lambda i=info: i.get_default(call_default_factory=True),
            )
            if verdict:
                verdict = False
        else:
            verdict = _compare(item.__dict__[name], lambda c=counterpart: c)
        if verdict is not None:
            lost.append(
                LostState(
                    owner,
                    name,
                    "excluded",
                    verdict,
                    compat_default=_has_compat_default(info),
                )
            )
    return lost


def _has_compat_default(info: Any) -> bool:
    return any(
        isinstance(m, StardagField) and m.compat_default is not _UNSET
        for m in info.metadata
    )


def _field_of(other: Any, name: str) -> Any:
    """``other``'s value for field ``name``: from a model, or from the dict
    a model in an untyped field is rebuilt as."""
    if isinstance(other, BaseModel):
        return other.__dict__.get(name, _MISSING)
    if isinstance(other, dict):
        return other.get(name, _MISSING)
    return _MISSING


def _compare(value: Any, reference: Callable[[], Any]) -> bool | None:
    """``None`` when ``value`` matches the reference, else whether the
    difference is decidable (compared unequal, and both plain data, so the
    inequality is a fact rather than the absence of value equality)."""
    try:
        ref = reference()
    except Exception:
        return False
    if value is ref:
        return None
    if (
        isinstance(value, float)
        and isinstance(ref, float)
        and math.isnan(value)
        and math.isnan(ref)
    ):
        return None
    try:
        # Equal is equal, plain or not: a fixed UUID or datetime default is
        # deep-copied into each instance, distinct but equal.
        if bool(value == ref):
            return None
    except Exception:
        return False
    return _is_plain_data(value) and _is_plain_data(ref)


def _is_plain_data(value: Any, _seen: set[int] | None = None) -> bool:
    """None, str, int, float, bool or bytes; or a list, tuple, dict, set or
    frozenset of those; or a pydantic model or dataclass made of those.

    Not plain, because equality cannot decide on it: a NaN anywhere (it is
    unequal to itself, so a fresh one in a rebuilt container is no loss), a
    dataclass with identity equality (``eq=False``), and a cycle."""
    if type(value) is float:
        return not math.isnan(value)
    if type(value) is complex:
        return False  # outside the agreed plain-data set
    if type(value) in _LEAF_TYPES:
        return True
    seen = set() if _seen is None else _seen
    if id(value) in seen:
        return False
    seen.add(id(value))
    try:
        if isinstance(value, dict):
            return all(
                _is_plain_data(k, seen) and _is_plain_data(v, seen)
                for k, v in value.items()
            )
        if isinstance(value, (list, tuple, set, frozenset)):
            return all(_is_plain_data(v, seen) for v in value)
        if isinstance(value, BaseModel):
            # Model equality covers private attributes and extras too, so
            # they have to be plain as well for a comparison to decide.
            private = getattr(value, "__pydantic_private__", None) or {}
            extra = getattr(value, "__pydantic_extra__", None) or {}
            return (
                all(
                    _is_plain_data(value.__dict__[name], seen)
                    for name in type(value).model_fields
                    if name in value.__dict__
                )
                and all(_is_plain_data(v, seen) for v in private.values())
                and all(_is_plain_data(v, seen) for v in extra.values())
            )
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            if type(value).__eq__ is object.__eq__:
                return False
            return all(
                _is_plain_data(getattr(value, f.name, None), seen)
                for f in dataclasses.fields(value)
            )
        return False
    finally:
        seen.discard(id(value))


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
        TaskRefNotHydratedError: ``task`` is, or holds, an unhydrated
            ``TaskRef``: a registry stores real bodies only.
    """
    from stardag._core.task_ref import contains_refs, unhydrated_ref_error

    if contains_refs(task):
        raise unhydrated_ref_error(task)
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


def checked_round_trip(task: "BaseTask") -> "BaseTask":
    """Check that ``task``'s body is a fixed point of its own round trip.

    ``dump(validate(dump(x))) == dump(x)``, where ``dump`` is the canonical
    instance body and ``validate`` is rehydration (compat mode, through the
    polymorphic adapter, as ``task_from_registry_data`` does). Also checks
    that the rehydrated task object has the same task id, since
    rehydration is strict on it. The driver calls this once per distinct
    instance at registration (so a trigger fails instead of two processes
    registering two instances of one construction later).

    Returns the rebuilt task object, for a caller that wants to compare
    against it (the Modal payload does) without a second round trip.

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
    return rebuilt


def check_serialization_stability(task: "BaseTask") -> None:
    """Check that ``task``'s body is a fixed point of its own round trip.

    See :func:`checked_round_trip`, which this calls and which also returns
    the rebuilt task.

    Raises:
        UnstableSerializationError: naming the field(s) whose value moved, or
            reporting an ``AliasTask`` payload that registry rehydration
            would refuse.
    """
    checked_round_trip(task)


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
