import pathlib
import typing

import modal
from modal.gpu import GPU_T

from stardag import Task, build
from stardag.build.task_runner import TaskRunner
from stardag.integration.modal._config import modal_config_provider


class FunctionSettings(typing.TypedDict, total=False):
    image: typing.Required[modal.Image]
    gpu: GPU_T | list[GPU_T]
    cpu: float | tuple[float, float]
    memory: int | tuple[int, int]
    timeout: int
    volumes: dict[
        typing.Union[str, pathlib.PurePosixPath],
        typing.Union[modal.Volume, modal.CloudBucketMount],
    ]
    # TODO add the rest of the function settings


WorkerSelector = typing.Callable[[Task], str]

default_image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install(
        "pydantic>=2.8.2",
        "pydantic-settings>=2.7.1",
        "uuid6>=2024.7.10",
    )
    # .env({"STARDAG_TARGET_ROOT__DEFAULT": "/data/root-default"})
    .add_local_python_source("stardag")
)


def _default_worker_selector(task: Task) -> str:
    return "worker_default"


class ModalTaskRunner(TaskRunner):
    def __init__(
        self,
        *,
        modal_app_name: str,
        worker_selector: WorkerSelector,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.worker_selector = worker_selector
        self.modal_app_name = modal_app_name

    def _run_task(self, task):
        self._reload_volumes()
        worker_name = self.worker_selector(task)
        worker_function = modal.Function.from_name(
            app_name=self.modal_app_name,
            name=worker_name,
        )
        if worker_function is None:
            raise ValueError(f"Worker function '{worker_name}' not found")

        # return worker_function.remote(task_type_adapter.dump_json(task).decode("utf-8"))
        try:
            res = worker_function.remote(task)
        except Exception as e:
            print(f"Error running task '{task}': {e}")
            raise
            # modal.interact()
            # import IPython

            # IPython.embed()

        return res

    def _reload_volumes(self):
        modal_config = modal_config_provider.get()
        for volume_name in modal_config.volume_name_to_mount_path.keys():
            vol = modal.Volume.from_name(volume_name, create_if_missing=True)
            vol.reload()


def _build(
    task: Task,
    worker_selector: WorkerSelector,
    modal_app_name: str,
):
    task_runner = ModalTaskRunner(
        modal_app_name=modal_app_name,
        worker_selector=worker_selector,
    )
    print(f"Building task: '{task}'")
    build(task, task_runner=task_runner)
    print(f"Completed building task '{task}'")


def _run(task: Task):
    print(f"Running task: '{task}'")
    task.run()
    print(f"Task '{task}' completed")
    print(f"task.output().path: {task.output().path}")


class StardagApp:
    def __init__(
        self,
        modal_app_or_name: modal.App | str,
        *,
        builder_settings: FunctionSettings | None = None,
        worker_settings: dict[str, FunctionSettings] | None = None,
        worker_selector: WorkerSelector | None = None,
    ):
        if isinstance(modal_app_or_name, str):
            self.modal_app = modal.App(name=modal_app_or_name)
        else:
            assert isinstance(modal_app_or_name, modal.App)
            assert modal_app_or_name.name is not None
            self.modal_app = modal_app_or_name

        self.worker_selector = worker_selector or _default_worker_selector

        self.task_runner = ModalTaskRunner(
            modal_app_name=self.name,
            worker_selector=self.worker_selector,
        )

        self.modal_app.function(
            **{
                **(builder_settings or {"image": default_image}),
                **{"name": "build", "serialized": True},
            }
        )(_build)

        worker_settings = worker_settings or {
            "worker_default": {"image": default_image}
        }
        for worker_name, settings in worker_settings.items():
            self.modal_app.function(
                **{
                    **settings,
                    **{"name": worker_name, "serialized": True},
                }
            )(_run)

    def build_remote(self, task: Task):
        return self.modal_app.registered_functions["build"].remote(
            task=task,
            worker_selector=self.worker_selector,
            modal_app_name=self.name,
        )

    def build_spawn(self, task: Task):
        build_function = modal.Function.from_name(
            app_name=self.name,
            name="build",
        )
        return build_function.spawn(
            task=task,
            worker_selector=self.worker_selector,
            modal_app_name=self.modal_app.name,
        )

    def local_entrypoint(self, *args, **kwargs):
        return self.modal_app.local_entrypoint(*args, **kwargs)

    @property
    def name(self) -> str:
        assert self.modal_app.name is not None
        return self.modal_app.name
