import typing

from stardag.build.registry import NoOpRegistry, RegistryABC
from stardag.task import Task, TaskDeps

RunCallback = typing.Callable[[Task], None]
AsyncRunCallback = typing.Callable[[Task], typing.Awaitable[None]]


class TaskRunner:
    def __init__(
        self,
        before_run_callback: RunCallback | None = None,
        on_complete_callback: RunCallback | None = None,
        registry: RegistryABC | None = None,
    ):
        self.before_run_callback = before_run_callback
        self.on_complete_callback = on_complete_callback
        self.registry = registry or NoOpRegistry()

    def run(self, task: Task) -> typing.Generator[TaskDeps, None, None] | None:
        if self.before_run_callback is not None:
            self.before_run_callback(task)

        res = task.run()

        # check if res is a generator -> task is not complete
        if hasattr(res, "__next__"):
            res = typing.cast(typing.Generator[TaskDeps, None, None], res)
            return res

        self.registry.register(task)  # TODO add async implementation
        if self.on_complete_callback is not None:
            self.on_complete_callback(task)

        self.registry.register(task)
        if self.on_complete_callback is not None:
            self.on_complete_callback(task)

        return res


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

    async def run(self, task: Task) -> typing.Generator[TaskDeps, None, None] | None:
        if self.before_run_callback is not None:
            await self.before_run_callback(task)

        res = task.run()

        # check if res is a coroutine
        if res is not None and hasattr(res, "__await__"):
            res = await res  # type: ignore

        # check if res is a generator -> task is not complete
        if hasattr(res, "__next__"):
            res = typing.cast(typing.Generator[TaskDeps, None, None], res)
            return res

        self.registry.register(task)  # TODO add async implementation
        if self.on_complete_callback is not None:
            await self.on_complete_callback(task)

        return res
