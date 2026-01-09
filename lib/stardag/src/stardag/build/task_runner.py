import asyncio
import typing

from stardag._task import BaseTask, TaskStruct
from stardag.build.registry import NoOpRegistry, RegistryABC

RunCallback = typing.Callable[[BaseTask], None]
AsyncRunCallback = typing.Callable[[BaseTask], typing.Awaitable[None]]


def _has_custom_run_aio(task: BaseTask) -> bool:
    """Check if task has overridden run_aio() (not using default delegation).

    The default run_aio() in BaseTask just delegates to run(). If a task has
    a custom async implementation, we can call it directly. Otherwise, we need
    to run the sync run() in a thread to avoid blocking the event loop.
    """
    # Check if the task's run_aio method is different from BaseTask's
    return type(task).run_aio is not BaseTask.run_aio


class TaskRunner:
    def __init__(
        self,
        *,
        before_run_callback: RunCallback | None = None,
        on_complete_callback: RunCallback | None = None,
        registry: RegistryABC | None = None,
    ):
        self.before_run_callback = before_run_callback
        self.on_complete_callback = on_complete_callback
        self.registry = registry or NoOpRegistry()

    def run(self, task: BaseTask) -> typing.Generator[TaskStruct, None, None] | None:
        self.registry.start_task(task)

        if self.before_run_callback is not None:
            self.before_run_callback(task)

        try:
            res = self._run_task(task)

            # check if res is a generator -> task is not complete
            if hasattr(res, "__next__"):
                res = typing.cast(typing.Generator[TaskStruct, None, None], res)
                return res

            # Task completed successfully
            self.registry.complete_task(task)

            # Upload registry assets if any
            assets = task.registry_assets()
            if assets:
                self.registry.upload_task_assets(task, assets)

            if self.on_complete_callback is not None:
                self.on_complete_callback(task)

            return res

        except Exception as e:
            # Task failed
            self.registry.fail_task(task, str(e))
            raise

    def _run_task(
        self, task: BaseTask
    ) -> typing.Generator[TaskStruct, None, None] | None:
        return task.run()


class AsyncTaskRunner:
    """Async task runner that uses async registry and task methods.

    This runner fully leverages async/await patterns:
    - Uses registry.*_aio() methods for async HTTP calls
    - Uses task.run_aio() for async task execution (when implemented)
    - Falls back to asyncio.to_thread() for tasks without async implementation
    - Uses task.registry_assets_aio() for async asset generation
    """

    def __init__(
        self,
        before_run_callback: AsyncRunCallback | None = None,
        on_complete_callback: AsyncRunCallback | None = None,
        registry: RegistryABC | None = None,
    ):
        self.before_run_callback = before_run_callback
        self.on_complete_callback = on_complete_callback
        self.registry = registry or NoOpRegistry()

    async def run(
        self, task: BaseTask
    ) -> typing.Generator[TaskStruct, None, None] | None:
        await self.registry.start_task_aio(task)

        if self.before_run_callback is not None:
            await self.before_run_callback(task)

        try:
            res = await self._run_task(task)

            # check if res is a coroutine
            if res is not None and hasattr(res, "__await__"):
                res = await res  # type: ignore

            # check if res is a generator -> task is not complete
            if hasattr(res, "__next__"):
                res = typing.cast(typing.Generator[TaskStruct, None, None], res)
                return res

            # Task completed successfully
            await self.registry.complete_task_aio(task)

            # Upload registry assets if any
            assets = task.registry_assets_aio()
            if assets:
                await self.registry.upload_task_assets_aio(task, assets)

            if self.on_complete_callback is not None:
                await self.on_complete_callback(task)

            return res

        except Exception as e:
            # Task failed
            await self.registry.fail_task_aio(task, str(e))
            raise

    async def _run_task(
        self, task: BaseTask
    ) -> typing.Generator[TaskStruct, None, None] | None:
        """Execute the task, using async if available or threading otherwise.

        Tasks that implement custom run_aio() are called directly.
        Tasks using the default run_aio() (which delegates to sync run()) are
        executed via asyncio.to_thread() to avoid blocking the event loop.
        """
        if _has_custom_run_aio(task):
            # Task has custom async implementation - call directly
            return await task.run_aio()
        else:
            # Task uses default delegation - run sync in thread to avoid blocking
            return await asyncio.to_thread(task.run)
