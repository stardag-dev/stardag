"""Prefect integration for building stardag task DAGs.

This module provides functions to build stardag tasks using Prefect for
orchestration. Prefect handles scheduling and dependency management via
its task submission system.
"""

import asyncio
import logging
import typing
from typing import Generator
from uuid import UUID

from prefect import flow
from prefect import task as prefect_task
from prefect.artifacts import create_markdown_artifact
from prefect.futures import PrefectConcurrentFuture

from stardag._task import BaseTask, TaskRef, TaskStruct, flatten_task_struct
from stardag.build import DefaultRunWrapper, RunWrapper
from stardag.integration.prefect.utils import format_key
from stardag.registry import RegistryABC, registry_provider

logger = logging.getLogger(__name__)

# Callback types for prefect build
AsyncRunCallback = typing.Callable[[BaseTask], typing.Awaitable[None]]


class _PrefectTaskExecutor:
    """Internal executor that wraps task execution with registry/callbacks.

    This class handles:
    - Registry calls (start_task, complete_task, etc.)
    - Before/after callbacks
    - Asset uploading
    - Delegation to RunWrapper for actual task execution
    """

    def __init__(
        self,
        run_wrapper: RunWrapper | None = None,
        registry: RegistryABC | None = None,
        before_run_callback: AsyncRunCallback | None = None,
        on_complete_callback: AsyncRunCallback | None = None,
    ):
        self.run_wrapper = run_wrapper or DefaultRunWrapper()
        self.registry = registry or registry_provider.get()
        self.before_run_callback = before_run_callback
        self.on_complete_callback = on_complete_callback

    async def run(
        self, task: BaseTask
    ) -> Generator[TaskStruct, None, None] | TaskStruct | None:
        """Execute task with registry tracking and callbacks.

        Returns:
            - None: Task completed with no dynamic dependencies.
            - Generator: Task has dynamic deps and is suspended (in-process).
            - TaskStruct: Task has dynamic deps but completed (cross-process).
        """
        await self.registry.start_task_aio(task)

        if self.before_run_callback is not None:
            await self.before_run_callback(task)

        try:
            result = await self.run_wrapper.run(task)

            # Check if result is a generator (dynamic deps, in-process)
            if result is not None and hasattr(result, "__next__"):
                # For dynamic deps, we return the generator without completing
                # Cast for type checker
                gen: Generator[TaskStruct, None, None] = result  # type: ignore[assignment]
                return gen

            # Check if result is TaskStruct (dynamic deps from cross-process)
            # In Prefect's case, this means RunWrapper returned deps directly
            if result is not None:
                # Return TaskStruct - caller should handle building these deps
                # and completing the task
                return result

            # Task completed successfully (result is None)
            await self.registry.complete_task_aio(task)

            # Upload registry assets if any
            assets = task.registry_assets_aio()
            if assets:
                await self.registry.upload_task_assets_aio(task, assets)

            if self.on_complete_callback is not None:
                await self.on_complete_callback(task)

            return None

        except Exception as e:
            await self.registry.fail_task_aio(task, str(e))
            raise


@flow
async def build_flow(task: BaseTask, **kwargs):
    """A flow that builds any stardag Task.

    NOTE that since a Task is a Pydantic model, it is serialized correctly as JSON by
    prefect. This means that if this flow is deployed to Prefect Cloud, the json
    representation of any task can be submitted to the flow via the UI.
    """
    return await build(task, **kwargs)


async def build(
    task: BaseTask,
    *,
    run_wrapper: RunWrapper | None = None,
    before_run_callback: AsyncRunCallback | None = None,
    on_complete_callback: AsyncRunCallback | None = None,
    wait_for_completion: bool = True,
    registry: RegistryABC | None = None,
) -> dict[str, PrefectConcurrentFuture]:
    """Build a stardag task DAG using Prefect for orchestration.

    Args:
        task: The root task to build
        run_wrapper: Optional RunWrapper for custom task execution (e.g., Modal)
        before_run_callback: Called before each task runs
        on_complete_callback: Called after each task completes
        wait_for_completion: Whether to wait for all tasks to complete
        registry: Registry for tracking task execution

    Returns:
        Dict mapping task IDs to Prefect futures
    """
    executor = _PrefectTaskExecutor(
        run_wrapper=run_wrapper,
        registry=registry,
        before_run_callback=before_run_callback,
        on_complete_callback=on_complete_callback,
    )

    task_id_to_future: dict[UUID, PrefectConcurrentFuture | None] = {}
    task_id_to_dynamic_future: dict[UUID, PrefectConcurrentFuture] = {}
    task_id_to_dynamic_deps: dict[
        UUID, tuple[list[BaseTask], PrefectConcurrentFuture]
    ] = {}

    res = await build_dag_recursive(
        task,
        executor=executor,
        task_id_to_future=task_id_to_future,
        task_id_to_dynamic_future=task_id_to_dynamic_future,
        task_id_to_dynamic_deps=task_id_to_dynamic_deps,
        visited=set(),
    )

    while res is None:
        # Get next completed dynamic task
        task_id, dynamic_future = await next(
            asyncio.as_completed(
                [
                    _completed_prefect_future(task_id, prefect_future)
                    for task_id, prefect_future in task_id_to_dynamic_future.items()
                ]
            )
        )
        del task_id_to_dynamic_future[task_id]  # important to avoid infinite loop
        result = dynamic_future.result()
        task_id, dynamic_deps = result
        if dynamic_deps is None:
            # task completed
            task_id_to_future[task_id] = dynamic_future
        else:
            prev_dynamic_deps, _ = task_id_to_dynamic_deps.get(task_id, ([], None))
            task_id_to_dynamic_deps[task_id] = (
                prev_dynamic_deps + dynamic_deps,
                dynamic_future,
            )

        res = await build_dag_recursive(
            task,
            executor=executor,
            task_id_to_future=task_id_to_future,
            task_id_to_dynamic_future=task_id_to_dynamic_future,
            task_id_to_dynamic_deps=task_id_to_dynamic_deps,
            visited=set(),
        )

    if wait_for_completion:
        for future in task_id_to_future.values():
            if future is not None:
                future.wait()

    return task_id_to_future  # type: ignore


async def build_dag_recursive(
    task: BaseTask,
    *,
    executor: _PrefectTaskExecutor,
    task_id_to_future: dict[UUID, PrefectConcurrentFuture | None],
    task_id_to_dynamic_future: dict[UUID, PrefectConcurrentFuture],
    task_id_to_dynamic_deps: dict[UUID, tuple[list[BaseTask], PrefectConcurrentFuture]],
    visited: set[UUID],
) -> PrefectConcurrentFuture | None:
    """Recursively build a stardag task DAG into Prefect flow logic.

    Returns None if the task cannot be scheduled yet due to upstream dynamic deps.
    """
    # Cycle detection
    if task.id in visited:
        raise ValueError("Cyclic dependencies detected")

    # Already built
    already_built_future = task_id_to_future.get(task.id, None)
    if already_built_future is not None:
        return already_built_future

    already_built_dynamic_future = task_id_to_dynamic_future.get(task.id, None)
    if already_built_dynamic_future is not None:
        return None

    task_ref = TaskRef.from_task(task)

    # Recurse dependencies
    dynamic_deps, prev_dynamic_future = task_id_to_dynamic_deps.get(task.id, ([], None))
    upstream_tasks = flatten_task_struct(task.requires()) + dynamic_deps
    upstream_build_results = [
        await build_dag_recursive(
            dep,
            executor=executor,
            task_id_to_future=task_id_to_future,
            task_id_to_dynamic_future=task_id_to_dynamic_future,
            task_id_to_dynamic_deps=task_id_to_dynamic_deps,
            visited=visited | {task.id},
        )
        for dep in upstream_tasks
    ]
    logger.debug(f"Upstream build results: {upstream_build_results}")
    if any(res is None for res in upstream_build_results):
        # Task with dynamic deps upstream
        return None

    if task.has_dynamic_deps():

        @prefect_task(name=f"{task_ref.slug}-dynamic")
        async def stardag_dynamic_task():
            if not task.complete():
                try:
                    result = await executor.run(task)
                    if result is None:
                        # Task completed with no dynamic deps
                        logger.debug("Task completed with no dynamic deps")
                        return task.id, None

                    # Check if result is a generator (in-process, can suspend/resume)
                    if hasattr(result, "__next__"):
                        gen: Generator[TaskStruct, None, None] = result  # type: ignore[assignment]
                        requires = next(gen)
                        deps = flatten_task_struct(requires)
                        completed = [dep.complete() for dep in deps]
                        logger.debug(f"Initial deps: {deps}, completed: {completed}")
                        while all(completed):
                            logger.debug("All deps complete")
                            requires = next(gen)
                            deps = flatten_task_struct(requires)
                            completed = [dep.complete() for dep in deps]
                            logger.debug(f"Deps: {deps}, completed: {completed}")

                        return task.id, deps
                    else:
                        # Result is TaskStruct (cross-process, task already completed)
                        # Return deps directly - task execution is already done
                        # Cast for type checker - we've verified it's not a generator
                        task_struct: TaskStruct = result  # type: ignore[assignment]
                        deps = flatten_task_struct(task_struct)
                        logger.debug(f"Cross-process deps: {deps}")
                        return task.id, deps

                except StopIteration:
                    logger.debug("Task completed")
                    return task.id, None
            return task.id, None

        extra_deps = [prev_dynamic_future] if prev_dynamic_future is not None else []
        future = stardag_dynamic_task.submit(  # type: ignore
            wait_for=upstream_build_results + extra_deps
        )
        task_id_to_dynamic_future[task.id] = future
        return None

    @prefect_task(name=task_ref.slug)
    async def stardag_task():
        if not task.complete():
            res = await executor.run(task)
            if res is not None:
                raise AssertionError(
                    "Tasks with dynamic deps should be executed separately."
                )
        return task.id

    future = stardag_task.submit(wait_for=upstream_build_results)  # type: ignore
    task_id_to_future[task.id] = future
    return future


async def _completed_prefect_future(
    key: UUID, future: PrefectConcurrentFuture, timeout: float | None = None
):
    """Wait for a prefect future and return the key with the future."""
    future.wait(timeout=timeout)
    return key, future


async def create_markdown(task: BaseTask):
    """Create a markdown artifact for a task."""
    output = getattr(task, "output", None)
    if output is None:
        output_path = "N/A"
    else:
        output_path = getattr(output, "path", "N/A")

    markdown = f"""# {TaskRef.from_task(task).slug}
**Task id**: `{task.id}`
**Task class**: `{task.__module__}.{task.__class__.__name__}`
**Output path**: [{output_path}]({output_path})
**Task spec**
```dict
{task.model_dump_json(indent=2)}
```
"""

    await create_markdown_artifact(  # type: ignore
        key=format_key(f"{TaskRef.from_task(task).slug}-spec"),
        description=f"Task spec for {task.id}",
        markdown=markdown,
    )


async def upload_task_on_complete_artifacts(task: BaseTask):
    """Upload artifacts to Prefect Cloud for tasks that implement the special method."""
    if hasattr(task, "prefect_on_complete_artifacts"):
        for artifact in task.prefect_on_complete_artifacts():  # type: ignore[attr-defined]
            await artifact.create()
