import modal

from stardag import task_type_adapter
from stardag.build.task_runner import TaskRunner


def modal_run_worker(task_json: str):
    task = task_type_adapter.validate_json(task_json)
    print(f"Running task: '{task_json}'")
    task.run()
    print(f"Task '{task_json}' completed")
    print(f"task.output().path: {task.output().path}")


class ModalTaskRunner(TaskRunner):
    def __init__(
        self,
        *,
        app: modal.App,
        default_runner: modal.Function,
        family_to_runner: dict[str, str] | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.app = app
        self.default_runner = default_runner
        self.family_to_runner = family_to_runner or {}

    def _run_task(self, task):
        runner_name = self.family_to_runner.get(task.get_family())
        if runner_name is not None:
            runner = self.app.registered_functions[runner_name]
        else:
            runner = self.default_runner
        return runner.remote(task_type_adapter.dump_json(task).decode("utf-8"))
