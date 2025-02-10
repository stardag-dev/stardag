from stardag.build.registry import RegistryABC, registry_provider
from stardag.task import Task


def build(
    task: Task,
    completion_cache: set[str] | None = None,
    registry: RegistryABC | None = None,
) -> None:
    registry = registry or registry_provider.get()
    _build(task, completion_cache or set(), registry=registry)


def _build(task: Task, completion_cache: set[str], registry: RegistryABC) -> None:
    if _is_complete(task, completion_cache):
        return

    for dep in task.deps():
        _build(dep, completion_cache, registry)

    task.run()
    registry.register(task)  # TODO add pre-run registration
    completion_cache.add(task.task_id)


def _is_complete(task: Task, completion_cache: set[str]) -> bool:
    if task.task_id in completion_cache:
        return True
    if task.complete():
        completion_cache.add(task.task_id)
        return True
    return False
