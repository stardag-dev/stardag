"""Simple test tasks for Modal integration tests.

These tasks are defined inside the stardag package (not in tests/) so they
can be deserialized in Modal containers that install stardag from local source.
"""

import asyncio

import stardag as sd


@sd.task
def make_range(limit: int) -> list[int]:
    """Return list(range(limit))."""
    return list(range(limit))


@sd.task
def sum_list(values: sd.Depends[list[int]]) -> int:
    """Return sum of input list."""
    return sum(values)


# --- Class-based tasks exercising Runner.run() branches ---


class AsyncDoubleTask(sd.Task[int]):
    """Async-only task: only implements ``run_aio``.

    Exercises ``Runner.run()``'s async-only branch
    (``asyncio.run(task.run_aio())``).
    """

    input_task: sd.TaskLoads[int]

    def requires(self):
        return self.input_task

    async def run_aio(self) -> None:
        val = await self.input_task.load_aio()
        await asyncio.sleep(0.01)
        await self._save_aio(val * 2)


class SyncDynamicRangeSumTask(sd.Task[int]):
    """Sync generator dynamic deps: yields a task, then reads it.

    Exercises ``Runner.run()``'s sync-generator path via
    ``_drive_sync_generator``. Generators cannot be pickled across the Modal
    boundary, so the first invocation returns the yielded dep as a
    ``TaskStruct``; the task is re-invoked after that dep is built.
    """

    limit: int

    def run(self):
        range_task = make_range(limit=self.limit)
        yield range_task
        values = range_task.load()
        self._save(sum(values))


class AsyncDynamicRangeSumTask(sd.Task[int]):
    """Async generator dynamic deps: yields a task, then reads it.

    Exercises ``Runner.run()``'s async-generator path via
    ``_drive_async_generator``.
    """

    limit: int

    async def run_aio(self):  # type: ignore[override]
        range_task = make_range(limit=self.limit)
        yield range_task
        values = await range_task.load_aio()
        await self._save_aio(sum(values))


class SleepTask(sd.Task[int]):
    """Long-sleeping sync task — used to verify Modal cancel propagation.

    The build engine cancels the asyncio future awaiting
    ``worker_function.remote.aio``. Modal translates that cancellation
    into a remote-call cancel; if it didn't, this task would block for
    ``seconds`` and the test would time out.
    """

    seconds: int = 60

    def run(self) -> None:
        import time

        time.sleep(self.seconds)
        self._save(self.seconds)
