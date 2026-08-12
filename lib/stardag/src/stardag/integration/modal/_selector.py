"""Worker selection: which Modal worker function a task routes to.

A selector is deployed-app configuration — every scheduler tick and every
resident build of an app uses the same one, so routing cannot change
mid-build.
"""

from __future__ import annotations

import typing

from stardag import BaseTask

WorkerSelection = typing.Union[str, tuple[str, dict[str, str]]]
"""Return type of a :data:`WorkerSelector`.

Either a worker name (``str``), or a ``(worker_name, env_overrides)`` tuple
where ``env_overrides`` is a dict of environment variables to set temporarily
around the task's ``run`` call inside the worker container (e.g. to tune
task-specific execution knobs such as worker/thread counts or batch sizes).
See :meth:`stardag.integration.modal.Runner.__call__`.
"""

WorkerSelector = typing.Callable[[BaseTask], WorkerSelection]
"""Type for functions that select which worker to use for a task.

A selector returns either a worker name, or a ``(worker_name, env_overrides)``
tuple (see :data:`WorkerSelection`).
"""


def _normalize_worker_selection(
    selection: WorkerSelection,
) -> tuple[str, dict[str, str] | None]:
    """Split a :data:`WorkerSelection` into ``(worker_name, env_overrides)``.

    Accepts either a bare worker name or a ``(worker_name, env_overrides)``
    tuple and always returns the two-tuple form (``env_overrides`` is ``None``
    when the selector returned a bare name).
    """
    if isinstance(selection, tuple):
        worker_name, env_overrides = selection
        return worker_name, env_overrides
    return selection, None


def _default_worker_selector(task: BaseTask) -> WorkerSelection:
    """Default worker selector - always returns 'default'."""
    return "default"


class WorkerSelectorByName:
    """Worker selector that routes tasks based on task name.

    Example:
        selector = WorkerSelectorByName(
            name_to_worker={"heavy_task": "gpu", "io_task": "high_memory"},
            default_worker="default",
        )
        stardag_app = StardagApp(..., worker_selector=selector)
    """

    def __init__(self, name_to_worker: dict[str, str], default_worker: str):
        """Initialize the selector.

        Args:
            name_to_worker: Dict mapping task names to worker names.
            default_worker: Worker name to use for tasks not in the mapping.
        """
        self.name_to_worker = name_to_worker
        self.default_worker = default_worker

    def __call__(self, task: BaseTask) -> str:
        return self.name_to_worker.get(task.get_name(), self.default_worker)

    def __repr__(self):
        return (
            f"{self.__class__.__name__}({self.name_to_worker}, {self.default_worker})"
        )
