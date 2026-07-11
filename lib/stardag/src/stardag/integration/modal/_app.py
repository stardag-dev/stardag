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

import asyncio
import inspect
import json
import logging
import os
import pathlib
import traceback as tb_module
import typing
from uuid import UUID

import modal

from stardag import BaseTask, TaskStruct, build, flatten_task_struct
from stardag._core.base_task import _has_custom_run, _has_custom_run_aio
from stardag.build import (
    BuildExitStatus,
    DetachedHandle,
    TaskExecutionError,
    TaskExecutorABC,
)
from stardag.build._base import BuildSummary
from stardag.config import clear_config_cache, config_provider, load_config
from stardag.integration.modal._target import (
    MODAL_VOLUME_URI_PREFIX,
    get_default_volume_mount_path,
    get_volume_name_and_path,
)
from stardag.registry._base import (
    NoOpRegistry,
    get_git_commit_hash,
    registry_provider,
)
from stardag.utils.env import temp_env_vars

try:
    from stardag.integration.prefect import (
        build_flow as prefect_build_flow,
    )
    from stardag.integration.prefect import (
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
        - STARDAG_API_URL: API endpoint
        - STARDAG_WORKSPACE_ID: Workspace UUID
        - STARDAG_ENVIRONMENT_ID: Environment UUID
        - STARDAG_TARGET_ROOTS: JSON dict of target roots (pydantic-settings parses this)
        - COMMIT_HASH: Current git commit (for traceability)

    Example:
        >>> env_vars = get_profile_env_vars("production")
        >>> print(env_vars)
        {
            'STARDAG_API_URL': 'https://api.stardag.com',
            'STARDAG_WORKSPACE_ID': '...',
            'STARDAG_ENVIRONMENT_ID': '...',
            'STARDAG_TARGET_ROOTS': '{"default": "s3://bucket/prefix"}',
            'COMMIT_HASH': 'abc123...'
        }
    """
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

    reg = config.registry
    if reg:
        env_vars["STARDAG_API_URL"] = reg.url
        if reg.workspace_id:
            env_vars["STARDAG_WORKSPACE_ID"] = reg.workspace_id
        if reg.environment_id:
            env_vars["STARDAG_ENVIRONMENT_ID"] = reg.environment_id

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


class TargetRootsVolumes(typing.NamedTuple):
    """Result of get_target_roots_volumes().

    Attributes:
        by_root_key: Dict of target root key to Modal Volume instance.
        by_volume_name: Dict of volume name to Modal Volume instance (deduped).
    """

    by_root_key: dict[str, modal.Volume]
    by_volume_name: dict[str, modal.Volume]


def get_target_roots_volumes(
    target_roots: dict[str, str] | None = None,
    create_if_missing: bool = True,
) -> TargetRootsVolumes:
    """Get Modal volumes for configured target roots.

    Scans target roots for ``modalvol://`` URIs and returns the corresponding
    Modal Volume objects, both keyed by target root name and deduped by
    volume name.

    Args:
        target_roots: Dict of target root key to URI or None (default from config).
        create_if_missing: Whether to create the Modal volume if it doesn't exist.
            When True, volumes are eagerly hydrated to trigger creation.
            When False, volumes are lazy references (hydrated by Modal at deploy time).

    Returns:
        TargetRootsVolumes with volumes keyed by root key and by volume name.
    """
    if target_roots is None:
        config = config_provider.get()
        target_roots = config.target.roots

    by_root_key: dict[str, modal.Volume] = {}
    by_volume_name: dict[str, modal.Volume] = {}
    for key, uri in target_roots.items():
        if not uri.startswith(MODAL_VOLUME_URI_PREFIX):
            continue
        volume_name, _ = get_volume_name_and_path(uri)
        if volume_name not in by_volume_name:
            vol = modal.Volume.from_name(
                volume_name, create_if_missing=create_if_missing
            )
            if create_if_missing:
                vol.hydrate()
            by_volume_name[volume_name] = vol
        by_root_key[key] = by_volume_name[volume_name]

    return TargetRootsVolumes(by_root_key=by_root_key, by_volume_name=by_volume_name)


class FinalizeResult(typing.NamedTuple):
    """Result of StardagApp.finalize().

    Attributes:
        volumes: Dict of target root key to Modal Volume instance.
        functions: List of created Modal function names.
        volume_mounts: Dict of mount_path -> volume_name for auto-mounted volumes.
        auto_volumes: Dict of mount_path -> Volume for auto-mounted volumes.
    """

    volumes: dict[str, modal.Volume]
    functions: list[str]
    volume_mounts: dict[str, str] = {}
    auto_volumes: dict[str, modal.Volume] = {}


# --- Function settings ---


class BuildTriggerResult(typing.NamedTuple):
    """Result of :meth:`StardagApp.build_trigger`.

    Attributes:
        build_id: The registry build id minted (or reused) at the trigger
            point. Pass it back to ``build_trigger(..., build_id=...)`` to
            re-attach/resume the same build.
        function_call: The Modal ``FunctionCall`` handle for the spawned
            build function invocation. Call ``.get()`` to block on the
            result if needed.
    """

    build_id: UUID
    function_call: typing.Any


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
        concurrency_limit: Max number of concurrent containers.
        allow_concurrent_inputs: Max concurrent inputs per container.
        container_idle_timeout: Seconds before idle container shuts down.
        keep_warm: Number of containers to keep warm.
        ephemeral_disk: Ephemeral disk size in MB.
        retries: Number of retries on failure.
    """

    image: typing.Required[modal.Image]
    gpu: str | list[str]
    cpu: float | tuple[float, float]
    memory: int | tuple[int, int]
    timeout: int
    volumes: dict[
        typing.Union[str, pathlib.PurePosixPath],
        typing.Union[modal.Volume, modal.CloudBucketMount],
    ]
    secrets: list[modal.Secret]
    concurrency_limit: int
    allow_concurrent_inputs: int
    container_idle_timeout: int
    keep_warm: int
    ephemeral_disk: int
    retries: int


# --- Worker selector ---


WorkerSelection = typing.Union[str, tuple[str, dict[str, str]]]
"""Return type of a :data:`WorkerSelector`.

Either a worker name (``str``), or a ``(worker_name, env_overrides)`` tuple
where ``env_overrides`` is a dict of environment variables to set temporarily
around the task's ``run`` call inside the worker container (e.g. to tune
task-specific execution knobs such as worker/thread counts or batch sizes).
See :meth:`Runner.__call__`.
"""

WorkerSelector = typing.Callable[[BaseTask], WorkerSelection]
"""Type for functions that select which worker to use for a task.

A selector returns either a worker name, or a ``(worker_name, env_overrides)``
tuple (see :data:`WorkerSelection`).
"""


def _normalize_worker_selection(
    selection: WorkerSelection,
) -> tuple[str, dict[str, str] | None]:
    """Split a :data:`WorkerSelection` into ``(worker_name, env_overrides)``.

    Accepts either a bare worker name or a ``(worker_name, env_overrides)``
    tuple and always returns the two-tuple form (``env_overrides`` is ``None``
    when the selector returned a bare name).
    """
    if isinstance(selection, tuple):
        worker_name, env_overrides = selection
        return worker_name, env_overrides
    return selection, None


def _default_worker_selector(task: BaseTask) -> WorkerSelection:
    """Default worker selector - always returns 'default'."""
    return "default"


def _callable_accepts_env_overrides(fn: typing.Callable[..., typing.Any]) -> bool:
    """Whether ``fn`` accepts an ``env_overrides`` argument.

    Used to stay backward-compatible with custom ``RunFunction`` implementations
    written against the older ``(task)``-only signature (before the optional
    ``env_overrides`` parameter was added).
    """
    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError):
        return False
    for param in signature.parameters.values():
        if param.kind is inspect.Parameter.VAR_KEYWORD:
            return True
        if param.name == "env_overrides":
            return True
    return False


# --- Task executor ---


MODAL_EXECUTOR_NAME = "modal"
"""Executor name recorded with detached executions (see DetachedHandle)."""


class ModalTaskExecutor(TaskExecutorABC):
    """Task executor that sends tasks to Modal for remote execution.

    This executor submits tasks to Modal worker functions. Use with
    RoutedTaskExecutor to route some tasks to Modal and others locally.

    By default tasks are executed *detached* (``worker.spawn`` + a tracked
    ``FunctionCall``): the worker invocation survives this process, its
    function call id is recorded in the registry, and a resumed build
    re-attaches to still-running workers instead of re-executing them.
    Pass ``detached=False`` for the legacy blocking ``worker.remote`` mode.

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
        detached: bool = True,
    ):
        """Initialize Modal executor.

        Args:
            modal_app_name: Name of the Modal app with worker functions.
            worker_selector: Function that selects which Modal worker to use per task.
            detached: Execute tasks as detached spawned function calls
                (restart-safe, re-attachable, explicitly cancellable).
                False restores the legacy blocking ``remote`` calls.
        """
        self.modal_app_name = modal_app_name
        self.worker_selector = worker_selector
        self.detached = detached
        # Cache of worker name -> modal.Function. ``modal.Function.from_name``
        # returns a lazy handle (no network call until invoked), but it is
        # invoked on every ``submit`` so we memoize it per worker name to avoid
        # recreating the handle for every task.
        self._worker_functions: dict[str, modal.Function] = {}
        # In-flight detached executions by task UUID, for explicit cancel().
        # Asyncio cancellation of ``FunctionCall.get`` does NOT stop the
        # remote call (unlike ``remote.aio``), so FAIL_FAST relies on this.
        self._in_flight: dict[UUID, modal.FunctionCall] = {}

    def _get_worker_function(self, worker_name: str) -> modal.Function:
        """Return the (memoized) ``modal.Function`` handle for a worker."""
        worker_function = self._worker_functions.get(worker_name)
        if worker_function is None:
            worker_function = modal.Function.from_name(
                app_name=self.modal_app_name,
                name=f"worker_{worker_name}",
            )
            self._worker_functions[worker_name] = worker_function
        return worker_function

    async def submit(self, task: BaseTask) -> None | TaskStruct | TaskExecutionError:
        """Execute task on Modal (blocking remote call)."""
        try:
            worker_name, env_overrides = _normalize_worker_selection(
                self.worker_selector(task)
            )
            worker_function = self._get_worker_function(worker_name)
            res = await worker_function.remote.aio(task, env_overrides=env_overrides)
            return res
        except Exception as e:
            return TaskExecutionError(
                exception=e,
                traceback="".join(tb_module.format_exception(e)),
            )

    # --- Detached execution (spawn + re-attach + cancel) ---

    def supports_detached(self, task: BaseTask) -> bool:
        """Detached mode is per-executor (constructor flag), not per-task."""
        return self.detached

    def _make_handle(
        self, task: BaseTask, function_call: modal.FunctionCall
    ) -> DetachedHandle:
        """Wrap a FunctionCall in a DetachedHandle tracking in-flight state."""
        self._in_flight[task.id] = function_call

        async def wait() -> None | TaskStruct | TaskExecutionError:
            try:
                return await function_call.get.aio()
            except Exception as e:
                return TaskExecutionError(
                    exception=e,
                    traceback="".join(tb_module.format_exception(e)),
                )
            finally:
                self._in_flight.pop(task.id, None)

        return DetachedHandle(
            executor=MODAL_EXECUTOR_NAME,
            ref=function_call.object_id,
            wait=wait,
        )

    async def submit_detached(self, task: BaseTask) -> DetachedHandle:
        """Spawn the task on its worker function; return a re-attachable handle."""
        worker_name, env_overrides = _normalize_worker_selection(
            self.worker_selector(task)
        )
        worker_function = self._get_worker_function(worker_name)
        function_call = await worker_function.spawn.aio(
            task, env_overrides=env_overrides
        )
        return self._make_handle(task, function_call)

    async def reattach(
        self, task: BaseTask, executor: str, ref: str
    ) -> DetachedHandle | None:
        """Re-attach to a spawned function call by id, if it is still live.

        Returns None (→ normal re-execution) when the ref belongs to a
        different backend, the call failed/was cancelled, or the result has
        expired. A call that already finished successfully yields a handle
        resolving immediately to its result.
        """
        if executor != MODAL_EXECUTOR_NAME or not self.detached:
            return None
        try:
            function_call = modal.FunctionCall.from_id(ref)
        except Exception:
            return None
        try:
            result = await function_call.get.aio(timeout=0)
        except TimeoutError:
            # Still running (or queued) — the interesting case.
            return self._make_handle(task, function_call)
        except Exception:
            # Failed, cancelled, expired result, or unknown id — re-execute.
            return None

        async def resolved() -> None | TaskStruct | TaskExecutionError:
            return result

        return DetachedHandle(executor=MODAL_EXECUTOR_NAME, ref=ref, wait=resolved)

    async def cancel(self, task: BaseTask) -> None:
        """Cancel the tracked in-flight function call for ``task``, if any."""
        function_call = self._in_flight.pop(task.id, None)
        if function_call is None:
            return
        try:
            await function_call.cancel.aio()
        except Exception as e:
            logger.warning(
                f"Failed to cancel Modal function call "
                f"{function_call.object_id} for task {task.id}: {e}"
            )

    async def setup(self) -> None:
        """No setup needed for Modal executor."""
        pass

    async def teardown(self) -> None:
        """No teardown needed for Modal executor."""
        pass


# --- Build and run function protocols ---


class BuildFailedError(Exception):
    """Raised when a build completes with failures or pending tasks."""

    pass


class BuildFunction(typing.Protocol):
    """Protocol for the function registered as the Modal "build" function.

    This function is called remotely on Modal to orchestrate a DAG build.
    It receives one or more root tasks, a worker selector, the Modal app
    name, and an optional ``build_kwargs`` dict, then coordinates task
    execution across Modal worker functions.

    Args (of ``__call__``):
        tasks: A single root ``BaseTask`` or a sequence of root tasks.
        worker_selector: Function picking a worker name per task.
        app_name: Name of the Modal app hosting the worker functions.
        build_kwargs: Optional dict of extra kwargs forwarded to the
            underlying build engine (the default ``Builder`` splats them
            into :func:`stardag.build`). ``None`` means "no extra kwargs".

    The default implementation (``Builder``) creates a ``ModalTaskExecutor``
    and calls ``stardag.build()``. Custom implementations can subclass
    ``Builder`` to override ``setup()``/``teardown()``/``build()``, or
    implement this protocol directly for full control.

    Any module-level code in the module where a custom build function is
    defined will execute inside the Modal container before the function is
    called — use this for container-level setup (imports, config, etc.).
    """

    def __call__(
        self,
        tasks: typing.Sequence[BaseTask] | BaseTask,
        worker_selector: WorkerSelector,
        app_name: str,
        build_kwargs: dict[str, typing.Any] | None = None,
    ) -> BuildSummary | None: ...


class RunFunction(typing.Protocol):
    """Protocol for the function registered as Modal "worker_*" functions.

    This function is called remotely on Modal to execute a single task.
    It receives a task instance and returns either ``None`` (task completed)
    or the ``TaskStruct`` yielded at the first incomplete dynamic-deps yield
    (which may include deps that are already complete — the caller is
    expected to filter if that matters). This enables idempotent
    re-execution: the build system schedules the yielded deps, then
    re-invokes the task. On re-execution the generator advances past
    previously-yielded batches whose deps are now complete.

    The default implementation (``Runner``) handles sync, async, and dynamic
    deps tasks. Custom implementations can subclass ``Runner`` to override
    ``setup()``/``teardown()``/``run()``, or implement this protocol directly.

    Any module-level code in the module where a custom run function is
    defined will execute inside the Modal container before the function is
    called — use this for container-level setup (imports, config, etc.).

    Args (of ``__call__``):
        task: The task instance to execute.

    Implementations *may* additionally accept an optional
    ``env_overrides: dict[str, str] | None`` keyword argument (the framework
    always forwards it by keyword). When the ``worker_selector`` returns
    ``(worker_name, env_overrides)`` (see :data:`WorkerSelection`), the
    framework forwards those overrides to run functions that accept the
    parameter; for run functions written against the older ``(task)``-only
    signature the framework instead applies the overrides to the process
    environment around the call. The default :class:`Runner` accepts
    ``env_overrides`` and applies them around its ``run`` call.
    """

    def __call__(self, task: BaseTask) -> None | TaskStruct: ...


class _RunFunctionWithEnv(typing.Protocol):
    """Internal protocol for run functions that accept ``env_overrides``.

    Used to type the call site that forwards selector-provided environment
    overrides (see :class:`StardagApp.finalize`'s ``_modal_run`` wrapper).
    """

    def __call__(
        self, task: BaseTask, *, env_overrides: dict[str, str] | None = None
    ) -> None | TaskStruct: ...


# --- Default build/run implementations, with overridable methods ---


class Builder(BuildFunction):
    """Default builder implementation with overridable setup/teardown.

    Override ``setup()``/``teardown()``/``build()`` to customize behavior.
    Pass an instance to ``StardagApp(build_function=MyBuilder())``.

    Example:

    .. code-block:: python

        class MyBuilder(Builder):
            def setup(self, tasks):
                super().setup(tasks)
                configure_my_environment()

        stardag_app = StardagApp(
            "my-app",
            build_function=MyBuilder(),
            ...
        )
    """

    def __init__(self, *, detached: bool = True):
        """Initialize the builder.

        Args:
            detached: Execute tasks as detached spawned Modal function calls
                (restart-safe, re-attachable on resume, explicitly
                cancellable). False restores the legacy blocking
                ``remote`` calls. Passed to :class:`ModalTaskExecutor`.
        """
        self.detached = detached

    def setup(self, tasks: typing.Sequence[BaseTask] | BaseTask) -> None:
        """Optional setup logic before the build starts."""
        _setup_logging()

    def teardown(
        self,
        tasks: typing.Sequence[BaseTask] | BaseTask,
        summary_or_exception: BuildSummary | None | Exception,
    ) -> None:
        """Optional teardown logic after the build finishes."""
        if isinstance(summary_or_exception, BuildSummary):
            summary = summary_or_exception
            logger.info(f"Build summary:\n{repr(summary)}")
            if summary.status != BuildExitStatus.SUCCESS:
                raise BuildFailedError(
                    f"Build finished with status {summary.status.value}: "
                    f"{summary.task_count.failed} failed, "
                    f"{summary.task_count.pending} pending"
                )
        elif summary_or_exception is None:
            logger.info("Build completed without a BuildSummary.")
        else:
            logger.error(f"Build exception:\n{repr(summary_or_exception)}")

    def __call__(
        self,
        tasks: typing.Sequence[BaseTask] | BaseTask,
        worker_selector: WorkerSelector,
        app_name: str,
        build_kwargs: dict[str, typing.Any] | None = None,
    ) -> BuildSummary | None:
        """Core build logic to orchestrate the DAG build."""
        modal_executor = ModalTaskExecutor(
            modal_app_name=app_name,
            worker_selector=worker_selector,
            detached=getattr(self, "detached", True),
        )
        summary_or_exception: BuildSummary | None | Exception = BuildFailedError(
            "Unknown error during build"
        )  # Placeholder for type checking, this should never be raised

        try:
            self.setup(tasks)
            summary = self.build(tasks, modal_executor, build_kwargs=build_kwargs)
            summary_or_exception = summary
            return summary
        except Exception as exception:
            summary_or_exception = exception
            raise
        finally:
            self.teardown(tasks, summary_or_exception)

    def build(
        self,
        tasks: typing.Sequence[BaseTask] | BaseTask,
        task_executor: ModalTaskExecutor,
        build_kwargs: dict[str, typing.Any] | None = None,
    ) -> BuildSummary | None:
        """Default build logic using stardag.build() with the ModalTaskExecutor.

        ``build_kwargs`` are forwarded to :func:`stardag.build` (e.g. ``fail_mode``,
        ``register_all``, ``global_lock_config``). Conflicting keys (``tasks``,
        ``task_executor``) are reserved and rejected.
        """
        kwargs = dict(build_kwargs or {})
        for reserved in ("tasks", "task_executor"):
            if reserved in kwargs:
                raise TypeError(
                    f"build_kwargs must not contain reserved key '{reserved}'"
                )
        return build(tasks, task_executor=task_executor, **kwargs)


class Runner(RunFunction):
    """Default runner implementation with overridable setup/teardown.

    Override ``setup()``/``teardown()``/``run()`` to customize behavior.
    Pass an instance to ``StardagApp(run_function=MyRunner())``.

    Example:

    .. code-block:: python

        class MyRunner(Runner):
            def setup(self, task):
                super().setup(task)
                torch.cuda.set_device(0)

        stardag_app = StardagApp(
            "my-app",
            run_function=MyRunner(),
            ...
        )
    """

    def setup(self, task: BaseTask) -> None:
        """Optional setup logic before the task runs."""
        _setup_logging()

    def teardown(self, task: BaseTask, exception: Exception | None) -> None:
        """Optional teardown logic after the task runs."""
        if exception:
            logger.error(f"Task {repr(task)} raised an exception: {repr(exception)}")

    def __call__(
        self, task: BaseTask, *, env_overrides: dict[str, str] | None = None
    ) -> None | TaskStruct:
        """Core logic to execute a single task.

        Returns ``None`` when the task completed, or a ``TaskStruct`` of
        dynamic dependencies that were not yet complete (idempotent
        re-execution pattern — see ``run``).

        Args:
            task: The task instance to execute.
            env_overrides: Optional environment variable overrides (selected by
                the ``worker_selector`` — see :data:`WorkerSelection`). When
                provided, they are set temporarily around the ``run`` call and
                the previous environment is restored afterwards.
        """
        result: None | TaskStruct = None
        exception: Exception | None = None
        try:
            self.setup(task)
            with temp_env_vars(env_overrides or {}):
                result = self.run(task)
        except Exception as e:
            exception = e
            raise
        finally:
            self.teardown(task, exception)
        return result

    def run(self, task: BaseTask) -> None | TaskStruct:
        """Default run logic — handles sync, async, and dynamic deps tasks.

        Dispatch policy:

        - **Async-only** (``run_aio`` defined, ``run`` not overridden):
          async generator ``run_aio`` is driven via ``_drive_async_generator``;
          otherwise ``asyncio.run(task.run_aio())``.
        - **Sync-only and dual** (``run`` defined, with or without ``run_aio``):
          ``task.run()`` is called. If it returns a sync generator it is
          driven via ``_drive_sync_generator``. Dual tasks intentionally
          prefer the sync path here because the Modal worker invocation is
          itself synchronous — if you need async execution for a dual task,
          implement it in ``run()`` (e.g. via ``asyncio.run`` internally).

        Generators cannot be serialized across the Modal boundary, so we
        mirror ``_run_task_in_process``: drive forward while yielded batches
        are fully complete, and at the first yield with any incomplete dep
        return the entire yielded ``TaskStruct``. The ``ModalTaskExecutor``
        builds those deps (filtering for incomplete ones) and re-invokes this
        function — on re-execution the generator advances past the
        now-complete batch.
        """
        has_run_aio = _has_custom_run_aio(task)
        has_run = _has_custom_run(task)

        if has_run_aio and not has_run:
            # Async-only task
            if inspect.isasyncgenfunction(type(task).run_aio):
                return asyncio.run(_drive_async_generator(task))
            asyncio.run(task.run_aio())
            return None

        # Sync (or dual) task — run and drive generator if returned.
        # Dual tasks deliberately take the sync path; see method docstring.
        return _drive_sync_generator(task.run())


def _drive_sync_generator(
    result: None | typing.Generator[TaskStruct, None, None] | TaskStruct,
) -> None | TaskStruct:
    """Drive a sync generator result for idempotent re-execution.

    Advances the generator past yield batches whose deps are all complete.
    Stops at the first yield with any incomplete dep and returns that yield's
    full ``TaskStruct`` (which may also include already-complete deps — the
    caller is expected to filter for incomplete ones when scheduling). If
    the generator completes, returns ``None``.

    If ``result`` is ``None`` (no dynamic deps) returns ``None``. If
    ``result`` is already a ``TaskStruct`` (unusual but possible when a
    user's ``run()`` returns deps directly) it is returned as-is.
    """
    if result is None:
        return None
    if not hasattr(result, "__next__"):
        # Already a TaskStruct (unusual, but handle it)
        return typing.cast(TaskStruct, result)

    gen = typing.cast(typing.Generator[TaskStruct, None, None], result)
    try:
        while True:
            yielded = next(gen)
            deps = flatten_task_struct(yielded)
            incomplete = [dep for dep in deps if not dep.complete()]
            if incomplete:
                return tuple(deps)
    except StopIteration:
        return None


async def _drive_async_generator(task: BaseTask) -> None | TaskStruct:
    """Drive an async generator ``run_aio`` for idempotent re-execution.

    Same contract as ``_drive_sync_generator``: advances past fully-complete
    yield batches and returns the first batch that contains any incomplete
    dep as a ``TaskStruct`` (may include already-complete deps). Returns
    ``None`` when the generator finishes.
    """
    agen = typing.cast(
        typing.AsyncGenerator[TaskStruct, None],
        task.run_aio(),  # type: ignore[assignment]
    )
    try:
        async for yielded in agen:
            deps = flatten_task_struct(yielded)
            incomplete = [dep for dep in deps if not dep.complete()]
            if incomplete:
                return tuple(deps)
    except StopAsyncIteration:
        pass
    return None


_default_build = Builder()
_default_run = Runner()


class PrefectBuilder(Builder):
    """Builder that uses Prefect for build orchestration.

    Requires the ``stardag.integration.prefect`` package to be installed.
    """

    def __init__(
        self,
        on_complete_callback: typing.Callable[[BaseTask], typing.Awaitable[None]]
        | None = None,
        before_run_callback: typing.Callable[[BaseTask], typing.Awaitable[None]]
        | None = None,
    ):
        # Prefect's build flow drives the executor via submit() only, so
        # detached mode has no effect there — keep the legacy behavior.
        super().__init__(detached=False)
        self.on_complete_callback = on_complete_callback
        self.before_run_callback = before_run_callback

    def build(
        self,
        tasks: typing.Sequence[BaseTask] | BaseTask,
        task_executor: ModalTaskExecutor,
        build_kwargs: dict[str, typing.Any] | None = None,
    ) -> BuildSummary | None:
        if prefect_build_flow is None:
            raise ImportError("Prefect is not installed")

        import asyncio

        _flow = prefect_build_flow  # local for pyright narrowing

        # TODO: support multiple root tasks in PrefectBuilder
        if isinstance(tasks, BaseTask):
            task = tasks
        else:
            if len(tasks) != 1:
                raise ValueError(
                    f"PrefectBuilder currently supports only a single root task, "
                    f"got {len(tasks)}"
                )
            task = tasks[0]

        flow_kwargs = dict(build_kwargs or {})
        # ``task`` is reserved because the flow is invoked as
        # ``_flow(...)(task, ...)`` below — letting the user pass another
        # ``task`` via build_kwargs would surface as a confusing
        # "got multiple values for argument 'task'" TypeError.
        for reserved in (
            "task",
            "task_executor",
            "before_run_callback",
            "on_complete_callback",
        ):
            if reserved in flow_kwargs:
                raise TypeError(
                    f"build_kwargs must not contain reserved key '{reserved}'"
                )

        async def _run():
            await _flow.with_options(
                name=f"stardag-build-{task.get_namespace()}:{task.get_name()}"
            )(
                task,
                task_executor=task_executor,
                before_run_callback=(self.before_run_callback or create_markdown),
                on_complete_callback=(
                    self.on_complete_callback or upload_task_on_complete_artifacts
                ),
                **flow_kwargs,
            )

        asyncio.run(_run())
        # Prefect manages its own flow result; no BuildSummary available.
        return None


def _setup_logging():
    """Setup logging for the modal app."""
    logging.basicConfig(level=logging.INFO)


# --- Stardag App ---


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
        build_function: BuildFunction = _default_build,
        run_function: RunFunction = _default_run,
        builder_settings: FunctionSettings,
        worker_settings: dict[str, FunctionSettings],
        worker_selector: WorkerSelector | None = None,
    ):
        """Initialize a StardagApp.

        Args:
            modal_app_or_name: Either a modal.App instance or a string name.
                If a string, a new modal.App will be created with that name.
            build_function: Callable registered as the Modal "build" function.
                Must match the ``BuildFunction`` protocol:
                ``(tasks, worker_selector, app_name) -> BuildSummary | None``.
                Defaults to ``Builder()`` which provides overridable
                ``setup()``/``teardown()``/``build()`` hooks. Subclass
                ``Builder`` for customization, or implement the protocol
                directly.

                Any module-level code in the module where this callable is
                defined will run inside the Modal container at import time —
                use this for container-level setup.
            run_function: Callable registered as the Modal worker functions.
                Must match the ``RunFunction`` protocol:
                ``(task) -> None``.
                Defaults to ``Runner()`` which provides overridable
                ``setup()``/``teardown()`` hooks.

                Any module-level code in the module where this callable is
                defined will run inside the Modal container at import time —
                use this for worker-level setup (GPU init, library preloading).
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
        self._build_function = build_function
        self._run_function = run_function
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

    @staticmethod
    def _prepare_function_settings(
        settings: FunctionSettings,
        *,
        extra_secrets: list[modal.Secret],
        auto_volumes: dict[str, modal.Volume],
    ) -> dict[str, typing.Any]:
        """Merge extra secrets and auto-volumes into function settings.

        Auto-mounted volumes are merged with user volumes, where user-specified
        volumes at the same mount path take precedence over auto-mounted ones.
        """
        result: dict[str, typing.Any] = dict(settings)

        # Merge secrets: existing + extra
        existing_secrets: list[modal.Secret] = list(result.get("secrets") or [])
        result["secrets"] = existing_secrets + extra_secrets

        # Merge volumes: auto-mounted (lower priority) + user (higher priority)
        user_volumes = dict(result.get("volumes") or {})
        result["volumes"] = {**auto_volumes, **user_volumes}

        return result

    def finalize(
        self,
        *,
        extra_secrets: list[modal.Secret] | None = None,
        create_volumes_if_missing: bool = True,
    ) -> FinalizeResult:
        """Finalize the app by creating Modal functions.

        This method creates the builder and worker functions on the Modal app.
        It should be called before deployment, typically by the CLI.

        Discovered Modal volumes from target roots are automatically mounted at
        /mnt/stardag-volumes/<volume-name> and the STARDAG_MODAL_VOLUME_MOUNTS
        env var is set so that ModalMountedVolumeFileTarget (local I/O) is used
        instead of ModalVolumeRemoteFileSystem (API-based).

        Args:
            extra_secrets: Additional secrets to inject into all functions.
                This is where profile-based environment variables are injected.
            create_volumes_if_missing: Whether to create Modal volumes for
                target roots if they don't exist.

        Returns:
            FinalizeResult with created volumes, function names, and mount info.

        Raises:
            RuntimeError: If finalize() has already been called.
        """
        if self._is_finalized:
            raise RuntimeError("StardagApp has already been finalized")

        # Discover and create Modal volumes from target roots
        target_roots_volumes = get_target_roots_volumes(
            create_if_missing=create_volumes_if_missing
        )

        # Compute auto-mount mapping from discovered volumes
        # volume_mounts: mount_path -> volume_name (for env var)
        # auto_volumes: mount_path -> Volume (for Modal function)
        volume_mounts: dict[str, str] = {}
        auto_volumes: dict[str, modal.Volume] = {}
        for vol_name, vol in target_roots_volumes.by_volume_name.items():
            mount_path = str(get_default_volume_mount_path(vol_name))
            volume_mounts[mount_path] = vol_name
            auto_volumes[mount_path] = vol

        extra_secrets = list(extra_secrets or [])

        # Inject volume mount config as env var so ModalMountedVolumeFileTarget is used
        if volume_mounts:
            extra_secrets.append(
                modal.Secret.from_dict(
                    {"STARDAG_MODAL_VOLUME_MOUNTS": json.dumps(volume_mounts)}
                )
            )
        # Wrap callables in real functions for Modal compatibility.
        # Modal's is_async() only accepts inspect.isfunction()-compatible objects,
        # not callable class instances. The wrappers delegate to the actual callable
        # and are what get serialized/sent to Modal.
        build_fn = self._build_function

        def _modal_build(
            tasks: typing.Sequence[BaseTask] | BaseTask,
            worker_selector: WorkerSelector,
            app_name: str,
            build_kwargs: dict[str, typing.Any] | None = None,
        ) -> BuildSummary | None:
            return build_fn(tasks, worker_selector, app_name, build_kwargs=build_kwargs)

        run_fn = self._run_function
        # The ``RunFunction`` protocol gained an optional ``env_overrides``
        # parameter. Older custom run functions implemented the protocol with a
        # bare ``(task)`` signature, so only forward ``env_overrides`` to those
        # that accept it; otherwise apply the overrides in the wrapper.
        run_fn_accepts_env = _callable_accepts_env_overrides(run_fn)

        def _modal_run(
            task: BaseTask, *, env_overrides: dict[str, str] | None = None
        ) -> typing.Any:
            if run_fn_accepts_env:
                run_fn_with_env = typing.cast(_RunFunctionWithEnv, run_fn)
                return run_fn_with_env(task, env_overrides=env_overrides)
            with temp_env_vars(env_overrides or {}):
                return run_fn(task)

        # Create builder function
        builder_settings = self._prepare_function_settings(
            self._builder_settings,
            extra_secrets=extra_secrets,
            auto_volumes=auto_volumes,
        )
        self.modal_app.function(
            **{
                **builder_settings,
                "name": "build",
                "serialized": True,
            }
        )(_modal_build)

        function_names = ["build"]

        # Create worker functions
        for worker_name, settings in self._worker_settings.items():
            worker_settings = self._prepare_function_settings(
                settings,
                extra_secrets=extra_secrets,
                auto_volumes=auto_volumes,
            )

            func_name = f"worker_{worker_name}"
            self.modal_app.function(
                **{
                    **worker_settings,
                    "name": func_name,
                    "serialized": True,
                }
            )(_modal_run)
            function_names.append(func_name)

        self._is_finalized = True

        return FinalizeResult(
            volumes=target_roots_volumes.by_root_key,
            functions=function_names,
            volume_mounts=volume_mounts,
            auto_volumes=auto_volumes,
        )

    def build_spawn(
        self,
        tasks: typing.Sequence[BaseTask] | BaseTask,
        worker_selector: WorkerSelector | None = None,
        *,
        build_kwargs: dict[str, typing.Any] | None = None,
    ):
        """Spawn a build job on a deployed Modal app (non-blocking).

        This method looks up the deployed "build" function by name and spawns
        a new execution. Use this for fire-and-forget builds.

        Args:
            tasks: A single root task or a sequence of root tasks to build.
            worker_selector: Optional override for worker selection.
            build_kwargs: Optional kwargs forwarded to the remote build function
                (e.g. ``{"fail_mode": FailMode.CONTINUE}``). The
                default ``Builder`` passes these to :func:`stardag.build`.

        Returns:
            A Modal FunctionCall handle for the spawned build.

        Example:
            handle = stardag_app.build_spawn(my_task)
            # Or multiple roots:
            handle = stardag_app.build_spawn([task_a, task_b])
            # Optionally wait for result:
            result = handle.get()
        """
        build_function = modal.Function.from_name(
            app_name=self.name,
            name="build",
        )
        return build_function.spawn(
            tasks=tasks,
            worker_selector=worker_selector or self.worker_selector,
            app_name=self.name,
            build_kwargs=build_kwargs,
        )

    def build_trigger(
        self,
        tasks: typing.Sequence[BaseTask] | BaseTask,
        worker_selector: WorkerSelector | None = None,
        *,
        build_kwargs: dict[str, typing.Any] | None = None,
        build_id: UUID | None = None,
        description: str | None = None,
    ) -> BuildTriggerResult:
        """Trigger a build with a registry build id minted at the trigger point.

        Unlike :meth:`build_spawn` — where the build id is created *inside*
        the Modal build container — this method first creates (or reuses) the
        build in the registry from the calling process, then spawns the
        deployed build function with ``resume_build_id`` set to it. As a
        result:

        - Any restart of the build function (Modal retry after preemption, a
          manual re-trigger with the returned ``build_id``) **resumes the
          same build** instead of creating a new one: already-completed task
          targets are detected during discovery and skipped.
        - The build appears in the registry immediately, before the Modal
          container has started.

        Set ``retries`` in the app's ``builder_settings`` to let Modal
        automatically re-run (and thereby resume) the build function after
        infrastructure failures.

        Requires registry credentials in the calling process (the active
        stardag profile), in addition to Modal credentials. If no registry is
        configured, use :meth:`build_spawn` instead.

        Args:
            tasks: A single root task or a sequence of root tasks to build.
            worker_selector: Optional override for worker selection.
            build_kwargs: Optional kwargs forwarded to the remote build
                function (must not contain ``resume_build_id``; it is set by
                this method).
            build_id: Existing build id to re-attach to (e.g. from a previous
                ``build_trigger`` call). If None, a new build is created.
            description: Optional description for the new build (ignored when
                ``build_id`` is given).

        Returns:
            BuildTriggerResult with the ``build_id`` and the spawned Modal
            ``FunctionCall`` handle.
        """
        merged_kwargs = dict(build_kwargs or {})
        if "resume_build_id" in merged_kwargs:
            raise TypeError(
                "build_kwargs must not contain 'resume_build_id'; pass "
                "build_id=... to build_trigger instead"
            )

        if build_id is None:
            registry = registry_provider.get()
            if isinstance(registry, NoOpRegistry):
                raise RuntimeError(
                    "build_trigger requires a configured registry to mint the "
                    "build id at the trigger point (run 'stardag auth login' "
                    "or configure an API key). Use build_spawn to trigger a "
                    "build without local registry credentials."
                )
            task_list = [tasks] if isinstance(tasks, BaseTask) else list(tasks)
            build_id = registry.build_start(
                root_tasks=task_list, description=description
            )
        merged_kwargs["resume_build_id"] = build_id

        build_function = modal.Function.from_name(
            app_name=self.name,
            name="build",
        )
        function_call = build_function.spawn(
            tasks=tasks,
            worker_selector=worker_selector or self.worker_selector,
            app_name=self.name,
            build_kwargs=merged_kwargs,
        )
        return BuildTriggerResult(build_id=build_id, function_call=function_call)

    def build_remote(
        self,
        tasks: typing.Sequence[BaseTask] | BaseTask,
        worker_selector: WorkerSelector | None = None,
        *,
        build_kwargs: dict[str, typing.Any] | None = None,
    ):
        """Run a build on a deployed Modal app (blocking).

        This method looks up the deployed "build" function by name and runs
        it synchronously. Use this when you need to wait for the build result.

        Args:
            tasks: A single root task or a sequence of root tasks to build.
            worker_selector: Optional override for worker selection.
            build_kwargs: Optional kwargs forwarded to the remote build function
                (e.g. ``{"fail_mode": FailMode.CONTINUE}``). The
                default ``Builder`` passes these to :func:`stardag.build`.

        Returns:
            The result of the build.

        Example:
            result = stardag_app.build_remote(my_task)
            # Or multiple roots:
            result = stardag_app.build_remote([task_a, task_b])
        """
        build_function = modal.Function.from_name(
            app_name=self.name,
            name="build",
        )
        return build_function.remote(
            tasks=tasks,
            worker_selector=worker_selector or self.worker_selector,
            app_name=self.name,
            build_kwargs=build_kwargs,
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
