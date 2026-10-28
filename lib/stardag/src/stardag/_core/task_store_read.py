"""Loading tasks from the committed task store: ``MyTask.from_store``,
``sd.task_from_store``, and references served from the store.

Which store applies is decided per call (:mod:`stardag._core.anchor`,
:func:`stardag.config.project.resolve_project_config`): an explicit
``store=``, then ``STARDAG_TASK_STORE``, then the package settings of the
calling module's file, then of the task class's defining module, then of
the working directory. A reference remembers the file of the code that
created it, so hydrating it at build time reads that package's store.
"""

from __future__ import annotations

import logging
import os
import threading
import typing
from pathlib import Path
from typing import Any
from uuid import UUID

from stardag._core.base_task import BaseTask
from stardag._core.task_store import (
    WRITE_HINT,
    AmbiguousStoredTaskError,
    TaskStore,
    project_config,
    resolve_store,
    uuid_str,
)
from stardag._core.task_store_format import (
    Envelope,
    TaskStoreError,
    denormalize,
    is_pointer,
    pointer_address,
)

logger = logging.getLogger(__name__)

_DRIFT_CAUSES = (
    "Likely cause: a significant field of that class was added without "
    "StardagField(compat_default=...), or removed or renamed."
)


def load_from_store(
    cls: type[BaseTask],
    task_id: UUID | str,
    *,
    instance_id: UUID | str | None = None,
    instance_hash: UUID | str | None = None,
    store: str | os.PathLike[str] | TaskStore | None = None,
    anchors: typing.Sequence[Path | None] | None = None,
) -> BaseTask:
    """``cls.from_store(...)``: see there. ``anchors`` default to the
    caller's file and ``cls``'s module."""
    from stardag._core.anchor import anchored, call_anchors

    anchors = tuple(anchors) if anchors is not None else call_anchors(cls)
    task_store = (
        store if isinstance(store, TaskStore) else resolve_store(store, anchors)
    )
    tid = uuid_str(task_id, "task_id")
    envelope = task_store.select(
        tid,
        instance_id=None
        if instance_id is None
        else uuid_str(instance_id, "instance_id"),
        instance_hash=(
            None if instance_hash is None else uuid_str(instance_hash, "instance_hash")
        ),
    )
    if envelope is None:
        which = " and".join(
            part
            for part in (
                f" with instance id {instance_id}" if instance_id is not None else "",
                f" with instance hash {instance_hash}"
                if instance_hash is not None
                else "",
            )
            if part
        )
        raise TaskStoreError(
            f"The task store at {task_store.root} holds no body of task {tid}"
            f"{which}. " + WRITE_HINT.format(task_id=tid)
        )
    with anchored(*anchors):
        return load_envelope(cls, envelope, task_store)


def load_envelope(
    cls: type[BaseTask], envelope: Envelope, task_store: TaskStore
) -> BaseTask:
    """Re-inline, import the hints, validate, check class and task id; on a
    task id mismatch, name the deepest nested task whose id moved."""
    from stardag._core import task_imports
    from stardag._core.rehydrate import (
        TaskClassNotRegisteredError,
        TaskRehydrationError,
        _validate_for,
    )

    seen: dict[tuple[str, str], Envelope] = {
        (envelope.task_id, envelope.instance_hash): envelope
    }

    def load(task_id: str, instance_hash: str, referenced_from: str) -> Envelope:
        key = (task_id, instance_hash)
        if key in seen:
            return seen[key]
        path = task_store.lookup(task_id, instance_hash)
        if path is None:
            raise TaskStoreError(
                f"The stored body of task {referenced_from} points to task "
                f"{task_id} (instance hash {instance_hash}), which is not in "
                f"the task store at {task_store.root}. The store is incomplete: "
                "write the whole tree again. "
                + WRITE_HINT.format(task_id=referenced_from)
            )
        seen[key] = task_store.read(path)
        return seen[key]

    bodies: dict[tuple[str, str], dict[str, Any]] = {}
    body = denormalize(envelope, load, bodies)
    task_imports.import_module_hints(
        (e.namespace, e.name, e.module) for e in seen.values()
    )
    prefix = f"Cannot load task {envelope.task_id} from the task store"
    try:
        task = _validate_for(cls, body, allow_aliased=False)
        rehydrated_id = str(task.id)
    except TaskClassNotRegisteredError as e:
        raise type(e)(f"{prefix}: {e}") from e
    except TaskRehydrationError as e:
        raise type(e)(f"{prefix}: {e}{_drift_note(envelope, seen, bodies)}") from e
    except Exception as e:
        raise TaskRehydrationError(
            f"{prefix}: {e}{_drift_note(envelope, seen, bodies)}"
        ) from e
    if rehydrated_id != envelope.task_id:
        from stardag._core.rehydrate import _id_mismatch_message

        raise TaskRehydrationError(
            f"{prefix}: "
            + _id_mismatch_message(task, rehydrated_id, envelope.task_id)
            + _drift_note(envelope, seen, bodies)
        )
    return task


# -----------------------------------------------------------------------------
# Nested drift
# -----------------------------------------------------------------------------


def _pointer_paths(value: Any, path: str) -> typing.Iterator[tuple[str, Any]]:
    if is_pointer(value):
        yield path, value
    elif isinstance(value, dict):
        for k in sorted(value):
            yield from _pointer_paths(value[k], f"{path}.{k}" if path else k)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from _pointer_paths(v, f"{path}[{i}]")


def _drift_note(
    root: Envelope,
    seen: dict[tuple[str, str], Envelope],
    bodies: dict[tuple[str, str], dict[str, Any]],
) -> str:
    try:
        return _drift_report(root, seen, bodies)
    except Exception:  # a diagnostic must never mask the error it explains
        logger.debug("Nested drift report failed", exc_info=True)
        return ""


def _drift_report(
    root: Envelope,
    seen: dict[tuple[str, str], Envelope],
    bodies: dict[tuple[str, str], dict[str, Any]],
) -> str:
    """Which nested task's id moved, the deepest first: each stored pointer
    records the nested task's id, so recompute every nested body's id and
    keep the moved ones none of whose own nested tasks moved."""
    from stardag._core.rehydrate import task_from_body

    root_key = (root.task_id, root.instance_hash)
    paths: dict[tuple[str, str], str] = {root_key: ""}
    children: dict[tuple[str, str], list[tuple[str, str]]] = {}
    order = [root_key]
    for key in order:
        env = seen.get(key)
        kids: list[tuple[str, str]] = []
        for path, value in _pointer_paths(env.body if env else {}, ""):
            child = pointer_address(value)
            kids.append(child)
            if child not in paths:
                parent = paths[key]
                paths[child] = f"{parent}.{path}" if parent else path
                order.append(child)
        children[key] = kids
    moved: dict[tuple[str, str], str] = {}
    for key in order[1:]:
        body = bodies.get(key)
        if body is None:
            continue
        try:
            now = str(task_from_body(body).id)
        except Exception as e:
            now = f"nothing (it no longer validates: {type(e).__name__})"
        if now != key[0]:
            moved[key] = now

    def has_moved_below(key: tuple[str, str], visiting: set) -> bool:
        for child in children.get(key, ()):
            if child in visiting:
                continue
            if child in moved or has_moved_below(child, visiting | {child}):
                return True
        return False

    deepest = [k for k in moved if not has_moved_below(k, {k})]
    if not deepest:
        if not moved:
            return (
                f" No nested task's id moved: the change is in "
                f"{seen[root_key].name}'s own significant fields. {_DRIFT_CAUSES}"
            )
        return ""
    listed = "; ".join(
        f"{paths[k]} ({_label(seen[k])}, stored as task {k[0]}) now recomputes "
        f"to {moved[k]}"
        for k in sorted(deepest, key=lambda k: paths[k])[:3]
    )
    return f" The deepest nested task whose id moved: {listed}. {_DRIFT_CAUSES}"


def _label(envelope: Envelope) -> str:
    return (
        f"{envelope.namespace}.{envelope.name}" if envelope.namespace else envelope.name
    )


# -----------------------------------------------------------------------------
# The function form
# -----------------------------------------------------------------------------

_TaskT = typing.TypeVar("_TaskT", bound=BaseTask)


def task_from_store(
    task_class: type[_TaskT],
    task_id: UUID | str,
    instance_id: UUID | str | None = None,
    *,
    instance_hash: UUID | str | None = None,
    store: str | os.PathLike[str] | None = None,
) -> _TaskT:
    """``task_class.from_store(...)``, as a function: for a class on which
    ``from_store`` is not the method (one declaring a field of that name
    shadows it). Never reads ``task_class.from_store``. See
    :meth:`BaseTask.from_store <stardag.BaseTask.from_store>`.

    Raises:
        TypeError: ``task_class`` is not a task class.
    """
    if not (isinstance(task_class, type) and issubclass(task_class, BaseTask)):
        raise TypeError(f"task_from_store() needs a task class, got {task_class!r}")
    return typing.cast(
        _TaskT,
        load_from_store(
            task_class,
            task_id,
            instance_id=instance_id,
            instance_hash=instance_hash,
            store=store,
        ),
    )


# -----------------------------------------------------------------------------
# References
# -----------------------------------------------------------------------------

#: Kept in a reference's ``__dict__``: the file of the code that created
#: it. Outside every field, so not dumped, hashed, compared or pickled.
REF_ANCHOR_KEY = "_stardag_anchor_file"

_STORE_MEMO_LOCK = threading.Lock()
_STORE_MEMO: dict[tuple[Path, Any, UUID, UUID | None], BaseTask] = {}


def ref_anchors(ref: Any) -> tuple[Path | None, ...]:
    from stardag._core.anchor import module_file

    return (vars(ref).get(REF_ANCHOR_KEY), module_file(ref.task_class))


def ref_store(ref: Any) -> Path | None:
    """The store that applies to ``ref``, or ``None``."""
    return project_config(ref_anchors(ref)).task_store


def hydrate_from_store(ref: Any) -> BaseTask | None:
    """A ``TaskRef``'s task from the store that applies to it, when that
    store holds the body: for a pinned reference, a body recorded under its
    instance id (``sources``, unverified metadata); for an unpinned one,
    the only body of the task, or among several the one written as the
    instance of the execution that completed it. ``None`` otherwise.
    Memoized per process.

    Raises:
        TaskRefHydrationError: The store holds the body but it cannot be
            loaded, or is of another class or task id.
    """
    from stardag._core.anchor import anchored
    from stardag._core.rehydrate import TaskRehydrationError
    from stardag._core.task_ref import TaskRefHydrationError

    anchors = ref_anchors(ref)
    try:
        root = project_config(anchors).task_store
    except TaskStoreError as e:
        raise TaskRefHydrationError(f"Cannot hydrate {ref!r}: {e}") from e
    if root is None:
        return None
    key = (root, ref.task_class, ref.task_id, ref.instance_id)
    with _STORE_MEMO_LOCK:
        cached = _STORE_MEMO.get(key)
    if cached is not None:
        return cached
    task_store = TaskStore(root)
    try:
        try:
            envelope = task_store.select(
                str(ref.task_id),
                instance_id=None if ref.instance_id is None else str(ref.instance_id),
            )
        except AmbiguousStoredTaskError:
            # Several bodies stored: the one written as the instance of the
            # execution that completed the task, if exactly one says so.
            completed = [
                e
                for e in (
                    task_store.read(p) for p in task_store.paths_of(str(ref.task_id))
                )
                if e.from_completed_execution
            ]
            if len(completed) != 1:
                return None
            envelope = completed[0]
        if envelope is None:
            return None
        with anchored(*anchors):
            task = load_envelope(ref.task_class, envelope, task_store)
    except (TaskStoreError, TaskRehydrationError) as e:
        raise TaskRefHydrationError(
            f"Cannot hydrate {ref!r} from the task store at {root}: {e}"
        ) from e
    if not isinstance(task, ref.task_class) or task.id != ref.task_id:
        raise TaskRefHydrationError(
            f"Cannot hydrate {ref!r}: the task store's body is a "
            f"{type(task).__qualname__} with task id {task.id}."
        )
    logger.debug("Hydrated %r from the task store at %s", ref, root)
    with _STORE_MEMO_LOCK:
        return _STORE_MEMO.setdefault(key, task)


def store_note(ref: Any) -> str:
    """For a hydration error: which store was checked, or that none
    applies."""
    try:
        root = ref_store(ref)
    except TaskStoreError as e:
        return f" The task store could not be resolved ({e})."
    if root is None:
        return (
            " No task store is configured for it ([tool.stardag] task_store in "
            "the package's pyproject.toml)."
        )
    return f" The task store at {root} holds no body of it."


def clear_store_memo() -> None:
    from stardag._core.task_store import clear_index_cache

    with _STORE_MEMO_LOCK:
        _STORE_MEMO.clear()
    clear_index_cache()
