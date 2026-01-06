import logging
from uuid import UUID

from stardag._task import BaseTask, flatten_task_struct
from stardag.build.registry import RegistryABC, registry_provider
from stardag.build.task_runner import RunCallback, TaskRunner

logger = logging.getLogger(__name__)


def build(
    task: BaseTask,
    *,
    completion_cache: set[UUID] | None = None,
    task_runner: TaskRunner | None = None,
    # TODO clean up duplicate arg options
    before_run_callback: RunCallback | None = None,
    on_complete_callback: RunCallback | None = None,
    registry: RegistryABC | None = None,
) -> None:
    registry = registry or registry_provider.get()
    task_runner = task_runner or TaskRunner(
        before_run_callback=before_run_callback,
        on_complete_callback=on_complete_callback,
        registry=registry,
    )

    registry.start_build(root_tasks=[task])

    try:
        _build(task, completion_cache or set(), task_runner=task_runner)
        registry.complete_build()
    except Exception as e:
        registry.fail_build(str(e))
        raise


def _build(
    task: BaseTask, completion_cache: set[UUID], task_runner: TaskRunner
) -> None:
    logger.info(f"Building task: {task}")
    if _is_complete(task, completion_cache):
        return

    deps = flatten_task_struct(task.requires())
    logger.info(f"Task '{task}' has {len(deps)} dependencies.")
    for dep in deps:
        _build(dep, completion_cache, task_runner)

    logger.info(f"Task: {task} has no pending dependencies. Running.")
    try:
        res = task_runner.run(task)
    except Exception as e:
        logger.exception(f"Error running task: {task} - {e}")
        raise

    if res is not None:
        # TODO: Handle dynamic dependencies
        raise NotImplementedError("Tasks with dynamic dependencies are not supported")

    completion_cache.add(task.id)


def _is_complete(task: BaseTask, completion_cache: set[UUID]) -> bool:
    if task.id in completion_cache:
        return True
    if task.complete():
        completion_cache.add(task.id)
        return True
    return False
