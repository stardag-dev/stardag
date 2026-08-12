"""Unit tests for ``Runner.run()`` dispatch behavior.

These tests do NOT require a real Modal account — they exercise ``Runner.run``
against local in-memory targets to verify the sync / async-only / sync-generator
/ async-generator dispatch branches and the ``TaskStruct`` return value for
idempotent re-execution.

For the end-to-end Modal round-trip (Runner invoked inside a Modal container),
see ``test__app.py::TestEndToEndBuild`` and ``TestEndToEndDynamicDepsBuild``.
"""

from __future__ import annotations

import pytest

try:
    import modal  # noqa: F401
except ImportError:
    pytest.skip("Skipping modal tests (import not available)", allow_module_level=True)

from stardag.integration.modal._runner import Runner
from stardag.testing.modal._tasks import (
    AsyncDoubleTask,
    AsyncDynamicRangeSumTask,
    SyncDynamicRangeSumTask,
    make_range,
    sum_list,
)


@pytest.fixture
def runner() -> Runner:
    return Runner()


class TestRunnerSyncTask:
    """Sync-only task: ``run()`` executes, returns None."""

    def test_sync_task_returns_none(self, runner: Runner, default_in_memory_fs_target):
        task = make_range(limit=3)
        result = runner(task)
        assert result is None
        assert task.complete()
        assert task.target().load() == [0, 1, 2]

    def test_sync_task_with_static_dep(
        self, runner: Runner, default_in_memory_fs_target
    ):
        # Pre-build the static dep so sum_list can execute
        range_task = make_range(limit=4)
        runner(range_task)
        assert range_task.complete()

        total = sum_list(values=range_task)
        result = runner(total)
        assert result is None
        assert total.target().load() == 6  # 0+1+2+3


class TestRunnerAsyncOnlyTask:
    """Async-only task: ``run_aio()`` is driven via ``asyncio.run``."""

    def test_async_only_task_returns_none(
        self, runner: Runner, default_in_memory_fs_target
    ):
        # Pre-build the input
        input_task = make_range(limit=5)  # produces list
        runner(input_task)
        # Use a task where the single numeric input is trivially available:
        # Wrap make_range's output in a simple int producer.
        source = sum_list(values=input_task)
        runner(source)
        assert source.complete()

        doubler = AsyncDoubleTask(input_task=source)
        result = runner(doubler)
        assert result is None
        assert doubler.complete()
        assert doubler.target().load() == 20  # sum(range(5)) * 2


class TestRunnerSyncDynamicDeps:
    """Sync generator dynamic deps: first invocation returns the yielded TaskStruct.

    Verifies the idempotent re-execution contract — generators can't cross
    the Modal boundary, so ``Runner.run()`` drives the generator forward and
    returns the first yield whose deps aren't all complete.
    """

    def test_first_invocation_returns_yielded_deps(
        self, runner: Runner, default_in_memory_fs_target
    ):
        task = SyncDynamicRangeSumTask(limit=4)
        result = runner(task)

        # The yielded dep (make_range(limit=4)) was incomplete — expect the
        # TaskStruct yield to be returned for the executor to build.
        assert result is not None
        # Result is the tuple/struct of yielded deps. For this task, a single
        # task is yielded, so flatten should find it.
        import stardag as sd

        deps = sd.flatten_task_struct(result)
        assert len(deps) == 1
        assert deps[0].id == make_range(limit=4).id
        # Task itself should NOT be complete yet (it was suspended)
        assert not task.complete()

    def test_re_execution_completes_task(
        self, runner: Runner, default_in_memory_fs_target
    ):
        task = SyncDynamicRangeSumTask(limit=4)
        # First invocation — yields deps (idempotent re-exec contract)
        first = runner(task)
        assert first is not None

        # Build the yielded deps (mimics what ModalTaskExecutor would do)
        import stardag as sd

        for dep in sd.flatten_task_struct(first):
            if not dep.complete():
                runner(dep)

        # Re-invoke the task — generator re-runs, past the yield now that the
        # dep is complete.
        second = runner(task)
        assert second is None
        assert task.complete()
        assert task.target().load() == 6  # sum(range(4))


class TestRunnerAsyncDynamicDeps:
    """Async generator dynamic deps: first invocation returns the yielded TaskStruct."""

    def test_first_invocation_returns_yielded_deps(
        self, runner: Runner, default_in_memory_fs_target
    ):
        task = AsyncDynamicRangeSumTask(limit=5)
        result = runner(task)

        assert result is not None
        import stardag as sd

        deps = sd.flatten_task_struct(result)
        assert len(deps) == 1
        assert deps[0].id == make_range(limit=5).id
        assert not task.complete()

    def test_re_execution_completes_task(
        self, runner: Runner, default_in_memory_fs_target
    ):
        task = AsyncDynamicRangeSumTask(limit=5)
        first = runner(task)
        assert first is not None

        import stardag as sd

        for dep in sd.flatten_task_struct(first):
            if not dep.complete():
                runner(dep)

        second = runner(task)
        assert second is None
        assert task.complete()
        assert task.target().load() == 10  # sum(range(5))
