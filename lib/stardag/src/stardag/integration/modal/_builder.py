"""Default ``BuildFunction`` implementations — the resident-orchestrator side.

A builder runs *inside* the app's deployed ``build`` function and drives a
whole DAG to completion in one container, delegating each task to a
:class:`ModalTaskExecutor`. Contrast with reactive scheduling, where no
container is resident and short-lived ticks drive the build instead (see
:mod:`stardag.integration.modal._tick`).
"""

from __future__ import annotations

import asyncio
import logging
import typing

from stardag import BaseTask, build
from stardag.build import BuildExitStatus, BuildSummary
from stardag.integration.modal._executor import ModalTaskExecutor
from stardag.integration.modal._logging import _setup_logging
from stardag.integration.modal._protocols import BuildFunction
from stardag.integration.modal._selector import WorkerSelector

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


class BuildFailedError(Exception):
    """Raised when a build completes with failures or pending tasks."""

    pass


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


_default_build = Builder()
