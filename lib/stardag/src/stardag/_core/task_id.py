"""The two task hashes, and the one place each is computed.

- The **task id** (:func:`_get_task_id_from_jsonable`): uuid5 over the
  hash-mode dump — significant fields only, compat defaults dropped.
- The **instance hash** (:func:`instance_hash_of_body`): uuid5 over the
  canonical JSON of the instance body — every field — under a namespace
  derived from the task-id namespace, so the two can never coincide.
"""

import json
from typing import TYPE_CHECKING, Any
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
    ``stardag.base_model``). ``NaN`` and infinities are refused
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
