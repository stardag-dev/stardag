"""The two task hashes, and the one place each is computed.

- The **task id** (:func:`_get_task_id_from_jsonable`): uuid5 over the
  hash-mode dump — significant fields only, compat defaults dropped.
- The **instance hash** (:func:`instance_hash_of_body`): uuid5 over the
  canonical JSON of the instance body — every field — under a namespace
  derived from the task-id namespace, so the two can never coincide.
"""

import dataclasses
import json
from typing import TYPE_CHECKING, Any, Mapping
from uuid import UUID, uuid5

from stardag.base_model import CONTEXT_MODE_KEY
from stardag.utils.resource_provider import resource_provider

if TYPE_CHECKING:
    from stardag._core.base_task import BaseTask

# Never change this value, it is used to generate stable UUID5 ids for tasks
_DEFAULT_TASK_UUID5_NAMESPACE = UUID("9ca26b27-f7ee-4044-8b3c-e335dc5778dc")

# If needed, users can override this with their own namespace
task_uuid5_namespace_provider = resource_provider(
    UUID,
    lambda: _DEFAULT_TASK_UUID5_NAMESPACE,
    "Namespace for task UUID5 generation.",
)


# The instance-hash namespace is derived, not fixed: an override of the task
# namespace moves both, and the two are distinct for any task namespace.
_INSTANCE_HASH_SALT = "stardag.instance_hash.v1"


def _hash_safe_json_dumps(obj):
    """Fixed separators and (deep) sort_keys for stable hash."""
    return json.dumps(
        obj,
        separators=(",", ":"),
        sort_keys=True,
    )


def _get_task_id_from_jsonable(data: dict) -> UUID:
    """Get the task ID from serialized task data.

    Args:
        data: The serialized task, obtained by:
            `task.model_dump(mode="json", context={CONTEXT_MODE_KEY: "hash"})`
            *bypassing/excluding* the `_hash_mode_finalize` logic.

    Returns:
        The UUID5 generated from the task data.
    """
    return uuid5(
        task_uuid5_namespace_provider.get(),
        _hash_safe_json_dumps(data),
    )


def _get_task_id_jsonable(task: "BaseTask") -> dict[str, Any]:
    """Get the hash mode JSONable representation of a task used to generate the task id.

    This is a testing util *bypassing/excluding* the `_hash_mode_finalize` logic."""
    task = task.model_copy()
    task._hash_mode_finalize = lambda data, info: data
    return task.model_dump(
        mode="json",
        context={CONTEXT_MODE_KEY: "hash"},
    )


def canonical_body_json(body: Any) -> str:
    """The canonical JSON of an instance body: sorted keys, compact
    separators, non-ASCII kept as is (the string is hashed as UTF-8).

    Sets are already sorted by the registry-mode dump (see
    ``stardag.base_model``, which applies :func:`canonicalize_sets` to every
    field). ``NaN`` and infinities are refused
    (``ValueError``): they are not JSON, and a registry could not store them.
    """
    return json.dumps(
        body,
        separators=(",", ":"),
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
    )


def instance_hash_of_body(body_json: str) -> UUID:
    """The instance hash of a canonical body (see :func:`canonical_body_json`).

    Rule 1 of the design: the instance hash is the hash of the stored
    bytes, so ``instance_hash <-> body`` is 1:1 by construction.
    """
    namespace = uuid5(task_uuid5_namespace_provider.get(), _INSTANCE_HASH_SALT)
    return uuid5(namespace, body_json)


# -- Sets ---------------------------------------------------------------------
#
# Canonical JSON sorts keys itself, but a set has already become a list by the
# time the dump reaches ``json.dumps``, in an iteration order that differs
# between processes (string hashing is randomised). So sets are ordered during
# the hash- and registry-mode dumps, at every nesting level, by the functions
# below. They are the only definition of that order: ``base_model`` applies
# them to every field and ``HashSafeSetSerializer`` uses them for its
# tie-break.

# Context key under which ``HashSafeSetSerializer`` records the sets it has
# already ordered by its own ``sort_key``: id(raw set) -> (raw set, its
# ordered dump). The walk keeps that order instead of re-sorting.
_ORDERED_SETS_CONTEXT_KEY = "__stardag_ordered_sets"


def canonical_item_key(item: Any) -> str:
    """The canonical JSON of one (already dumped) item, as a sort key.

    Non-finite floats are allowed here: this only orders; the body dump
    refuses them later with a proper error.
    """
    return json.dumps(item, separators=(",", ":"), sort_keys=True, ensure_ascii=False)


def sorted_canonically(items: list[Any]) -> list[Any]:
    """``items`` (the dump of a set's members) in their canonical order.

    By value where the items compare, else by :func:`canonical_item_key`.
    Either way two items that tie are equal as JSON, so the output does not
    depend on the input order.
    """
    try:
        return sorted(items)
    except TypeError:
        return sorted(items, key=canonical_item_key)


def record_ordered_set(
    context: dict[str, Any] | None, raw: Any, ordered: list[Any]
) -> None:
    """Record that ``ordered`` is the canonical dump of the set ``raw``."""
    if context is not None:
        context.setdefault(_ORDERED_SETS_CONTEXT_KEY, {})[id(raw)] = (raw, ordered)


def canonicalize_sets(
    raw: Any, dumped: Any, context: Mapping[str, Any] | None = None
) -> Any:
    """``dumped`` (the serialization of ``raw``) with every set at every
    nesting level sorted into its canonical order.

    ``raw`` says which lists came from sets; the two trees are walked in
    parallel (dict values by position, falling back to key, list and tuple
    items by index, model and dataclass fields by name). A nested
    ``StardagBaseModel`` is returned as is: its own dump already applied
    this to its fields. A set dumped by ``HashSafeSetSerializer`` keeps its
    ``sort_key`` order. Where the two trees do not line up (a custom
    serializer reshaped the value) the walk stops and returns ``dumped``
    unchanged below that point.
    """
    # Local import: base_model imports this module lazily.
    from pydantic import BaseModel

    from stardag.base_model import StardagBaseModel

    if isinstance(raw, StardagBaseModel):
        return dumped
    if isinstance(raw, (set, frozenset)):
        if not isinstance(dumped, list) or len(dumped) != len(raw):
            return dumped
        ordered = (context or {}).get(_ORDERED_SETS_CONTEXT_KEY, {}).get(id(raw))
        if ordered is not None and ordered[0] is raw and ordered[1] == dumped:
            return dumped
        # A plain set dumps in its iteration order, so pairing is by position.
        items = [canonicalize_sets(r, d, context) for r, d in zip(raw, dumped)]
        return sorted_canonically(items)
    if isinstance(raw, (list, tuple)):
        if not isinstance(dumped, list) or len(dumped) != len(raw):
            return dumped
        return [canonicalize_sets(r, d, context) for r, d in zip(raw, dumped)]
    if isinstance(raw, dict):
        if not isinstance(dumped, dict):
            return dumped
        if len(dumped) == len(raw):
            return {
                key: canonicalize_sets(r, d, context)
                for r, (key, d) in zip(raw.values(), dumped.items())
            }
        by_str = {str(k): v for k, v in raw.items()}
        return {
            key: canonicalize_sets(by_str[key], d, context) if key in by_str else d
            for key, d in dumped.items()
        }
    if isinstance(raw, BaseModel):
        names = type(raw).model_fields
    elif dataclasses.is_dataclass(raw) and not isinstance(raw, type):
        names = [f.name for f in dataclasses.fields(raw)]
    else:
        return dumped
    if not isinstance(dumped, dict):
        return dumped
    out = dict(dumped)
    for name in names:
        if name in out:
            out[name] = canonicalize_sets(getattr(raw, name, None), out[name], context)
    return out
