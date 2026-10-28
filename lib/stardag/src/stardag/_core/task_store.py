"""The committed task store: reviewed, registry-independent copies of the
task instances a code base pins.

For pinned, long-lived instances in code (a key dataset, a trained model),
the recommended handle is a body stored with the package, loaded eagerly::

    MODEL = TrainedModel.from_store("6f1c...", instance_hash="7a2e...")

The store is a directory in the package (``[tool.stardag] task_store`` in
its ``pyproject.toml``, see :mod:`stardag.config.project`), holding one
normalized, content-addressed file per distinct instance body (format:
:mod:`stardag._core.task_store_format`). This module locates a store and
reads and writes its files; :mod:`stardag._core.task_store_read` loads
tasks from it and :mod:`stardag._core.task_store_write` fills it.

Trust model: a store is code. Reading one imports the module each file
names as its class's import hint, so point ``store=`` (or the config) only
at a store you would run the code of: a reviewed one in your repository or
an installed package. Only a file's ``body`` is verified (against its
content address); ``task_id``, ``namespace``, ``name``, ``module`` and
``sources`` are unverified metadata.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import secrets
import tempfile
import threading
import typing
from pathlib import Path
from typing import Any
from uuid import UUID

from stardag._core.task_store_format import Envelope, TaskStoreError, label

logger = logging.getLogger(__name__)

WRITE_HINT = (
    "Write it with 'stardag tasks ref {task_id} --write' (from the registry) "
    "or sd.write_to_store(task) (from a task object)."
)
_MARKER_TEXT = (
    "# A stardag task store: one file per stored instance body. Written by\n"
    "# stardag; files are content-addressed and must not be edited by hand.\n"
    "# This file marks the directory, so an installed package's store is\n"
    "# found without its pyproject.toml. Ship it with the package data.\n"
    "format = 1\n"
)


class TaskStoreNotConfiguredError(TaskStoreError):
    """No task store directory was given or configured."""


class AmbiguousStoredTaskError(TaskStoreError):
    """The store holds several bodies of one task and none was picked."""


# -----------------------------------------------------------------------------
# Locating the store
# -----------------------------------------------------------------------------


def project_config(anchors: typing.Iterable[Path | None] = ()) -> Any:
    from stardag.config.project import ProjectConfigError, resolve_project_config

    try:
        return resolve_project_config(anchors)
    except ProjectConfigError as e:
        raise TaskStoreError(str(e)) from e


def configured_store(anchors: typing.Iterable[Path | None] = ()) -> Path | None:
    """The store configured for a call anchored at ``anchors`` (see
    :func:`stardag.config.project.resolve_project_config`), or ``None``."""
    return project_config(anchors).task_store


def resolve_store(
    store: str | os.PathLike[str] | None,
    anchors: typing.Iterable[Path | None] = (),
) -> TaskStore:
    """The store at ``store``, else the configured one.

    An explicit relative ``store`` resolves against the root of the
    package settings that apply (the ``pyproject.toml``'s directory), as a
    configured ``task_store`` does, and against the working directory when
    none apply. ``pkg:<package>/<path>`` names a directory inside a package.

    Raises:
        TaskStoreNotConfiguredError: Neither is set.
    """
    from stardag.config.project import ProjectConfigError, resolve_store_spec

    if store is not None:
        # An explicit store bypasses automatic selection: discovery failing
        # (an installed package with several stores) only loses the root
        # a relative path would resolve against.
        try:
            config = project_config(anchors)
        except TaskStoreError:
            config = None
        root = config.root if config is not None else None
        modules = config.task_modules if config is not None and root else None
        try:
            return TaskStore(resolve_store_spec(store, root), task_modules=modules)
        except ProjectConfigError as e:
            raise TaskStoreError(str(e)) from e
    config = project_config(anchors)
    if config.task_store is None:
        raise TaskStoreNotConfiguredError(
            "No task store configured. Pass store=<directory>, set task_store "
            "under [tool.stardag] in the package's pyproject.toml (relative to "
            "that file, or 'pkg:<package>/<dir>'), or set STARDAG_TASK_STORE."
        )
    return TaskStore(config.task_store, task_modules=config.task_modules)


# -----------------------------------------------------------------------------
# The store
# -----------------------------------------------------------------------------

_INDEX_LOCK = threading.Lock()
_INDEXES: dict[Path, dict[tuple[str, str], Path]] = {}


def clear_index_cache() -> None:
    with _INDEX_LOCK:
        _INDEXES.clear()


class TaskStore:
    """A task store directory (see the module docstring)."""

    def __init__(
        self, root: Path, *, task_modules: tuple[str, ...] | None = None
    ) -> None:
        self.root = root
        #: The package's ``task_modules``, recorded in the store's marker so
        #: an installed copy (no ``pyproject.toml``) still has them. ``None``
        #: when this store was not reached through package settings.
        self.task_modules = task_modules

    def __repr__(self) -> str:
        return f"TaskStore({str(self.root)!r})"

    def path_for(self, envelope: Envelope) -> Path:
        return (
            self.root
            / label(envelope.namespace, envelope.name)
            / envelope.task_id
            / f"{envelope.instance_hash}.json"
        )

    def index(self, *, fresh: bool = False) -> dict[tuple[str, str], Path]:
        """Every stored body by ``(task_id, instance_hash)``, from the file
        layout alone (nothing is read). Files and directories outside the
        layout (a README, the marker) are ignored.

        Memoized per process and store; writes in this process keep it
        current, and a lookup that misses re-walks once (``fresh=True``),
        so files added on disk meanwhile are found."""
        key = self.root
        if not fresh:
            with _INDEX_LOCK:
                cached = _INDEXES.get(key)
            if cached is not None:
                return cached
        found: dict[tuple[str, str], Path] = {}
        if self.root.is_dir():
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
        with _INDEX_LOCK:
            _INDEXES[key] = found
        return found

    def lookup(self, task_id: str, instance_hash: str) -> Path | None:
        path = self.index().get((task_id, instance_hash))
        if path is None or not path.exists():
            path = self.index(fresh=True).get((task_id, instance_hash))
        return path

    def paths_of(self, task_id: str) -> list[Path]:
        """Every stored body of ``task_id``. The task's directory (one per
        task id) is located from the index and listed afresh, so a body
        added on disk since the index was cached (a pull, another process)
        is seen."""
        dirs = {p.parent for (tid, _), p in self.index().items() if tid == task_id}
        if not dirs or not all(d.is_dir() for d in dirs):
            dirs = {
                p.parent
                for (tid, _), p in self.index(fresh=True).items()
                if tid == task_id
            }
        return sorted(
            path
            for d in dirs
            for path in d.glob("*.json")
            if _uuid_or_none(path.stem) is not None
        )

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
        with _store_lock(self.root):
            written = self._write_locked(path, envelope, source)
            self._write_marker()
        if written is not None:
            with _INDEX_LOCK:
                cached = _INDEXES.get(self.root)
                if cached is not None:
                    cached[(envelope.task_id, envelope.instance_hash)] = path
        return written

    def _write_marker(self) -> None:
        import tomllib

        marker = self.root / _marker_name()
        existing: tuple[str, ...] | None = None
        if marker.exists():
            try:
                existing = tuple(
                    tomllib.loads(marker.read_text("utf-8")).get("task_modules", ())
                )
            except (OSError, tomllib.TOMLDecodeError):
                existing = None
            if self.task_modules is None or existing == self.task_modules:
                return
        modules = self.task_modules or ()
        text = _MARKER_TEXT
        if modules:
            listed = ", ".join(json.dumps(m) for m in modules)
            text += (
                "# The package's [tool.stardag] task_modules, for an installed copy.\n"
                f"task_modules = [{listed}]\n"
            )
        _atomic_write(marker, text)

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
            # A known module hint is never replaced by none (a rewrite from
            # a class defined in __main__).
            module = envelope.module or existing.module
            if updated is None and existing.module == module:
                return None
            updated = updated or existing
            updated.module = module
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
        """The stored body of ``task_id`` with the given instance hash and/or
        recorded as the given instance id, or the only one stored; ``None``
        when none matches.

        The instance hash is verified (it is the file's content address);
        the instance id is matched against ``sources``, unverified
        metadata. Given both, the body must satisfy both.

        Raises:
            AmbiguousStoredTaskError: Several bodies are stored for the task
                and neither an instance id nor a hash picks one.
        """
        candidates = self.paths_of(task_id)
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


def _marker_name() -> str:
    from stardag.config.project import STORE_MARKER

    return STORE_MARKER


# -----------------------------------------------------------------------------
# File I/O
# -----------------------------------------------------------------------------

_THREAD_LOCK = threading.RLock()


@contextlib.contextmanager
def _store_lock(root: Path) -> typing.Iterator[None]:
    """Serialize writes to one store, across threads and processes (an
    advisory ``flock`` where the platform has one): two writers adding
    different sources to one body must not overwrite each other's addition.

    One lock file per store (keyed by its resolved path), under the
    temporary directory, so nothing but store files ever appears in the
    committed store and lock files do not accumulate per stored body."""
    key = str(root.resolve())
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
    lock_dir = Path(tempfile.gettempdir()) / "stardag-task-store-locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    with _THREAD_LOCK, open(lock_dir / f"{digest}.lock", "a") as handle:
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


def _uuid_or_none(text: str) -> str | None:
    try:
        return str(UUID(text))
    except ValueError:
        return None


def _atomic_write(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically, with the mode an ordinary file
    gets (``0o666 & ~umask``), not ``mkstemp``'s ``0o600``: a store copied
    into a container image must be readable by its user."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # Created with mode 0o666 so the kernel applies the umask in effect now
    # (mkstemp would force 0o600); O_EXCL on a random name, as mkstemp does.
    tmp = str(path.parent / f".{path.stem}.{secrets.token_hex(8)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
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


def uuid_str(value: UUID | str, what: str) -> str:
    try:
        return str(UUID(str(value)))
    except ValueError as e:
        raise ValueError(f"{what} must be a UUID, got {value!r}") from e
