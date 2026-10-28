"""Writing the committed task store: from task objects
(:func:`write_to_store`, ``sd.write_to_store``) and from the registry
(:func:`write_from_registry`, ``stardag tasks ref <id> --write``).

See :mod:`stardag._core.task_store` for the store and its reader, and
:mod:`stardag._core.task_store_format` for the file format.
"""

from __future__ import annotations

import datetime
import json
import os
import typing
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from stardag._core.base_task import BaseTask, TaskStruct, flatten_task_struct
from stardag._core.instance import reachable_models
from stardag._core.task_store import TaskStore, project_config, resolve_store, uuid_str
from stardag._core.task_store_format import NestedTask, TaskStoreError, normalize

if typing.TYPE_CHECKING:
    from stardag.registry import RegistryABC


@dataclass(frozen=True)
class StoredTask:
    """One root written to the store, and what to paste to read it.

    ``str(stored)`` is the import (when the class is importable by name)
    and the ``from_store(...)`` call.
    """

    task_class: type[BaseTask]
    task_id: UUID
    instance_hash: UUID
    instance_id: UUID | None
    #: The root's file.
    path: Path
    #: Files created or updated (empty when the store already held it all).
    written: tuple[Path, ...]
    #: The ``store=`` argument the reader needs: the store directory as
    #: given, when it is not the configured store; ``None`` otherwise.
    store_argument: str | None = None

    @property
    def import_line(self) -> str | None:
        from stardag._core.task_ref import reference_line

        return reference_line(self.task_class)[0]

    @property
    def call(self) -> str:
        """The ``from_store(...)`` expression reading this root back."""
        from stardag._core.task_ref import reference_line

        prefix = reference_line(self.task_class)[1]
        return reader_call(
            prefix,
            self.task_class,
            str(self.task_id),
            instance_id=None if self.instance_id is None else str(self.instance_id),
            instance_hash=str(self.instance_hash),
            store=self.store_argument,
        )

    def __str__(self) -> str:
        return "\n".join(line for line in (self.import_line, self.call) if line)


def reader_call(
    prefix: str,
    task_class: type | None,
    task_id: str,
    *,
    instance_id: str | None,
    instance_hash: str | None,
    store: str | None = None,
) -> str:
    """``Prefix.from_store("<task-id>", instance_id=..., instance_hash=...)``,
    pinned to the registry instance when known and always to the instance
    hash (the verified one); or
    ``sd.task_from_store(Prefix, ...)`` for a class whose ``from_store``
    is shadowed by a field."""
    # Both when known: the instance id is matched against unverified
    # metadata (``sources``); the hash is the file's verified address.
    pinned = ""
    if instance_id is not None:
        pinned += f', instance_id="{instance_id}"'
    if instance_hash is not None:
        pinned += f', instance_hash="{instance_hash}"'
    if store is not None:
        pinned += f", store={json.dumps(store)}"
    fields = getattr(task_class, "model_fields", None) or {}
    if "from_store" in fields:
        return f'sd.task_from_store({prefix}, "{task_id}"{pinned})'
    return f'{prefix}.from_store("{task_id}"{pinned})'


def store_argument(
    store: str | os.PathLike[str] | None,
    task_store: TaskStore,
    anchors: typing.Sequence[Path | None],
) -> str | None:
    """The ``store=`` a printed reader line needs: ``store`` exactly as given
    (a relative path stays relative, and resolves the same in the reader),
    unless it resolves to the store configured for the same anchors, which
    the reader finds by itself. An absolute path outside the package whose
    settings apply is machine-specific: printed, with a warning."""
    import warnings

    if store is None:
        return None
    try:
        config = project_config(anchors)
    except TaskStoreError:
        return os.fspath(store)  # no applicable settings to compare against
    if config.task_store is not None and config.task_store == task_store.root:
        return None
    text = os.fspath(store)
    if (
        Path(text).is_absolute()
        and config.root is not None
        and not task_store.root.is_relative_to(config.root)
    ):
        warnings.warn(
            f"The task store {text} is an absolute path outside the package, so "
            "the printed from_store(..., store=...) line only works on this "
            "machine. Configure [tool.stardag] task_store in the package's "
            "pyproject.toml, or pass a path relative to it, or 'pkg:<package>/<dir>'.",
            stacklevel=3,
        )
    return text


def module_hint(cls: type) -> str | None:
    """The import hint recorded for ``cls``: its ``__module__``, unless that
    is ``__main__`` (meaningless in another process)."""
    module = cls.__module__
    return None if module == "__main__" else module


def _stamp() -> dict[str, Any]:
    from stardag._version import __version__

    now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    return {"written_at": now.isoformat(), "stardag_version": __version__}


def _identify_from(live: typing.Iterable[BaseTask]):
    """The normalizer's ``identify``: by instance hash from the live task
    objects in the tree, else by validating the sub-body."""
    by_hash: dict[str, BaseTask] = {}
    for task in live:
        try:
            by_hash.setdefault(str(task.instance_hash), task)
        except Exception:
            continue

    def identify(sub: dict[str, Any], instance_hash: str) -> NestedTask | None:
        from stardag._core import task_imports
        from stardag.polymorphic import NAME_KEY, NAMESPACE_KEY

        task = by_hash.get(instance_hash)
        if task is None:
            namespace, name = sub[NAMESPACE_KEY], sub[NAME_KEY]
            if not task_imports.is_registered(namespace, name):
                return None
            from stardag._core.rehydrate import task_from_body

            try:
                task = task_from_body(sub)
                task.id  # noqa: B018 (computes the id, may raise)
            except Exception:
                # Not a task body after all (a non-task polymorphic model
                # sharing a class name): kept inline, losslessly.
                return None
        return NestedTask(
            task_id=str(task.id),
            namespace=task.get_namespace(),
            name=task.get_name(),
            module=module_hint(type(task)),
        )

    return identify


def _live_tasks(task: BaseTask) -> list[BaseTask]:
    return [m for m in reachable_models(task) if isinstance(m, BaseTask)]


def _write_tree(
    task_store: TaskStore,
    task: BaseTask,
    body: dict[str, Any],
    root_source: dict[str, Any],
    nested_source: dict[str, Any],
) -> tuple[Path, str, list[Path]]:
    root = NestedTask(
        task_id=str(task.id),
        namespace=task.get_namespace(),
        name=task.get_name(),
        module=module_hint(type(task)),
    )
    root_hash, envelopes = normalize(body, root, _identify_from(_live_tasks(task)))
    written: list[Path] = []
    root_key = (root.task_id, root_hash)
    # Nested first: a reader never finds a root whose pointers dangle, even
    # if writing is interrupted.
    for key in sorted(envelopes, key=lambda k: k == root_key):
        source = root_source if key == root_key else nested_source
        path = task_store.write(envelopes[key], dict(source))
        if path is not None:
            written.append(path)
    return task_store.path_for(envelopes[root_key]), root_hash, written


def write_to_store(
    tasks: TaskStruct,
    *,
    store: str | os.PathLike[str] | None = None,
) -> list[StoredTask]:
    """Write each task's whole tree into the committed task store.

    From the task objects in hand, with no registry involved, so a task not
    built yet can be pinned too. Each distinct body becomes one file
    (shared upstreams once); a file already holding the body is left as is.
    Read a root back with ``MyTask.from_store(task_id, instance_hash=...)``
    (``str()`` of each returned :class:`StoredTask` is that line, with its
    import).

    For an instance that ran, prefer writing it from the registry
    (``stardag tasks ref <task-id> --write``): the file then records the
    registry instance id it was read as.

    Args:
        tasks: A task, or several (a list, tuple or dict of them).
        store: The store directory. Default: the configured one
            (``[tool.stardag] task_store`` of the calling package).

    Returns:
        One :class:`StoredTask` per root, in order.

    Raises:
        TaskStoreError: No store is configured, or a file at a body's address
            holds another body.
        TaskRefNotHydratedError: A task holds an unhydrated ``TaskRef``
            (``sd.resolve_refs`` it first).
    """
    from stardag._core.anchor import call_anchors

    roots = flatten_task_struct(tasks)
    anchors = call_anchors(*(type(t) for t in roots[:1]))
    task_store = resolve_store(store, anchors)
    source = {"kind": "local", **_stamp()}
    results: list[StoredTask] = []
    for task in roots:
        body = task.instance_body()
        _refuse_aliased(body, str(task.id))
        path, root_hash, written = _write_tree(task_store, task, body, source, source)
        results.append(
            StoredTask(
                task_class=type(task),
                task_id=task.id,
                instance_hash=UUID(root_hash),
                instance_id=None,
                path=path,
                written=tuple(written),
                store_argument=store_argument(store, task_store, anchors),
            )
        )
    return results


def write_from_registry(
    task_id: UUID | str,
    *,
    instance_id: UUID | str | None = None,
    registry: RegistryABC | None = None,
    store: str | os.PathLike[str] | None = None,
    anchors: typing.Sequence[Path | None] = (),
) -> StoredTask:
    """Read an instance body from the registry and write its tree into the
    store (``stardag tasks ref <task-id> --write``).

    The body written is the registry's, as stored: re-inlining the file's
    pointers gives the registry's bytes, and the file records the instance
    id. It is first checked to rehydrate to ``task_id`` under the current
    code (every class in it imported), and nothing is written otherwise.

    Args:
        task_id: The task.
        instance_id: The instance to write. Default: the newest.
        registry: Default: the configured registry.
        store: Default: the configured store.
        anchors: Where the store config is looked up from. Default: the
            working directory (the CLI writes into the package it runs in).

    Raises:
        TaskStoreError: The body does not rehydrate to the task id (the
            error names the cause), the task has no instance, or no store
            is configured.
    """
    from stardag._core.rehydrate import task_from_body
    from stardag.registry import registry_provider

    task_store = resolve_store(store, anchors)
    registry = registry or registry_provider.get()
    tid = uuid_str(task_id, "task_id")
    if instance_id is not None:
        instance = registry.task_instance_get(instance_id, task_id=tid)
        if instance.task_id is not None and str(UUID(instance.task_id)) != tid:
            raise TaskStoreError(
                f"Instance {instance_id} belongs to task {instance.task_id}, "
                f"not to task {tid}."
            )
    else:
        info = registry.task_get(tid)
        if not info.instances:
            raise TaskStoreError(f"Task {tid} has no instance to write.")
        instance = info.instances[0]
    body = dict(instance.body)
    _refuse_aliased(body, tid)
    try:
        task = task_from_body(body, expected_task_id=tid)
    except Exception as e:
        raise TaskStoreError(
            f"Not writing instance {instance.id} of task {tid}: its body does "
            f"not rehydrate to the task under the current code. {e}"
        ) from e
    origin = registry_origin(registry)
    stamp = _stamp()
    path, root_hash, written = _write_tree(
        task_store,
        task,
        body,
        {**origin, "instance_id": str(instance.id), **stamp},
        {**origin, **stamp},
    )
    return StoredTask(
        task_class=type(task),
        task_id=UUID(tid),
        instance_hash=UUID(root_hash),
        instance_id=instance.id,
        path=path,
        written=tuple(written),
        store_argument=store_argument(store, task_store, anchors),
    )


def _refuse_aliased(body: Any, task_id: str) -> None:
    from stardag._core.rehydrate import _contains_aliased

    if _contains_aliased(body):
        raise TaskStoreError(
            f"Task {task_id} holds an AliasTask, whose body embeds pickled "
            "bytes; it cannot be stored. Use a task reference instead."
        )


def registry_origin(registry: Any) -> dict[str, Any]:
    """The provenance recorded for a body read from ``registry``: its URL,
    workspace and environment as the client itself resolved them (ids),
    named by the active profile's slugs only when that profile resolves to
    exactly this scope. Never the ambient profile's scope otherwise: a CLI
    ``-p`` override builds the client and then restores the ambient
    profile."""
    from stardag.config import get_config

    url = getattr(registry, "api_url", None)
    environment = getattr(registry, "environment_id", None) or None
    workspace = getattr(registry, "_cache_workspace_id", None) or None
    try:
        config = get_config()
    except Exception:
        config = None
    reg = config.registry if config is not None else None
    if (
        reg is not None
        and url is not None
        and reg.url.rstrip("/") == url.rstrip("/")
        and (reg.environment_id or None) == environment
        and (reg.workspace_id or None) == workspace
    ):
        slugs = _profile_slugs(config.context.profile) if config else None
        if slugs is not None:
            workspace, environment = slugs
    return {
        "kind": "registry",
        "registry_url": url,
        "workspace": workspace,
        "environment": environment,
    }


def _profile_slugs(profile: str | None) -> tuple[str, str] | None:
    if not profile:
        return None
    from stardag.config.io import load_toml_file
    from stardag.config.models import TomlConfig
    from stardag.config.paths import find_project_config, get_user_config_path

    data = load_toml_file(get_user_config_path())
    project = find_project_config()
    if project is not None:
        for key, value in load_toml_file(project).get("profile", {}).items():
            data.setdefault("profile", {})[key] = value
    found = TomlConfig.from_toml_dict(data).profile.get(profile)
    return (found.workspace, found.environment) if found else None
