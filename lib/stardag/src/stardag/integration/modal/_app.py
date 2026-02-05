"""Stardag Modal integration - App and executor for running tasks on Modal.

This module provides:
- StardagApp: A wrapper around modal.App that manages builder and worker functions
- ModalTaskExecutor: A task executor that sends tasks to Modal for remote execution
- Helper functions for profile-based environment configuration

Example usage:

    import modal
    from stardag.integration.modal import StardagApp, FunctionSettings

    # Define your image (user has full control)
    image = (
        modal.Image.debian_slim()
        .pip_install("pandas", "numpy", "stardag")
        .add_local_python_source("my_code")  # Local sources last for caching
    )

    # Create the app (functions are NOT created yet)
    stardag_app = StardagApp(
        "my-app",
        builder_settings=FunctionSettings(image=image),
        worker_settings={"default": FunctionSettings(image=image)},
    )

    # Deploy with: stardag modal deploy my_app.py --profile prod
    # The --profile flag injects environment variables as a Modal secret

    # To run tasks remotely (after deployment):
    from my_tasks import my_task
    stardag_app.build_spawn(my_task)  # Looks up deployed function by name
"""

from __future__ import annotations

import json
import logging
import pathlib
import typing

import modal
from modal.gpu import GPU_T

from stardag import BaseTask, TaskStruct, build
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


# --- Configuration helpers ---


def get_profile_env_vars(profile: str | None = None) -> dict[str, str]:
    """Get environment variables from a stardag profile for Modal deployment.

    These environment variables configure the stardag SDK inside Modal containers
    to connect to the correct registry, workspace, and environment.

    Args:
        profile: Profile name to use. If None, uses the active profile
            (from STARDAG_PROFILE env var or default profile in config).

    Returns:
        Dict of environment variables to inject into Modal functions:
        - STARDAG_REGISTRY_URL: API endpoint
        - STARDAG_WORKSPACE_ID: Workspace UUID
        - STARDAG_ENVIRONMENT_ID: Environment UUID
        - STARDAG_TARGET_ROOTS: JSON dict of target roots (pydantic-settings parses this)
        - COMMIT_HASH: Current git commit (for traceability)

    Example:
        >>> env_vars = get_profile_env_vars("production")
        >>> print(env_vars)
        {
            'STARDAG_REGISTRY_URL': 'https://api.stardag.com',
            'STARDAG_WORKSPACE_ID': '...',
            'STARDAG_ENVIRONMENT_ID': '...',
            'STARDAG_TARGET_ROOTS': '{"default": "s3://bucket/prefix"}',
            'COMMIT_HASH': 'abc123...'
        }
    """
    import os

    from stardag.config import clear_config_cache, config_provider, load_config
    from stardag.registry._base import get_git_commit_hash

    # Load config for specific profile if provided
    if profile:
        # Temporarily set STARDAG_PROFILE to load that profile's config
        old_profile = os.environ.get("STARDAG_PROFILE")
        os.environ["STARDAG_PROFILE"] = profile
        try:
            clear_config_cache()
            config = load_config()
        finally:
            if old_profile is not None:
                os.environ["STARDAG_PROFILE"] = old_profile
            else:
                os.environ.pop("STARDAG_PROFILE", None)
            clear_config_cache()
    else:
        config = config_provider.get()

    env_vars: dict[str, str] = {}

    if config.api.url:
        env_vars["STARDAG_REGISTRY_URL"] = config.api.url
    if config.context.workspace_id:
        env_vars["STARDAG_WORKSPACE_ID"] = config.context.workspace_id
    if config.context.environment_id:
        env_vars["STARDAG_ENVIRONMENT_ID"] = config.context.environment_id

    # Add target roots as JSON (pydantic-settings parses JSON for nested fields)
    if config.target.roots:
        env_vars["STARDAG_TARGET_ROOTS"] = json.dumps(config.target.roots)

    # Add git commit for traceability
    commit_hash = get_git_commit_hash()
    if commit_hash:
        env_vars["COMMIT_HASH"] = commit_hash

    return env_vars


def get_profile_secret(profile: str | None = None) -> modal.Secret:
    """Create a Modal secret from a stardag profile's environment variables.

    This is the recommended way to inject profile configuration into Modal
    functions at runtime, rather than baking them into the image.

    Args:
        profile: Profile name to use. If None, uses the active profile.

    Returns:
        A modal.Secret that can be passed to FunctionSettings.secrets.

    Example:
        >>> secret = get_profile_secret("production")
        >>> stardag_app = StardagApp(
        ...     "my-app",
        ...     builder_settings=FunctionSettings(
        ...         image=my_image,
        ...         secrets=[secret],  # Injected at runtime
        ...     ),
        ...     ...
        ... )
    """
    env_vars = get_profile_env_vars(profile)
    return modal.Secret.from_dict(typing.cast(dict[str, str | None], env_vars))


# --- Function settings ---


class FunctionSettings(typing.TypedDict, total=False):
    """Settings for Modal function configuration.

    These settings are passed to modal.App.function() when creating
    builder and worker functions.

    Attributes:
        image: Required. The Modal image to use for the function.
        gpu: GPU configuration (e.g., "A10G", "T4", or list for fallback).
        cpu: CPU cores (float or (min, max) tuple).
        memory: Memory in MB (int or (min, max) tuple).
        timeout: Function timeout in seconds.
        volumes: Dict of mount path to Volume or CloudBucketMount.
        secrets: List of Modal secrets to inject.
    """

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


# --- Worker selector ---


WorkerSelector = typing.Callable[[BaseTask], str]
"""Type for functions that select which worker to use for a task."""


def _default_worker_selector(task: BaseTask) -> str:
    """Default worker selector - always returns 'default'."""
    return "default"


# --- Task executor ---


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


# --- Internal build/run functions ---


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


# --- Stardag App ---


BuilderType = typing.Literal["basic", "prefect"]


class StardagApp:
    """Wrapper around modal.App for Stardag task execution.

    StardagApp manages the Modal app and its functions for building and
    running Stardag tasks. It supports deferred function creation to allow
    runtime configuration (e.g., profile-based environment variables).

    Lifecycle:
        1. Create StardagApp with settings (functions NOT created yet)
        2. Call finalize() to create the Modal functions (done by CLI on deploy)
        3. After deployment, use build_spawn/build_remote to execute tasks

    Example:
        import modal
        from stardag.integration.modal import StardagApp, FunctionSettings

        # User defines their image with full control
        image = (
            modal.Image.debian_slim()
            .pip_install("stardag", "pandas")
            .add_local_python_source("my_code")
        )

        # Create app (deferred - no functions created yet)
        stardag_app = StardagApp(
            "my-app",
            builder_settings=FunctionSettings(image=image),
            worker_settings={"default": FunctionSettings(image=image)},
        )

        # Deploy with CLI (finalize is called automatically):
        # $ stardag modal deploy my_app.py --profile production

        # After deployment, run tasks:
        stardag_app.build_spawn(my_task)

    Attributes:
        modal_app: The underlying modal.App instance.
        name: The app name.
        is_finalized: Whether finalize() has been called.
    """

    def __init__(
        self,
        modal_app_or_name: modal.App | str,
        *,
        builder_type: BuilderType = "basic",
        builder_settings: FunctionSettings,
        worker_settings: dict[str, FunctionSettings],
        worker_selector: WorkerSelector | None = None,
    ):
        """Initialize a StardagApp.

        Args:
            modal_app_or_name: Either a modal.App instance or a string name.
                If a string, a new modal.App will be created with that name.
            builder_type: Type of builder to use ("basic" or "prefect").
            builder_settings: Settings for the builder function.
            worker_settings: Dict of worker name to settings. Must include "default".
            worker_selector: Function to select worker for each task.
                Defaults to always returning "default".
        """
        if isinstance(modal_app_or_name, str):
            self.modal_app = modal.App(name=modal_app_or_name)
        else:
            assert isinstance(modal_app_or_name, modal.App)
            assert modal_app_or_name.name is not None
            self.modal_app = modal_app_or_name

        self.worker_selector = worker_selector or _default_worker_selector
        self.builder_type = builder_type
        self._builder_settings = builder_settings
        self._worker_settings = worker_settings
        self._is_finalized = False

    @property
    def is_finalized(self) -> bool:
        """Whether the app has been finalized (functions created)."""
        return self._is_finalized

    @property
    def name(self) -> str:
        """The Modal app name."""
        assert self.modal_app.name is not None
        return self.modal_app.name

    def finalize(
        self,
        *,
        extra_secrets: list[modal.Secret] | None = None,
    ) -> None:
        """Finalize the app by creating Modal functions.

        This method creates the builder and worker functions on the Modal app.
        It should be called before deployment, typically by the CLI.

        Args:
            extra_secrets: Additional secrets to inject into all functions.
                This is where profile-based environment variables are injected.

        Raises:
            RuntimeError: If finalize() has already been called.
        """
        if self._is_finalized:
            raise RuntimeError("StardagApp has already been finalized")

        extra_secrets = extra_secrets or []

        # Merge extra secrets into builder settings
        builder_settings: dict[str, typing.Any] = dict(self._builder_settings)
        existing_secrets: list[modal.Secret] = list(
            builder_settings.get("secrets") or []
        )
        builder_settings["secrets"] = existing_secrets + extra_secrets

        # Create builder function
        build_function = _prefect_build if self.builder_type == "prefect" else _build
        self.modal_app.function(
            **{
                **builder_settings,
                "name": "build",
                "serialized": True,
            }
        )(build_function)

        # Create worker functions
        for worker_name, settings in self._worker_settings.items():
            worker_settings: dict[str, typing.Any] = dict(settings)
            existing_worker_secrets: list[modal.Secret] = list(
                worker_settings.get("secrets") or []
            )
            worker_settings["secrets"] = existing_worker_secrets + extra_secrets

            self.modal_app.function(
                **{
                    **worker_settings,
                    "name": f"worker_{worker_name}",
                    "serialized": True,
                }
            )(_run)

        self._is_finalized = True

    def build_spawn(
        self, task: BaseTask, worker_selector: WorkerSelector | None = None
    ):
        """Spawn a build job on a deployed Modal app (non-blocking).

        This method looks up the deployed "build" function by name and spawns
        a new execution. Use this for fire-and-forget builds.

        Args:
            task: The root task to build.
            worker_selector: Optional override for worker selection.

        Returns:
            A Modal FunctionCall handle for the spawned build.

        Example:
            handle = stardag_app.build_spawn(my_task)
            # Build is running in the background
            # Optionally wait for result:
            result = handle.get()
        """
        build_function = modal.Function.from_name(
            app_name=self.name,
            name="build",
        )
        return build_function.spawn(
            task=task,
            worker_selector=worker_selector or self.worker_selector,
            modal_app_name=self.name,
        )

    def build_remote(
        self, task: BaseTask, worker_selector: WorkerSelector | None = None
    ):
        """Run a build on a deployed Modal app (blocking).

        This method looks up the deployed "build" function by name and runs
        it synchronously. Use this when you need to wait for the build result.

        Args:
            task: The root task to build.
            worker_selector: Optional override for worker selection.

        Returns:
            The result of the build.

        Example:
            result = stardag_app.build_remote(my_task)
            print(f"Build completed: {result}")
        """
        build_function = modal.Function.from_name(
            app_name=self.name,
            name="build",
        )
        return build_function.remote(
            task=task,
            worker_selector=worker_selector or self.worker_selector,
            modal_app_name=self.name,
        )

    def local_entrypoint(self, *args, **kwargs):
        """Create a local entrypoint on the underlying Modal app.

        This is a passthrough to modal.App.local_entrypoint().
        """
        return self.modal_app.local_entrypoint(*args, **kwargs)


class WorkerSelectorByName:
    """Worker selector that routes tasks based on task name.

    Example:
        selector = WorkerSelectorByName(
            name_to_worker={"heavy_task": "gpu", "io_task": "high_memory"},
            default_worker="default",
        )
        stardag_app = StardagApp(..., worker_selector=selector)
    """

    def __init__(self, name_to_worker: dict[str, str], default_worker: str):
        """Initialize the selector.

        Args:
            name_to_worker: Dict mapping task names to worker names.
            default_worker: Worker name to use for tasks not in the mapping.
        """
        self.name_to_worker = name_to_worker
        self.default_worker = default_worker

    def __call__(self, task: BaseTask) -> str:
        return self.name_to_worker.get(task.get_name(), self.default_worker)

    def __repr__(self):
        return (
            f"{self.__class__.__name__}({self.name_to_worker}, {self.default_worker})"
        )
