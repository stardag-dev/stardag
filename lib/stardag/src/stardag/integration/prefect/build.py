import asyncio
import logging
from uuid import UUID

from prefect import flow
from prefect import task as prefect_task
from prefect.artifacts import create_markdown_artifact
from prefect.futures import PrefectConcurrentFuture

from stardag._task import BaseTask, TaskRef, flatten_task_struct
from stardag.build.registry import RegistryABC, registry_provider
from stardag.build.task_runner import AsyncRunCallback, AsyncTaskRunner
from stardag.integration.prefect.utils import format_key

logger = logging.getLogger(__name__)


@flow
async def build_flow(task: BaseTask, **kwargs):
    """A flow that builds any stardag Task.

    NOTE that since task is a Pydantic model, if is serialized correctly as JSON by
    prefect. This means that if this flow is deployed to Prefect Cloud, the json
    representation of any task can be submitted to the flow via the UI.
    """
    return await build(task, **kwargs)


async def build(
    task: BaseTask,
    *,
    task_runner: AsyncTaskRunner | None = None,
    # TODO clean up duplicate arg options
    before_run_callback: AsyncRunCallback | None = None,
    on_complete_callback: AsyncRunCallback | None = None,
    wait_for_completion: bool = True,
    registry: RegistryABC | None = None,
) -> dict[str, PrefectConcurrentFuture]:
    task_runner = task_runner or AsyncTaskRunner(
        before_run_callback=before_run_callback,
        on_complete_callback=on_complete_callback,
        registry=registry or registry_provider.get(),
    )
    task_id_to_future = {}
    task_id_to_dynamic_future = {}
    task_id_to_dynamic_deps = {}
    res = await build_dag_recursive(
        task,
        task_runner=task_runner,
        task_id_to_future=task_id_to_future,
        task_id_to_dynamic_future=task_id_to_dynamic_future,
        task_id_to_dynamic_deps=task_id_to_dynamic_deps,
        visited=set([]),
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
        # TODO avoid duplicate task_id references
        task_id, dynamic_deps = result
        # TODO check for exceptions
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
            task_runner=task_runner,
            task_id_to_future=task_id_to_future,
            task_id_to_dynamic_future=task_id_to_dynamic_future,
            task_id_to_dynamic_deps=task_id_to_dynamic_deps,
            visited=set([]),
        )
    if wait_for_completion:
        for future in task_id_to_future.values():
            future.wait()

    return task_id_to_future


async def build_dag_recursive(
    task: BaseTask,
    *,
    task_runner: AsyncTaskRunner,
    task_id_to_future: dict[UUID, PrefectConcurrentFuture | None],
    task_id_to_dynamic_future: dict[
        UUID, PrefectConcurrentFuture
    ],  # dynamic tasks tentative run
    task_id_to_dynamic_deps: dict[
        UUID, tuple[list[BaseTask], PrefectConcurrentFuture]
    ],  # dynamic tasks' dependencies
    visited: set[UUID],  # check for cyclic dependencies
) -> (
    PrefectConcurrentFuture | None
):  # None means could not be scheduled yet, because has dynamic dep task as upstream
    """Translates a stardag task-DAG into Prefect flow logic."""
    # print(f"\nBuilding task {task.id}")
    # pprint(task_id_to_future)
    # pprint(task_id_to_dynamic_future)
    # pprint(task_id_to_dynamic_deps)
    # print()

    # Cycle detection
    if task.id in visited:
        raise ValueError("Cyclic dependencies detected")

    # Hitting a built "branch"
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
            task_runner=task_runner,
            task_id_to_future=task_id_to_future,
            task_id_to_dynamic_future=task_id_to_dynamic_future,
            task_id_to_dynamic_deps=task_id_to_dynamic_deps,
            visited=visited | {task.id},
        )
        for dep in upstream_tasks
    ]
    logger.debug(f"Upstream build results: {upstream_build_results}")
    if any([res is None for res in upstream_build_results]):
        # Task with dynamic deps upstream
        return None

    if task.has_dynamic_deps():

        @prefect_task(name=f"{task_ref.slug}-dynamic")
        async def stardag_dynamic_task():
            # TODO: concurrency lock
            if not task.complete():
                try:
                    gen = await task_runner.run(task)
                    assert gen is not None

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

                except StopIteration:
                    logger.debug("Task completed")
                    return task.id, None

        extra_deps = [prev_dynamic_future] if prev_dynamic_future is not None else []
        future = stardag_dynamic_task.submit(  # type: ignore
            wait_for=upstream_build_results + extra_deps
        )
        task_id_to_dynamic_future[task.id] = future

        # signal that this task has dynamic deps, we can't build downstream tasks yet
        return None

    @prefect_task(
        name=task_ref.slug,
        # TODO caching. Make sure to include environment (stardag root) in cache key
        # cache_key_fn=lambda *args, **kwargs: task.id
    )
    async def stardag_task():
        # TODO: concurrency lock
        if not task.complete():
            res = await task_runner.run(task)
            # check if it's a generator
            if res is not None:
                raise AssertionError(
                    "Tasks with dynamic deps should be executed separately."
                )

        return task.id

    future = stardag_task.submit(wait_for=upstream_build_results)  # type: ignore
    task_id_to_future[task.id] = future

    return future


async def _completed_prefect_future(
    key, future: PrefectConcurrentFuture, timeout: float | None = None
):
    future.wait(timeout=timeout)
    return key, future


async def create_markdown(task: BaseTask):
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


async def upload_task_on_complete_artifacts(task):
    """Upload artifacts to Prefect Cloud for tasks that implement the special method."""
    if hasattr(task, "prefect_on_complete_artifacts"):
        for artifact in task.prefect_on_complete_artifacts():
            await artifact.create()
