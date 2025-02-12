from stardag._base import Task
from stardag.build.registry import RegistryABC, registry_provider
from stardag.build.task_runner import RunCallback, TaskRunner


def build(
    task: Task,
    completion_cache: set[str] | None = None,
    before_run_callback: RunCallback | None = None,
    on_complete_callback: RunCallback | None = None,
    registry: RegistryABC | None = None,
) -> None:
    task_runner = TaskRunner(
        before_run_callback=before_run_callback,
        on_complete_callback=on_complete_callback,
        registry=registry or registry_provider.get(),
    )

    _build(task, completion_cache or set(), task_runner=task_runner)


def _build(task: Task, completion_cache: set[str], task_runner: TaskRunner) -> None:
    if _is_complete(task, completion_cache):
        return

    for dep in task.deps():
        _build(dep, completion_cache, task_runner)

    res = task_runner.run(task)
    if res is not None:
        raise NotImplementedError("Tasks with dynamic dependencies are not supported")

    completion_cache.add(task.task_id)


def _is_complete(task: Task, completion_cache: set[str]) -> bool:
    if task.task_id in completion_cache:
        return True
    if task.complete():
        completion_cache.add(task.task_id)
        return True
    return False
