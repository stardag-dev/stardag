import logging
import pathlib
import typing

import modal
from modal.gpu import GPU_T

from stardag import BaseTask, build
from stardag._task import TaskStruct
from stardag.build import TaskExecutorABC
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


WorkerSelector = typing.Callable[[BaseTask], str]


def _default_worker_selector(task: BaseTask) -> str:
    return "default"


class ModalTaskExecutor(TaskExecutorABC):
    """Task executor that sends tasks to Modal for remote execution.

    This executor submits tasks to Modal worker functions. Use with
    RoutedTaskExecutor to route some tasks to Modal and others locally.

    Example:
        from stardag.build import HybridConcurrentTaskExecutor, RoutedTaskExecutor

        modal_executor = ModalTaskExecutor(
            modal_app_name="my-app",
            worker_selector=lambda task: "gpu" if needs_gpu(task) else "default",
        )
        local_executor = HybridConcurrentTaskExecutor()

        routed = RoutedTaskExecutor(
            executors={"modal": modal_executor, "local": local_executor},
            router=lambda task: "modal" if run_on_modal(task) else "local",
        )
        build([task], task_executor=routed)
    """

    def __init__(
        self,
        *,
        modal_app_name: str,
        worker_selector: WorkerSelector,
    ):
        """Initialize Modal executor.

        Args:
            modal_app_name: Name of the Modal app with worker functions.
            worker_selector: Function that selects which Modal worker to use per task.
        """
        self.modal_app_name = modal_app_name
        self.worker_selector = worker_selector

    async def submit(self, task: BaseTask) -> None | TaskStruct | Exception:
        """Execute task on Modal."""
        try:
            await self._reload_volumes()
            worker_name = self.worker_selector(task)
            worker_function = modal.Function.from_name(
                app_name=self.modal_app_name,
                name=f"worker_{worker_name}",
            )
            if worker_function is None:
                return ValueError(f"Worker function '{worker_name}' not found")

            res = await worker_function.remote.aio(task)
            return res
        except Exception as e:
            return e

    async def setup(self) -> None:
        """No setup needed for Modal executor."""
        pass

    async def teardown(self) -> None:
        """No teardown needed for Modal executor."""
        pass

    async def _reload_volumes(self):
        modal_config = modal_config_provider.get()
        for volume_name in modal_config.volume_name_to_mount_path.keys():
            vol = modal.Volume.from_name(volume_name, create_if_missing=True)
            await vol.reload.aio()


# Backwards compatibility alias
ModalRunWrapper = ModalTaskExecutor


def _build(
    task: BaseTask,
    worker_selector: WorkerSelector,
    modal_app_name: str,
):
    _setup_logging()
    modal_executor = ModalTaskExecutor(
        modal_app_name=modal_app_name,
        worker_selector=worker_selector,
    )
    logger.info(f"Building root task: {repr(task)}")
    build([task], task_executor=modal_executor)
    logger.info(f"Completed building root task {repr(task)}")


async def _prefect_build(
    task: BaseTask,
    worker_selector: WorkerSelector,
    modal_app_name: str,
    on_complete_callback: typing.Callable[[BaseTask], typing.Awaitable[None]]
    | None = None,
    before_run_callback: typing.Callable[[BaseTask], typing.Awaitable[None]]
    | None = None,
):
    if (
        prefect_build_flow is None
        or create_markdown is None
        or upload_task_on_complete_artifacts is None
    ):
        raise ImportError("Prefect is not installed")

    _setup_logging()
    task_executor = ModalTaskExecutor(
        modal_app_name=modal_app_name,
        worker_selector=worker_selector,
    )
    logger.info(f"Building root task: {repr(task)}")
    await prefect_build_flow.with_options(
        name=f"stardag-build-{task.get_namespace()}:{task.get_name()}"
    )(
        task,
        task_executor=task_executor,
        before_run_callback=(
            before_run_callback or create_markdown
        ),  # TODO default to None
        on_complete_callback=(
            on_complete_callback or upload_task_on_complete_artifacts
        ),
    )
    logger.info(f"Completed building root task {repr(task)}")


def _run(task: BaseTask):
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

    def build_remote(
        self, task: BaseTask, worker_selector: WorkerSelector | None = None
    ):
        return self.modal_app.registered_functions["build"].remote(
            task=task,
            worker_selector=worker_selector or self.worker_selector,
            modal_app_name=self.name,
        )

    def build_spawn(
        self, task: BaseTask, worker_selector: WorkerSelector | None = None
    ):
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


class WorkerSelectorByName:
    def __init__(self, name_to_worker: dict[str, str], default_worker: str):
        self.name_to_worker = name_to_worker
        self.default_worker = default_worker

    def __call__(self, task: BaseTask) -> str:
        return self.name_to_worker.get(task.get_name(), self.default_worker)

    def __repr__(self):
        return (
            f"{self.__class__.__name__}({self.name_to_worker}, {self.default_worker})"
        )


# Backwards compatibility aliases
WorkerSelectorByTypeName = WorkerSelectorByName
WorkerSelectorByFamily = WorkerSelectorByName
