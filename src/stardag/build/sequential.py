import logging

from stardag._base import Task
from stardag.build.registry import RegistryABC, registry_provider
from stardag.build.task_runner import RunCallback, TaskRunner

logger = logging.getLogger(__name__)


def build(
    task: Task,
    completion_cache: set[str] | None = None,
    task_runner: TaskRunner | None = None,
    before_run_callback: RunCallback | None = None,
    on_complete_callback: RunCallback | None = None,
    registry: RegistryABC | None = None,
) -> None:
    task_runner = task_runner or TaskRunner(
        before_run_callback=before_run_callback,
        on_complete_callback=on_complete_callback,
        registry=registry or registry_provider.get(),
    )

    _build(task, completion_cache or set(), task_runner=task_runner)


def _build(task: Task, completion_cache: set[str], task_runner: TaskRunner) -> None:
    logger.info(f"Building task: {task}")
    if _is_complete(task, completion_cache):
        return

    deps = task.deps()
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

    completion_cache.add(task.task_id)


def _is_complete(task: Task, completion_cache: set[str]) -> bool:
    if task.task_id in completion_cache:
        return True
    if task.complete():
        completion_cache.add(task.task_id)
        return True
    return False
