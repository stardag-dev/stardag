import modal
from pydantic import TypeAdapter

import stardag as sd
from stardag.build.task_runner import TaskRunner


class ModalTaskRunner(TaskRunner):
    def _run_task(self, task):
        return run_task.remote(task_adapter.dump_json(task).decode("utf-8"))


global_app = modal.App("stardag-poc")


image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install(
        "pydantic>=2.8.2",
        "pydantic-settings>=2.7.1",
        "uuid6>=2024.7.10",
    )
    .add_local_python_source("stardag")
)


task_adapter = TypeAdapter(sd.TaskParam[sd.Task])
tasks_adapter = TypeAdapter(list[sd.TaskParam[sd.Task]])


@global_app.function(image=image)
def run_task(task_json: str):
    task = task_adapter.validate_json(task_json)
    print(f"Running task: '{task_json}'")
    task.run()
    print(f"Task '{task_json}' completed")
    print(f"task.output().path: {task.output().path}")


@global_app.function(image=image)
def build(task_json: str):
    task = task_adapter.validate_json(task_json)
    print(f"Building task: '{task_json}'")
    sd.build(task, task_runner=ModalTaskRunner())
    print(f"Completed building task '{task_json}'")


@sd.task(family="Range")
def get_range(limit: int) -> list[int]:
    return list(range(limit))


@sd.task(family="Sum")
def get_sum(integers: sd.Depends[list[int]]) -> int:
    return sum(integers)


@global_app.local_entrypoint()
def main(limit: int = 10):
    root = get_sum(integers=get_range(limit=limit))
    # sd.build(root, task_runner=ModalTaskRunner())
    build.remote(task_adapter.dump_json(root).decode("utf-8"))


if __name__ == "__main__":
    root = get_sum(integers=get_range(limit=10))
