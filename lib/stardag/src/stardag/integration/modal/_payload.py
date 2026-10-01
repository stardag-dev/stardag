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
not importable by reference (``__main__``, a local class), a nested task
field with a plain annotation — is sent **by value**, as before, with a
warning once per class: it is not protected across deploys. A reactive
build never has one (its pre-flight refuses any task a tick could not
rehydrate), so the fallback is only ever taken by resident and hybrid
builds.

The payload is a plain ``dict``, so there is no stardag class to unpickle
on the far side and its shape can grow. Receivers keep accepting a pickled
:class:`BaseTask` too: an older deployment's tick still sends one.
"""

from __future__ import annotations

import importlib
import logging
import sys
import typing

from pydantic import BaseModel

from stardag._core.base_task import BaseTask
from stardag._core.instance import check_serialization_stability
from stardag._core.rehydrate import TaskRehydrationError, task_from_registry_data
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


def _is_importable_by_reference(cls: type) -> bool:
    """Whether importing ``cls.__module__`` on the far side defines ``cls``.

    The same test cloudpickle applies before pickling a class by reference:
    a class in ``__main__``, or one defined inside a function, is not
    reachable by import, so a receiver could never resolve it by name.
    """
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
            return False
    return obj is cls


def _model_classes(value: typing.Any) -> set[type]:
    """Every pydantic model class reachable from ``value`` through its fields."""
    found: set[type] = set()
    seen: set[int] = set()
    stack = [value]
    while stack:
        item = stack.pop()
        if id(item) in seen:
            continue
        seen.add(id(item))
        if isinstance(item, BaseModel):
            found.add(type(item))
            # Declared fields only: ``__dict__`` may also hold cached values
            # that are not part of the body.
            stack.extend(
                item.__dict__[name]
                for name in type(item).model_fields
                if name in item.__dict__
            )
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
        classes = _model_classes(task)
        for cls in sorted(classes, key=lambda c: (c.__module__, c.__qualname__)):
            if not _is_importable_by_reference(cls):
                return (
                    f"{cls.__module__}.{cls.__qualname__} is not importable "
                    "by reference",
                    None,
                )
        body = task.instance_body()
        task_id = task.id
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


def to_task_payloads(
    tasks: typing.Sequence[BaseTask] | BaseTask,
) -> list[TaskOrPayload] | TaskOrPayload:
    """:func:`to_task_payload` over roots, keeping their shape: a single
    root stays a single value, so the receiver sees what it always has."""
    if isinstance(tasks, (list, tuple)):
        return [to_task_payload(t) for t in tasks]
    return to_task_payload(typing.cast(BaseTask, tasks))


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
    for module in payload["modules"]:
        try:
            importlib.import_module(module)
        except Exception as e:
            raise TaskRehydrationError(
                f"Cannot import {module!r}, which defines a class in task "
                f"{payload['task_id']}: {e}"
            ) from e
    return task_from_registry_data(payload["body"], expected_task_id=payload["task_id"])


def from_task_payloads(
    values: typing.Sequence[TaskOrPayload] | TaskOrPayload,
) -> typing.Sequence[BaseTask] | BaseTask:
    """:func:`from_task_payload` over roots, keeping their shape."""
    if isinstance(values, (list, tuple)):
        return [from_task_payload(v) for v in values]
    return from_task_payload(typing.cast(TaskOrPayload, values))
