"""How a task crosses a Modal function call: as its instance body, not a pickle.

Every task that enters a deployed function — a worker's task, the
bootstrap's and the resident builder's roots — is produced by one process
and consumed by another, and the two need not run the same code. Workers
are resolved by name (``modal.Function.from_name``), which always gives the
app's **current** deployment, so a tick or builder still running deployment
N spawns onto N+1's workers; and a trigger serializes its roots under its
own local code, whatever the deployed app runs.

A cloudpickled task survives that badly. Unpickling a pydantic model
restores its ``__dict__`` without validation, so a field added to any model
in the task since the pickle was made is simply absent, and reading it
raises ``AttributeError`` — the change registry rehydration is designed to
absorb (a missing field takes its ``compat_default`` or class default).

So a task is sent as a :data:`TaskPayload`: its instance body, its task id,
and the modules defining every class in it. The receiver imports those
modules and rehydrates with :func:`~stardag.task_from_registry_data` in
compat mode, checking the id: what it runs is exactly what the registry
would rehydrate, and a significant change fails the id check rather than
running a different task.

A task that cannot make that round trip — an ``AliasTask``, a class that is
not importable by reference (``__main__``, a local class), a body that is
not a fixed point of its own round trip (a nested task field with a plain
annotation, a lossy serializer), or state the body does not carry (a private
attribute set at runtime, a field excluded from the dump) — is sent **by
value**, as before, with a warning once per class: it is not protected
across deploys. In a reactive build the bootstrap's pre-flight already
refuses a task a tick could not rehydrate, so the fallback is not taken
there for an unimportable class or an unstable body; runtime state, though,
is not part of what the registry stores, and a tick runs such a task
without it either way.

The payload is a plain ``dict``, so there is no stardag class to unpickle
on the far side and its shape can grow. Receivers keep accepting a pickled
:class:`BaseTask` too: an older deployment's tick still sends one.
"""

from __future__ import annotations

import collections.abc
import dataclasses
import importlib
import logging
import sys
import typing

from pydantic import BaseModel

from stardag._core.base_task import BaseTask
from stardag._core.instance import check_serialization_stability
from stardag._core.rehydrate import TaskRehydrationError, task_from_registry_data
from stardag.polymorphic import NAME_KEY, NAMESPACE_KEY, TypeId
from stardag.build._task_modules import module_is_main

logger = logging.getLogger(__name__)

PAYLOAD_KEY = "__stardag_task_payload__"
"""Marks a dict as a :data:`TaskPayload`; the value is its format version."""

PAYLOAD_VERSION = 1

TaskPayload = dict[str, typing.Any]
"""``{PAYLOAD_KEY: 1, "body": ..., "task_id": ..., "modules": [...]}``."""

TaskOrPayload = BaseTask | TaskPayload

# Task classes already warned about being sent by value (one warning each).
_warned_by_value: set[type] = set()


# Answers of :func:`_is_importable_by_reference`, per class: a class's module
# attributes do not change after import, and the check runs on every spawn.
_importable_cache: dict[type, bool] = {}


def _is_importable_by_reference(cls: type) -> bool:
    """Whether importing ``cls.__module__`` on the far side defines ``cls``.

    Close to the test cloudpickle applies before pickling a class by
    reference: a class in ``__main__``, or one defined inside a function, is
    not reachable by import, so a receiver could never resolve it by name.
    Reachable means at its qualified name, or under any module-level name:
    a ``@sd.task(name="Range")`` class is bound to the decorated function's
    name (``get_range``), not to ``Range``, and importing its module still
    defines and registers it, which is all rehydration needs.
    """
    cached = _importable_cache.get(cls)
    if cached is None:
        cached = _importable_cache[cls] = _reachable_by_import(cls)
    return cached


def _reachable_by_import(cls: type) -> bool:
    module_name = cls.__module__
    if module_is_main(module_name) or "<locals>" in cls.__qualname__:
        return False
    module = sys.modules.get(module_name)
    if module is None:
        return False
    obj: typing.Any = module
    for part in cls.__qualname__.split("."):
        obj = getattr(obj, part, None)
        if obj is None:
            break
    if obj is cls:
        return True
    return any(value is cls for value in vars(module).values())


def _body_task_classes(body: typing.Any) -> set[type]:
    """The task class of every task in ``body``, by its discriminator keys.

    What the receiver resolves through the task registry, wherever it sits
    in the body: a task nested in a container the object walk does not
    know (an arbitrary serializable type) is still named here. A
    discriminated dict that is not a task (another polymorphic family) is
    not this lookup's to resolve, and is skipped.
    """
    found: set[type] = set()
    stack = [body]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            if NAMESPACE_KEY in item and NAME_KEY in item:
                try:
                    found.add(
                        BaseTask._registry().get_class(
                            TypeId(namespace=item[NAMESPACE_KEY], name=item[NAME_KEY])
                        )
                    )
                except KeyError:
                    pass
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return found


def _runtime_state(models: typing.Iterable[BaseModel]) -> str | None:
    """A description of state the instance body does not carry, if any.

    A private attribute set away from its default, or a field excluded from
    the dump holding a non-default value: a pickle carried both, a payload
    carries neither, so a task holding one stays by value rather than
    arriving without it.
    """
    for model in models:
        cls = type(model)
        private = getattr(model, "__pydantic_private__", None) or {}
        for name, attribute in cls.__private_attributes__.items():
            if name not in private:
                continue
            try:
                if private[name] != attribute.get_default():
                    return f"{cls.__qualname__}.{name} is runtime state"
            except Exception:
                return f"{cls.__qualname__}.{name} is runtime state"
        for name, info in cls.model_fields.items():
            if not info.exclude or name not in model.__dict__:
                continue
            try:
                default = info.get_default(call_default_factory=True)
                if model.__dict__[name] != default:
                    return f"{cls.__qualname__}.{name} is excluded from the body"
            except Exception:
                return f"{cls.__qualname__}.{name} is excluded from the body"
    return None


def _models(value: typing.Any) -> list[BaseModel]:
    """Every pydantic model instance reachable from ``value`` through its
    fields, and through dataclasses and containers holding models."""
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
        elif isinstance(item, (list, tuple, set, frozenset)):
            stack.extend(item)
    return found


def _by_value_reason(task: BaseTask) -> tuple[str | None, TaskPayload | None]:
    """The payload for ``task``, or the reason it must go by value.

    Never raises: any failure to build a payload is a reason to fall back
    to the pickle, never a reason to fail the spawn.
    """
    try:
        models = _models(task)
        state = _runtime_state(models)
        if state is not None:
            return f"{state}, which its instance body does not carry", None
        body = task.instance_body()
        task_id = task.id
        # The object walk and the body walk together: a task nested in a
        # type the object walk does not enter is still named by the body.
        classes = {type(m) for m in models} | _body_task_classes(body)
        for cls in sorted(classes, key=lambda c: (c.__module__, c.__qualname__)):
            if not _is_importable_by_reference(cls):
                return (
                    f"{cls.__module__}.{cls.__qualname__} is not importable "
                    "by reference",
                    None,
                )
    except Exception as e:
        return f"its instance body could not be built ({e})", None
    try:
        # A dry run of what the receiver does, and stricter: the whole body
        # must be a fixed point of its round trip, not only the task id, so
        # a lossy non-significant field is caught here, where the pickle is
        # still a fallback, rather than silently changed on the worker.
        check_serialization_stability(task)
    except Exception as e:
        return f"its registry-data round trip is not stable ({e})", None
    return None, {
        PAYLOAD_KEY: PAYLOAD_VERSION,
        "body": body,
        "task_id": str(task_id),
        "modules": sorted({cls.__module__ for cls in classes}),
    }


def to_task_payload(task: BaseTask) -> TaskOrPayload:
    """``task`` as it should cross a Modal call: its payload, or the task
    itself (by value) when it cannot be rehydrated on the far side."""
    reason, payload = _by_value_reason(task)
    if payload is not None:
        return payload
    cls = type(task)
    if cls not in _warned_by_value:
        _warned_by_value.add(cls)
        logger.warning(
            f"Task class {cls.__module__}.{cls.__qualname__} is sent to Modal "
            f"by value (pickled) because {reason}. A pickled task does not "
            "survive a redeploy that changes its classes: a field added since "
            "it was pickled is missing on the worker. Make the task "
            "rehydratable from registry data to be protected."
        )
    return task


def _is_single_root(value: typing.Any) -> bool:
    """Whether ``value`` is one root (a task, a payload, or anything that is
    not a collection of them), as the triggers' ``tasks`` argument allows."""
    return (
        isinstance(value, (BaseTask, str, bytes))
        or is_task_payload(value)
        or not isinstance(value, collections.abc.Iterable)
    )


def to_task_payloads(
    tasks: typing.Iterable[BaseTask] | BaseTask,
) -> list[TaskOrPayload] | TaskOrPayload:
    """:func:`to_task_payload` over roots: a single root stays a single
    value, so the receiver sees what it always has; any other iterable of
    roots is sent as a list."""
    if _is_single_root(tasks):
        return to_task_payload(typing.cast(BaseTask, tasks))
    return [to_task_payload(t) for t in typing.cast(typing.Iterable[BaseTask], tasks)]


def is_task_payload(value: typing.Any) -> bool:
    """Whether ``value`` is a :data:`TaskPayload` (rather than a task)."""
    return isinstance(value, dict) and PAYLOAD_KEY in value


def from_task_payload(value: TaskOrPayload) -> BaseTask:
    """The task a receiver runs: rehydrated from a payload in compat mode.

    Anything that is not a payload — a by-value :class:`BaseTask`, from this
    stardag's fallback or an older sender — passes through unchanged.

    Raises:
        TaskRehydrationError: A module cannot be imported, the body does
            not validate, or the rehydrated id differs from the sender's.
    """
    if not is_task_payload(value):
        return typing.cast(BaseTask, value)
    payload = typing.cast(TaskPayload, value)
    version = payload[PAYLOAD_KEY]
    if version != PAYLOAD_VERSION:
        raise TaskRehydrationError(
            f"Unsupported task payload version {version!r} (this stardag "
            f"reads version {PAYLOAD_VERSION}); the sender runs a newer "
            "stardag than this deployment — redeploy the app."
        )
    # Best-effort: a module the sender's classes came from may be gone from
    # this deployment -- a field removed along with its type's module is a
    # change compat rehydration absorbs (the field is dropped). Whether
    # anything still needed is missing is rehydration's call, and its error
    # carries what failed to import.
    import_errors: list[str] = []
    for module in payload["modules"]:
        try:
            importlib.import_module(module)
        except Exception as e:
            import_errors.append(f"{module}: {type(e).__name__}: {e}")
    try:
        return task_from_registry_data(
            payload["body"], expected_task_id=payload["task_id"]
        )
    except TaskRehydrationError as e:
        if not import_errors:
            raise
        raise TaskRehydrationError(
            f"{e} (modules that failed to import: {'; '.join(import_errors)})"
        ) from e


def from_task_payloads(
    values: typing.Iterable[TaskOrPayload] | TaskOrPayload,
) -> list[BaseTask] | BaseTask:
    """:func:`from_task_payload` over roots, keeping their shape: a single
    task or payload stays single, any other iterable becomes a list."""
    if _is_single_root(values):
        return from_task_payload(typing.cast(TaskOrPayload, values))
    return [
        from_task_payload(v)
        for v in typing.cast(typing.Iterable[TaskOrPayload], values)
    ]
