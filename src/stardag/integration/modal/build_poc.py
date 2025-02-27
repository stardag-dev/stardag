"""Plan:
- Provide sd.integrations.modal.build which takes as input a mapping
from task class to runner (App, function name | Class.method name) or a generic callable
which takes a task and returns something on the same format. (Can also return None, for
runnig in the build function itself or an actual Function / Method)
-
"""

import modal
from pydantic import TypeAdapter

import stardag as sd
from stardag.build.task_runner import TaskRunner

global_app = modal.App("stardag-poc")
vol = modal.Volume.from_name("stardag-poc-volume", create_if_missing=True)


class ModalTaskRunner(TaskRunner):
    def __init__(self, *, family_to_runner: dict[str, str], **kwargs):
        super().__init__(**kwargs)
        self.family_to_runner = family_to_runner

    def _run_task(self, task):
        runner_name = self.family_to_runner[task.get_family()]
        runner = global_app.registered_functions[runner_name]
        return runner.remote(task_adapter.dump_json(task).decode("utf-8"))


image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install(
        "pydantic>=2.8.2",
        "pydantic-settings>=2.7.1",
        "uuid6>=2024.7.10",
    )
    .env({"STARDAG_TARGET_ROOT__DEFAULT": "/data/root-default"})
    .add_local_python_source("stardag")
)


task_adapter = TypeAdapter(sd.TaskParam[sd.Task])
tasks_adapter = TypeAdapter(list[sd.TaskParam[sd.Task]])


@global_app.cls()
class _TaskRun:
    @modal.method()
    def run(self, task_json: str):
        task = task_adapter.validate_json(task_json)
        print(f"Running task: '{task_json}'")
        task.run()
        print(f"Task '{task_json}' completed")
        print(f"task.output().path: {task.output().path}")


def run(task_json: str):
    task = task_adapter.validate_json(task_json)
    print(f"Running task: '{task_json}'")
    task.run()
    print(f"Task '{task_json}' completed")
    print(f"task.output().path: {task.output().path}")


@global_app.function(image=image, volumes={"/data": vol})
def runner_1(task_json: str):
    res = run(task_json)
    vol.commit()
    return res


@global_app.function(image=image, volumes={"/data": vol})
def runner_2(task_json: str):
    res = run(task_json)
    vol.commit()
    return res


@global_app.function(image=image, volumes={"/data": vol})
def build(task_json: str, family_to_runner: dict[str, str]):
    task = task_adapter.validate_json(task_json)
    print(f"Building task: '{task_json}'")
    sd.build(task, task_runner=ModalTaskRunner(family_to_runner=family_to_runner))
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
    build.remote(
        task_json=task_adapter.dump_json(root).decode("utf-8"),
        family_to_runner={"Range": "runner_1", "Sum": "runner_2"},
    )


if __name__ == "__main__":
    root = get_sum(integers=get_range(limit=10))
