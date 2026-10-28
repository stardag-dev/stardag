"""The committed task store: reviewed, registry-independent copies of the
task instances a code base pins.

For pinned, long-lived instances in code (a key dataset, a trained model),
the recommended handle is a body stored in the project, loaded eagerly::

    MODEL = TrainedModel.from_store("6f1c...", instance_id="0193...")

The store is a directory in the repository (``[project] task_store`` in
``.stardag/config.toml``), holding one normalized, content-addressed file
per distinct instance body (format: :mod:`stardag._core.task_store_format`).
Two writers fill it:

- :func:`write_to_store` (``sd.write_to_store``): from task objects in hand,
  no registry needed, so a spec not built yet can be pinned too;
- :func:`write_from_registry` (``stardag tasks ref <id> --write``): reads an
  instance body from the registry, checks it rehydrates to its task id, and
  writes its tree.

:func:`load_from_store` (``MyTask.from_store``, ``sd.task_from_store``)
reads one back: it re-inlines every pointer, imports each stored class's
module hint, validates in compat mode and checks the task id. No registry,
profile or network is involved, which is what makes a test suite reading
the store hermetic.

The registry stays the record of what ran; the store is a reviewed copy.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import tempfile
import threading
import typing
from pathlib import Path
from typing import Any
from uuid import UUID

from stardag._core.base_task import BaseTask
from stardag._core.task_store_format import (
    Envelope,
    TaskStoreError,
    denormalize,
    label,
)

_WRITE_HINT = (
    "Write it with 'stardag tasks ref {task_id} --write' (from the registry) "
    "or sd.write_to_store(task) (from a task object)."
)


class TaskStoreNotConfiguredError(TaskStoreError):
    """No task store directory was given or configured."""


class AmbiguousStoredTaskError(TaskStoreError):
    """The store holds several bodies of one task and none was picked."""


# -----------------------------------------------------------------------------
# Locating the store
# -----------------------------------------------------------------------------


def configured_store() -> Path | None:
    """The configured store directory (``[project] task_store`` or
    ``STARDAG_TASK_STORE``), or ``None``."""
    from stardag.config.project import get_project_config

    return get_project_config().task_store


def resolve_store(store: str | os.PathLike[str] | None) -> TaskStore:
    """The store at ``store`` (relative to the working directory), else the
    configured one.

    Raises:
        TaskStoreNotConfiguredError: Neither is set.
    """
    if store is not None:
        return TaskStore(Path(store).expanduser().absolute())
    configured = configured_store()
    if configured is None:
        raise TaskStoreNotConfiguredError(
            "No task store configured. Pass store=<directory>, set task_store "
            "under [project] in .stardag/config.toml (relative to the project "
            "root), or set STARDAG_TASK_STORE."
        )
    return TaskStore(configured)


class TaskStore:
    """A task store directory (see the module docstring)."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def __repr__(self) -> str:
        return f"TaskStore({str(self.root)!r})"

    def path_for(self, envelope: Envelope) -> Path:
        return (
            self.root
            / label(envelope.namespace, envelope.name)
            / envelope.task_id
            / f"{envelope.instance_hash}.json"
        )

    def index(self) -> dict[tuple[str, str], Path]:
        """Every stored body by ``(task_id, instance_hash)``, from the file
        layout alone (nothing is read). Files and directories that are not
        part of the layout (a README) are ignored."""
        found: dict[tuple[str, str], Path] = {}
        if not self.root.is_dir():
            return found
        for class_dir in self.root.iterdir():
            if not class_dir.is_dir() or class_dir.name.startswith("."):
                continue
            for task_dir in class_dir.iterdir():
                task_id = _uuid_or_none(task_dir.name)
                if task_id is None or not task_dir.is_dir():
                    continue
                for path in task_dir.glob("*.json"):
                    instance_hash = _uuid_or_none(path.stem)
                    if instance_hash is not None:
                        found[(task_id, instance_hash)] = path
        return found

    def read(self, path: Path) -> Envelope:
        """The envelope in ``path``, checked against its address."""
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as e:
            raise TaskStoreError(f"Cannot read task store file {path}: {e}") from e
        envelope = Envelope.from_json(text, str(path))
        if envelope.instance_hash != path.stem or envelope.task_id != path.parent.name:
            raise TaskStoreError(
                f"{path} holds task {envelope.task_id}, instance hash "
                f"{envelope.instance_hash}, which is not the address its path "
                "names. Store files must not be renamed or moved by hand."
            )
        return envelope

    def write(self, envelope: Envelope, source: dict[str, Any]) -> Path | None:
        """Store ``envelope`` with ``source`` added; the path written, or
        ``None`` when the file already says all of it (a no-op).

        Raises:
            TaskStoreError: The path holds a different body. Content
                addressing makes that a bug (or a hand edit), never a
                legitimate update.
        """
        path = self.path_for(envelope)
        with _address_lock(path):
            return self._write_locked(path, envelope, source)

    def _write_locked(
        self, path: Path, envelope: Envelope, source: dict[str, Any]
    ) -> Path | None:
        if path.exists():
            existing = self.read(path)
            if existing.to_json_body() != envelope.to_json_body():
                raise TaskStoreError(
                    f"{path} already holds a different body under the same "
                    "instance hash. Store files are content-addressed, so this "
                    "means the file was edited by hand; restore it from version "
                    "control, or delete it and write it again."
                )
            updated = existing.with_source(source)
            if updated is None and existing.module == envelope.module:
                return None
            updated = updated or existing
            updated.module = envelope.module
        else:
            envelope.sources = [source]
            updated = envelope
        _atomic_write(path, updated.to_json())
        return path

    def select(
        self,
        task_id: str,
        *,
        instance_id: str | None = None,
        instance_hash: str | None = None,
    ) -> Envelope | None:
        """The stored body of ``task_id`` matching the given instance id or
        hash, or the only one stored; ``None`` when none matches.

        Raises:
            AmbiguousStoredTaskError: Several bodies are stored for the task
                and neither an instance id nor a hash picks one.
        """
        index = self.index()
        candidates = sorted(p for (tid, _), p in index.items() if tid == task_id)
        if instance_hash is not None:
            candidates = [p for p in candidates if p.stem == instance_hash]
        envelopes = [self.read(p) for p in candidates]
        if instance_id is not None:
            envelopes = [e for e in envelopes if instance_id in e.instance_ids]
        if len(envelopes) > 1:
            listed = "; ".join(
                f"instance_hash={e.instance_hash!r}"
                + (
                    f" (instance ids {', '.join(e.instance_ids)})"
                    if e.instance_ids
                    else ""
                )
                for e in envelopes
            )
            raise AmbiguousStoredTaskError(
                f"The task store at {self.root} holds {len(envelopes)} bodies of "
                f"task {task_id}: {listed}. Pass instance_id= or instance_hash= "
                "to pick one."
            )
        return envelopes[0] if envelopes else None


@contextlib.contextmanager
def _address_lock(path: Path) -> typing.Iterator[None]:
    """Serialize the read-merge-replace of one store file, across threads
    and processes (an advisory ``flock`` on a sibling lock file where the
    platform has one): two writers adding different sources to one body
    must not overwrite each other's addition. The lock file lives in the
    temporary directory, keyed by the file's path, so nothing but store
    files ever appears in the (committed) store."""
    digest = hashlib.sha256(str(path.absolute()).encode("utf-8")).hexdigest()[:32]
    lock_dir = Path(tempfile.gettempdir()) / "stardag-task-store-locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"{digest}.lock"
    with _THREAD_LOCK, open(lock_path, "a") as handle:
        try:
            import fcntl
        except ImportError:  # pragma: no cover (Windows: threads only)
            yield
            return
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


_THREAD_LOCK = threading.RLock()


def _uuid_or_none(text: str) -> str | None:
    try:
        return str(UUID(text))
    except ValueError:
        return None


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# -----------------------------------------------------------------------------
# Reading
# -----------------------------------------------------------------------------


def load_from_store(
    cls: type[BaseTask],
    task_id: UUID | str,
    *,
    instance_id: UUID | str | None = None,
    instance_hash: UUID | str | None = None,
    store: str | os.PathLike[str] | TaskStore | None = None,
) -> BaseTask:
    """``cls.from_store(...)``: see there."""
    task_store = store if isinstance(store, TaskStore) else resolve_store(store)
    tid = _uuid_str(task_id, "task_id")
    envelope = task_store.select(
        tid,
        instance_id=None
        if instance_id is None
        else _uuid_str(instance_id, "instance_id"),
        instance_hash=(
            None if instance_hash is None else _uuid_str(instance_hash, "instance_hash")
        ),
    )
    if envelope is None:
        which = (
            f" with instance id {instance_id}"
            if instance_id is not None
            else f" with instance hash {instance_hash}"
            if instance_hash is not None
            else ""
        )
        raise TaskStoreError(
            f"The task store at {task_store.root} holds no body of task {tid}"
            f"{which}. " + _WRITE_HINT.format(task_id=tid)
        )
    return load_envelope(cls, envelope, task_store)


def load_envelope(
    cls: type[BaseTask], envelope: Envelope, task_store: TaskStore
) -> BaseTask:
    """Re-inline, import the hints, validate, check class and task id."""
    from stardag._core import task_imports
    from stardag._core.rehydrate import TaskRehydrationError, _validate_for

    index = task_store.index()
    seen: dict[tuple[str, str], Envelope] = {
        (envelope.task_id, envelope.instance_hash): envelope
    }

    def load(task_id: str, instance_hash: str, referenced_from: str) -> Envelope:
        key = (task_id, instance_hash)
        if key in seen:
            return seen[key]
        path = index.get(key)
        if path is None:
            raise TaskStoreError(
                f"The stored body of task {referenced_from} points to task "
                f"{task_id} (instance hash {instance_hash}), which is not in "
                f"the task store at {task_store.root}. The store is incomplete: "
                "write the whole tree again. "
                + _WRITE_HINT.format(task_id=referenced_from)
            )
        seen[key] = task_store.read(path)
        return seen[key]

    body = denormalize(envelope, load)
    task_imports.import_module_hints(
        (e.namespace, e.name, e.module) for e in seen.values()
    )
    try:
        task = _validate_for(cls, body, allow_aliased=False)
        rehydrated_id = str(task.id)
    except TaskRehydrationError as e:
        raise type(e)(
            f"Cannot load task {envelope.task_id} from the task store: {e}"
        ) from e
    except Exception as e:
        raise TaskRehydrationError(
            f"Cannot load task {envelope.task_id} from the task store: {e}"
        ) from e
    if rehydrated_id != envelope.task_id:
        from stardag._core.rehydrate import _id_mismatch_message

        raise TaskRehydrationError(
            "Cannot load from the task store: "
            + _id_mismatch_message(task, rehydrated_id, envelope.task_id)
        )
    return task


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


def find_in_store(
    cls: type[BaseTask], task_id: UUID, instance_id: UUID | None
) -> BaseTask | None:
    """The task from the configured store when it holds the body a reference
    points to: for a pinned reference, a body recorded under that instance
    id; for an unpinned one, the only body of the task. ``None`` when no
    store is configured or it has no such body (read the registry then)."""
    root = configured_store()
    if root is None:
        return None
    task_store = TaskStore(root)
    try:
        envelope = task_store.select(
            str(task_id),
            instance_id=None if instance_id is None else str(instance_id),
        )
    except AmbiguousStoredTaskError:
        return None  # several bodies stored: not "exactly one"
    if envelope is None:
        return None
    return load_envelope(cls, envelope, task_store)


_STORE_MEMO_LOCK = threading.Lock()
_STORE_MEMO: dict[tuple[Path, Any, UUID, UUID | None], BaseTask] = {}


def hydrate_from_store(ref: Any) -> BaseTask | None:
    """A ``TaskRef``'s task from the configured store (see
    :func:`find_in_store`), checked like a registry read; ``None`` when the
    store does not serve it. Memoized per process, like hydration.

    Raises:
        TaskRefHydrationError: The store holds the body but it cannot be
            loaded, or is of another class or task id.
    """
    from stardag._core.rehydrate import TaskRehydrationError
    from stardag._core.task_ref import TaskRefHydrationError

    root = configured_store()
    if root is None:
        return None
    key = (root, ref.task_class, ref.task_id, ref.instance_id)
    with _STORE_MEMO_LOCK:
        cached = _STORE_MEMO.get(key)
    if cached is not None:
        return cached
    try:
        task = find_in_store(ref.task_class, ref.task_id, ref.instance_id)
    except (TaskStoreError, TaskRehydrationError) as e:
        raise TaskRefHydrationError(
            f"Cannot hydrate {ref!r} from the task store at {root}: {e}"
        ) from e
    if task is None:
        return None
    if not isinstance(task, ref.task_class) or task.id != ref.task_id:
        raise TaskRefHydrationError(
            f"Cannot hydrate {ref!r}: the task store's body is a "
            f"{type(task).__qualname__} with task id {task.id}."
        )
    with _STORE_MEMO_LOCK:
        return _STORE_MEMO.setdefault(key, task)


def clear_store_memo() -> None:
    with _STORE_MEMO_LOCK:
        _STORE_MEMO.clear()


def _uuid_str(value: UUID | str, what: str) -> str:
    try:
        return str(UUID(str(value)))
    except ValueError as e:
        raise ValueError(f"{what} must be a UUID, got {value!r}") from e
