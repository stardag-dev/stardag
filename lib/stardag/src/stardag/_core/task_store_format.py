"""The committed task store's file format: normalized, content-addressed
instance bodies.

One file per distinct instance body::

    <store>/<label>/<task_id>/<instance_hash>.json

``<label>`` is ``<namespace>.<Name>``, or ``<Name>`` for the root namespace,
escaped to be a safe directory name on every platform (see :func:`label`).
It is there for a human browsing the store; lookups go by task id and
instance hash only.

A file is an **envelope** around the body::

    {
      "stardag_task_store": 1,
      "task_id": "...",
      "instance_hash": "...",
      "namespace": "my_project.models",
      "name": "TrainedModel",
      "module": "my_project.models",
      "sources": [{"kind": "registry", "registry_url": ..., ...}],
      "body": {... every nested task replaced by a pointer ...}
    }

- **Pointers.** A nested task in the body is replaced by
  ``{"__stardag_task": {"task": <label>, "task_id": ..., "instance_hash":
  ...}}``, and its own body is stored in its own file, at
  ``<task>/<task_id>/<instance_hash>.json``: a reader follows it by that
  rule. The instance hash, not only the task id, is the address: two
  downstreams may embed the same completion with different
  non-significant fields, which are two bodies. ``task`` is the readable
  class label, for browsing; it is checked against the target file's
  namespace and name on read, and a mismatch is refused.
- **Lossless.** A nested task's sub-body in a canonical instance body is
  that task's own canonical instance body, so the pointer's instance hash
  is the hash of exactly the bytes it replaced, and re-inlining every
  pointer (:func:`denormalize`) reproduces the original body byte for byte.
- **Deduplicated.** A shared upstream is one file, pointed at by every
  downstream.
- **Deterministic.** Keys of the body are sorted, the envelope's keys come
  in a fixed order, two-space indentation with every property and array
  element on its own line, non-ASCII kept as is (UTF-8), a trailing
  newline: the same content is the same bytes, so diffs are minimal. The
  file formatting is independent of the instance hash, which is computed
  from the canonical compact body.
- **What looks like a task is stored as one.** A nested dict carrying the
  ``__namespace`` / ``__name`` keys of a registered task class is split
  into its own file, even in a plain ``dict`` field. Lossless all the same.
- **Verified vs metadata.** Only ``body`` is verified, against the file's
  address (its instance hash); every other envelope field is metadata.
- **``module``** is the ``__module__`` of the live class that wrote the
  file: an import hint for a reader in a fresh process (``None`` for a
  class defined in ``__main__``). Never derived from the namespace.
- **``sources``**: where the body came from, one entry per distinct origin:
  ``{"kind": "registry", "registry_url", "workspace", "environment",
  "instance_id"}`` for a body read from a registry (``instance_id`` on the
  instance actually read, not on the nested files written with it), or
  ``{"kind": "local"}`` for one written from a task object; both with
  ``written_at`` and ``stardag_version``. One body may be several
  registry instances (an instance is a body *under a scope*), so a file
  lists every instance id it is known as.
"""

from __future__ import annotations

import json
import re
import typing
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from stardag._core.task_id import canonical_body_json, instance_hash_of_body
from stardag.exceptions import StardagError
from stardag.polymorphic import NAME_KEY, NAMESPACE_KEY

FORMAT_KEY = "stardag_task_store"
FORMAT_VERSION = 1
#: The reserved key of a pointer to a nested task's own file.
POINTER_KEY = "__stardag_task"
# Source fields that do not distinguish one origin from another.
_SOURCE_STAMP_FIELDS = ("written_at", "stardag_version", "selection")
#: ``selection`` of a registry source whose instance is the one the task's
#: completing execution ran (see ``stardag.registry._instance_select``).
COMPLETED_EXECUTION = "completed_execution"


class TaskStoreError(StardagError):
    """The task store could not be read or written: not configured, a
    missing or unreadable file, a file that does not match its address, or
    a body that does not rehydrate to its task id."""


def is_pointer(value: Any) -> bool:
    return isinstance(value, dict) and len(value) == 1 and POINTER_KEY in value


def pointer(task: str, task_id: str, instance_hash: str) -> dict[str, Any]:
    """A pointer to a nested task's file, ``<task>/<task_id>/<instance_hash>.json``.
    ``task`` is the readable class label (:func:`label`), for browsing; the
    task id and instance hash are the address."""
    return {
        POINTER_KEY: {
            "instance_hash": instance_hash,
            "task": task,
            "task_id": task_id,
        }
    }


def pointer_address(value: dict[str, Any]) -> tuple[str, str]:
    """``(task_id, instance_hash)``: the authoritative part of a pointer."""
    inner = value[POINTER_KEY]
    if not (
        isinstance(inner, dict)
        and isinstance(inner.get("task_id"), str)
        and isinstance(inner.get("instance_hash"), str)
        and isinstance(inner.get("task"), str)
    ):
        raise TaskStoreError(f"Malformed task store pointer: {value!r}")
    return inner["task_id"], inner["instance_hash"]


def pointer_label(value: dict[str, Any]) -> str:
    return value[POINTER_KEY]["task"]


_WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


def label(namespace: str, name: str) -> str:
    """The directory naming a class in the store (for humans browsing it
    only: lookups never read it). Safe on every platform: characters
    outside ``[A-Za-z0-9_.-]`` become ``_``, a leading dot (``.``, ``..``,
    a hidden directory) is escaped with ``_``, as is a name Windows
    reserves (``CON``, ``NUL``, ``COM1``, ..., with or without an
    extension)."""
    raw = f"{namespace}.{name}" if namespace else name
    text = re.sub(r"[^A-Za-z0-9_.-]", "_", raw) or "_"
    if text.startswith(".") or text.split(".", 1)[0].upper() in _WINDOWS_RESERVED:
        text = "_" + text
    return text


def hash_of(body: Any) -> str:
    """The instance hash of a (fully inlined) body."""
    return str(instance_hash_of_body(canonical_body_json(body)))


@dataclass
class Envelope:
    """One stored body, as in a store file."""

    task_id: str
    instance_hash: str
    namespace: str
    name: str
    module: str | None
    body: dict[str, Any]
    sources: list[dict[str, Any]] = field(default_factory=list)

    @property
    def from_completed_execution(self) -> bool:
        """Whether a source records this body as the instance of the
        execution that completed the task (unverified metadata)."""
        return any(s.get("selection") == COMPLETED_EXECUTION for s in self.sources)

    @property
    def instance_ids(self) -> list[str]:
        return [s["instance_id"] for s in self.sources if s.get("instance_id")]

    def to_json_body(self) -> str:
        """The (normalized) body's canonical JSON, for comparing bodies."""
        return canonical_body_json(self.body)

    def to_json(self) -> str:
        """The file content: deterministic for the same envelope."""
        data = {
            FORMAT_KEY: FORMAT_VERSION,
            "task_id": self.task_id,
            "instance_hash": self.instance_hash,
            "namespace": self.namespace,
            "name": self.name,
            "module": self.module,
            "sources": [_sorted(s) for s in self.sources],
            "body": _sorted(self.body),
        }
        return json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + "\n"

    @classmethod
    def from_json(cls, text: str, where: str) -> Envelope:
        try:
            data = json.loads(text)
        except ValueError as e:
            raise TaskStoreError(f"{where} is not valid JSON: {e}") from e
        if not isinstance(data, dict) or FORMAT_KEY not in data:
            raise TaskStoreError(f"{where} is not a stardag task store file.")
        if data[FORMAT_KEY] != FORMAT_VERSION:
            raise TaskStoreError(
                f"{where} has task store format {data[FORMAT_KEY]!r}; this "
                f"version of stardag reads format {FORMAT_VERSION}. Upgrade "
                "stardag, or rewrite the file."
            )
        try:
            return cls(
                task_id=str(UUID(data["task_id"])),
                instance_hash=str(UUID(data["instance_hash"])),
                namespace=str(data["namespace"]),
                name=str(data["name"]),
                module=data.get("module"),
                body=dict(data["body"]),
                sources=list(data.get("sources") or []),
            )
        except (KeyError, TypeError, ValueError) as e:
            raise TaskStoreError(f"{where} is missing or has a bad {e}.") from e

    def with_source(self, source: dict[str, Any]) -> Envelope | None:
        """This envelope plus ``source``, or ``None`` when an equivalent
        source is already listed (nothing to write). An equivalent source
        learning that its instance is the completing execution's is
        updated to say so."""
        for i, s in enumerate(self.sources):
            if source_identity(s) != source_identity(source):
                continue
            if (
                source.get("selection") == COMPLETED_EXECUTION
                and s.get("selection") != COMPLETED_EXECUTION
            ):
                sources = list(self.sources)
                sources[i] = {**s, "selection": COMPLETED_EXECUTION}
                return Envelope(
                    task_id=self.task_id,
                    instance_hash=self.instance_hash,
                    namespace=self.namespace,
                    name=self.name,
                    module=self.module,
                    body=self.body,
                    sources=sources,
                )
            return None
        return Envelope(
            task_id=self.task_id,
            instance_hash=self.instance_hash,
            namespace=self.namespace,
            name=self.name,
            module=self.module,
            body=self.body,
            sources=[*self.sources, source],
        )


def source_identity(source: dict[str, Any]) -> tuple[tuple[str, Any], ...]:
    return tuple(
        sorted(
            (k, json.dumps(v, sort_keys=True))
            for k, v in source.items()
            if k not in _SOURCE_STAMP_FIELDS
        )
    )


def _sorted(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _sorted(value[k]) for k in sorted(value)}
    if isinstance(value, list):
        return [_sorted(v) for v in value]
    return value


# -----------------------------------------------------------------------------
# Normalizing and denormalizing
# -----------------------------------------------------------------------------


class NestedTask(typing.NamedTuple):
    """What the normalizer needs to know about a nested task body."""

    task_id: str
    namespace: str
    name: str
    module: str | None


def contains_pointer_key(value: Any) -> bool:
    if isinstance(value, dict):
        return POINTER_KEY in value or any(
            contains_pointer_key(v) for v in value.values()
        )
    if isinstance(value, list):
        return any(contains_pointer_key(v) for v in value)
    return False


def normalize(
    body: dict[str, Any],
    root: NestedTask,
    identify: typing.Callable[[dict[str, Any], str], NestedTask | None],
) -> tuple[str, dict[tuple[str, str], Envelope]]:
    """Split ``body`` (a full instance body) into envelopes, one per distinct
    body in its tree, keyed by ``(task_id, instance_hash)``; returns the root's
    instance hash and the envelopes (sources left empty).

    ``identify(sub_body, instance_hash)`` says whether a nested dict is a task
    body (``None`` when it is not, e.g. a non-task polymorphic model) and
    gives its task id and class.
    """
    if contains_pointer_key(body):
        raise TaskStoreError(
            f"The body contains the reserved key {POINTER_KEY!r}, which the "
            "task store uses for pointers; it cannot be stored."
        )
    envelopes: dict[tuple[str, str], Envelope] = {}
    memo: dict[str, dict[str, Any]] = {}

    def store(sub: dict[str, Any], info: NestedTask) -> str:
        sub_hash = hash_of(sub)
        key = (info.task_id, sub_hash)
        if key not in envelopes:
            envelopes[key] = Envelope(
                task_id=info.task_id,
                instance_hash=sub_hash,
                namespace=info.namespace,
                name=info.name,
                module=info.module,
                body={k: walk(v) for k, v in sub.items()},
            )
        return sub_hash

    def walk(value: Any) -> Any:
        if isinstance(value, dict):
            if isinstance(value.get(NAMESPACE_KEY), str) and isinstance(
                value.get(NAME_KEY), str
            ):
                sub_json = canonical_body_json(value)
                cached = memo.get(sub_json)
                if cached is not None:
                    return cached
                sub_hash = str(instance_hash_of_body(sub_json))
                info = identify(value, sub_hash)
                if info is not None:
                    store(value, info)
                    memo[sub_json] = pointer(
                        label(info.namespace, info.name), info.task_id, sub_hash
                    )
                    return memo[sub_json]
            return {k: walk(v) for k, v in value.items()}
        if isinstance(value, list):
            return [walk(v) for v in value]
        return value

    return store(body, root), envelopes


def denormalize(
    envelope: Envelope,
    load: typing.Callable[[str, str, str], Envelope],
    memo: dict[tuple[str, str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """``envelope``'s full body, every pointer re-inlined recursively.

    ``load(task_id, instance_hash, referenced_from)`` reads a pointed-at
    envelope. Each re-inlined body is checked against the instance hash it
    is stored under, so a hand-edited file is refused rather than loaded.
    """
    memo = {} if memo is None else memo
    key = (envelope.task_id, envelope.instance_hash)
    if key in memo:
        return memo[key]

    def walk(value: Any) -> Any:
        if is_pointer(value):
            task_id, instance_hash = pointer_address(value)
            nested = load(task_id, instance_hash, envelope.task_id)
            expected = pointer_label(value)
            if label(nested.namespace, nested.name) != expected:
                raise TaskStoreError(
                    f"The stored body of task {envelope.task_id} points to "
                    f"{expected} {task_id}, but that file holds a "
                    f"{label(nested.namespace, nested.name)}. The pointer's "
                    "task name is for reading only and must match the file it "
                    "points to; the file was edited by hand."
                )
            return denormalize(nested, load, memo)
        if isinstance(value, dict):
            return {k: walk(v) for k, v in value.items()}
        if isinstance(value, list):
            return [walk(v) for v in value]
        return value

    body = walk(envelope.body)
    actual = hash_of(body)
    if actual != envelope.instance_hash:
        raise TaskStoreError(
            f"The stored body of task {envelope.task_id} (instance hash "
            f"{envelope.instance_hash}) hashes to {actual}: the file, or one "
            "it points to, was edited. Store files are content-addressed and "
            "must not be edited by hand; rewrite them with "
            f"'stardag tasks ref {envelope.task_id} --write' or "
            "sd.write_to_store(task)."
        )
    memo[key] = body
    return body
