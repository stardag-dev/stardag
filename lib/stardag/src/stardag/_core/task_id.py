import json
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid5

from stardag.base_model import CONTEXT_MODE_KEY
from stardag.utils.resource_provider import resource_provider

if TYPE_CHECKING:
    from stardag._core.task import BaseTask

# Never change this value, it is used to generate stable UUID5 ids for tasks
_DEFAULT_TASK_UUID5_NAMESPACE = UUID("9ca26b27-f7ee-4044-8b3c-e335dc5778dc")

# If needed, users can override this with their own namespace
task_uuid5_namespace_provider = resource_provider(
    UUID,
    lambda: _DEFAULT_TASK_UUID5_NAMESPACE,
    "Namespace for task UUID5 generation.",
)


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
