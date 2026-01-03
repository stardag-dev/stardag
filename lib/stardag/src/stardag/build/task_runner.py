import typing

from stardag._task import BaseTask, TaskStruct
from stardag.build.registry import NoOpRegistry, RegistryABC

RunCallback = typing.Callable[[BaseTask], None]
AsyncRunCallback = typing.Callable[[BaseTask], typing.Awaitable[None]]


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
        # Notify registry that task is starting
        if hasattr(self.registry, "start"):
            self.registry.start(task)  # type: ignore

        if self.before_run_callback is not None:
            self.before_run_callback(task)

        try:
            res = self._run_task(task)

            # check if res is a generator -> task is not complete
            if hasattr(res, "__next__"):
                res = typing.cast(typing.Generator[TaskStruct, None, None], res)
                return res

            # Task completed successfully
            if hasattr(self.registry, "complete"):
                self.registry.complete(task)  # type: ignore
            else:
                self.registry.register(task)

            if self.on_complete_callback is not None:
                self.on_complete_callback(task)

            return res

        except Exception as e:
            # Task failed
            if hasattr(self.registry, "fail"):
                self.registry.fail(task, str(e))  # type: ignore
            raise

    def _run_task(
        self, task: BaseTask
    ) -> typing.Generator[TaskStruct, None, None] | None:
        return task.run()


class AsyncTaskRunner:
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
        # Notify registry that task is starting
        if hasattr(self.registry, "start"):
            self.registry.start(task)  # type: ignore

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
            if hasattr(self.registry, "complete"):
                self.registry.complete(task)  # type: ignore
            else:
                self.registry.register(task)

            if self.on_complete_callback is not None:
                await self.on_complete_callback(task)

            return res

        except Exception as e:
            # Task failed
            if hasattr(self.registry, "fail"):
                self.registry.fail(task, str(e))  # type: ignore
            raise

    async def _run_task(
        self, task: BaseTask
    ) -> typing.Generator[TaskStruct, None, None] | None:
        return task.run()
