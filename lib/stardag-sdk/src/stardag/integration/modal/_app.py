import logging
import pathlib
import typing

import modal
from modal.gpu import GPU_T

from stardag import Task, build
from stardag.build.task_runner import AsyncTaskRunner, TaskRunner
from stardag.integration.modal._config import modal_config_provider

try:
    from stardag.integration.prefect.build import build_flow as prefect_build_flow
    from stardag.integration.prefect.build import (
        create_markdown,
        upload_task_on_complete_artifacts,
    )
except ImportError:
    prefect_build_flow = None
    create_markdown = None
    upload_task_on_complete_artifacts = None

logger = logging.getLogger(__name__)


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
    secrets: list[modal.Secret]
    # TODO add the rest of the function settings


WorkerSelector = typing.Callable[[Task], str]


def _default_worker_selector(task: Task) -> str:
    return "default"


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
            name=f"worker_{worker_name}",
        )
        if worker_function is None:
            raise ValueError(f"Worker function '{worker_name}' not found")

        res = worker_function.remote(task)

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
    _setup_logging()
    task_runner = ModalTaskRunner(
        modal_app_name=modal_app_name,
        worker_selector=worker_selector,
    )
    logger.info(f"Building root task: {repr(task)}")
    build(task, task_runner=task_runner)
    logger.info(f"Completed building root task {repr(task)}")


class ModalAsyncTaskRunner(AsyncTaskRunner):
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

    async def _run_task(self, task):
        await self._reload_volumes()
        worker_name = self.worker_selector(task)
        worker_function = modal.Function.from_name(
            app_name=self.modal_app_name,
            name=f"worker_{worker_name}",
        )
        if worker_function is None:
            raise ValueError(f"Worker function '{worker_name}' not found")

        res = await worker_function.remote.aio(task)

        return res

    async def _reload_volumes(self):
        modal_config = modal_config_provider.get()
        for volume_name in modal_config.volume_name_to_mount_path.keys():
            vol = modal.Volume.from_name(volume_name, create_if_missing=True)
            await vol.reload.aio()


async def _prefect_build(
    task: Task,
    worker_selector: WorkerSelector,
    modal_app_name: str,
    on_complete_callback: typing.Callable[[Task], typing.Awaitable[None]] | None = None,
    before_run_callback: typing.Callable[[Task], typing.Awaitable[None]] | None = None,
):
    if (
        prefect_build_flow is None
        or create_markdown is None
        or upload_task_on_complete_artifacts is None
    ):
        raise ImportError("Prefect is not installed")

    _setup_logging()
    task_runner = ModalAsyncTaskRunner(
        modal_app_name=modal_app_name,
        worker_selector=worker_selector,
        before_run_callback=(
            before_run_callback or create_markdown
        ),  # TODO default to None
        on_complete_callback=(
            on_complete_callback or upload_task_on_complete_artifacts
        ),
    )
    logger.info(f"Building root task: {repr(task)}")
    await prefect_build_flow.with_options(
        name=f"stardag-build-{task.get_namespace_family()}"
    )(task, task_runner=task_runner)
    logger.info(f"Completed building root task {repr(task)}")


def _run(task: Task):
    _setup_logging()
    logger.info(f"Running task: {repr(task)}")
    try:
        task.run()
    except Exception as e:
        logger.exception(f"Error running task: {repr(task)} - {e}")
        raise

    logger.info(f"Completed running task: {repr(task)}")


def _setup_logging():
    """Setup logging for the modal app"""
    logging.basicConfig(level=logging.INFO)


BuilderType = typing.Literal["basic", "prefect"]


class StardagApp:
    def __init__(
        self,
        modal_app_or_name: modal.App | str,
        *,
        builder_type: BuilderType = "basic",
        builder_settings: FunctionSettings,
        worker_settings: dict[str, FunctionSettings],
        worker_selector: WorkerSelector | None = None,
    ):
        if isinstance(modal_app_or_name, str):
            self.modal_app = modal.App(name=modal_app_or_name)
        else:
            assert isinstance(modal_app_or_name, modal.App)
            assert modal_app_or_name.name is not None
            self.modal_app = modal_app_or_name

        self.worker_selector = worker_selector or _default_worker_selector

        build_function = _prefect_build if builder_type == "prefect" else _build

        self.modal_app.function(
            **{
                **builder_settings,
                **{
                    "name": "build",
                    "serialized": True,
                    # "include_source": False,
                },
            }
        )(build_function)

        for worker_name, settings in worker_settings.items():
            self.modal_app.function(
                **{
                    **settings,
                    **{
                        "name": f"worker_{worker_name}",
                        "serialized": True,
                        # "include_source": False,
                    },
                }
            )(_run)

    def build_remote(self, task: Task, worker_selector: WorkerSelector | None = None):
        return self.modal_app.registered_functions["build"].remote(
            task=task,
            worker_selector=worker_selector or self.worker_selector,
            modal_app_name=self.name,
        )

    def build_spawn(self, task: Task, worker_selector: WorkerSelector | None = None):
        build_function = modal.Function.from_name(
            app_name=self.name,
            name="build",
        )
        return build_function.spawn(
            task=task,
            worker_selector=worker_selector or self.worker_selector,
            modal_app_name=self.modal_app.name,
        )

    def local_entrypoint(self, *args, **kwargs):
        return self.modal_app.local_entrypoint(*args, **kwargs)

    @property
    def name(self) -> str:
        assert self.modal_app.name is not None
        return self.modal_app.name


class WorkerSelectorByFamily:
    def __init__(self, family_to_worker: dict[str, str], default_worker: str):
        self.family_to_worker = family_to_worker
        self.default_worker = default_worker

    def __call__(self, task: Task) -> str:
        return self.family_to_worker.get(task.get_family(), self.default_worker)

    def __repr__(self):
        return (
            f"{self.__class__.__name__}({self.family_to_worker}, {self.default_worker})"
        )
