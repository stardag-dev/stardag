import typing

import modal

from stardag import Task, build, task_type_adapter

if typing.TYPE_CHECKING:
    from stardag.integration.modal import ModalTaskRunner


def modal_build_worker(task_json: str, task_runner: "ModalTaskRunner"):
    task = task_type_adapter.validate_json(task_json)
    print(f"Building task: '{task_json}'")
    build(task, task_runner=task_runner)
    print(f"Completed building task '{task_json}'")


def modal_build_entrypoint(task: Task, build_function: modal.Function):
    build_function.remote(
        task_json=task_type_adapter.dump_json(task).decode("utf-8"),
    )
