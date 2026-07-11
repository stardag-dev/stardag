"""Unit tests for ModalTaskExecutor's detached execution surface.

Modal primitives are faked; the live behaviors these mocks encode
(spawned-call survival, from_id re-attach, call-id stability, cancel) are
pinned against a real workspace in ``test_live_semantics.py``.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

try:
    import modal  # noqa: F401
except ImportError:
    pytest.skip("Skipping modal tests (import not available)", allow_module_level=True)

from stardag import BaseTask
from stardag.build import TaskExecutionError
from stardag.integration.modal._app import MODAL_EXECUTOR_NAME, ModalTaskExecutor


class FakeFunctionCall:
    """Stand-in for modal.FunctionCall with configurable get() behavior."""

    def __init__(
        self,
        object_id: str = "fc-fake-1",
        result=None,
        error: Exception | None = None,
        running: bool = False,
        block: bool = False,
    ):
        self.object_id = object_id
        self._result = result
        self._error = error
        self._running = running
        self._block = block
        self.cancel_count = 0
        self.get = SimpleNamespace(aio=self._get_aio)
        self.cancel = SimpleNamespace(aio=self._cancel_aio)

    async def _get_aio(self, timeout: float | None = None):
        if self._running and timeout == 0:
            raise TimeoutError("still running")
        if self._block and timeout is None:
            import asyncio

            await asyncio.Event().wait()  # blocks until cancelled
        if self._error is not None:
            raise self._error
        return self._result

    async def _cancel_aio(self):
        self.cancel_count += 1


class FakeWorkerFunction:
    """Stand-in for a deployed Modal worker function."""

    def __init__(self, function_call: FakeFunctionCall):
        self._function_call = function_call
        self.spawn_calls: list[tuple] = []
        self.spawn = SimpleNamespace(aio=self._spawn_aio)

    async def _spawn_aio(self, task, env_overrides=None):
        self.spawn_calls.append((task, env_overrides))
        return self._function_call


def _make_executor(worker_function, detached: bool = True) -> ModalTaskExecutor:
    executor = ModalTaskExecutor(
        modal_app_name="test-app",
        worker_selector=lambda task: "default",
        detached=detached,
    )
    executor._worker_functions["default"] = worker_function  # bypass from_name
    return executor


def _make_task() -> BaseTask:
    task = MagicMock(spec=BaseTask)
    task.id = __import__("uuid").uuid4()
    return task


class TestSupportsDetached:
    def test_default_on(self):
        executor = _make_executor(FakeWorkerFunction(FakeFunctionCall()))
        assert executor.supports_detached(_make_task()) is True

    def test_opt_out(self):
        executor = _make_executor(
            FakeWorkerFunction(FakeFunctionCall()), detached=False
        )
        assert executor.supports_detached(_make_task()) is False


class TestSubmitDetached:
    async def test_spawns_and_returns_handle(self):
        function_call = FakeFunctionCall(object_id="fc-123", result=None)
        worker = FakeWorkerFunction(function_call)
        executor = _make_executor(worker)
        task = _make_task()

        handle = await executor.submit_detached(task)

        assert handle.executor == MODAL_EXECUTOR_NAME
        assert handle.ref == "fc-123"
        assert len(worker.spawn_calls) == 1
        assert worker.spawn_calls[0][0] is task
        # In-flight tracking active until wait() resolves.
        assert executor._in_flight[task.id] is function_call

        result = await handle.wait()
        assert result is None
        assert task.id not in executor._in_flight

    async def test_wait_wraps_remote_exception(self):
        function_call = FakeFunctionCall(error=ValueError("task blew up"))
        executor = _make_executor(FakeWorkerFunction(function_call))
        task = _make_task()

        handle = await executor.submit_detached(task)
        result = await handle.wait()

        assert isinstance(result, TaskExecutionError)
        assert isinstance(result.exception, ValueError)
        assert task.id not in executor._in_flight


class TestReattach:
    def _patch_from_id(self, monkeypatch, function_call: FakeFunctionCall):
        calls: list[str] = []

        def from_id(ref: str):
            calls.append(ref)
            return function_call

        monkeypatch.setattr(modal.FunctionCall, "from_id", staticmethod(from_id))
        return calls

    async def test_running_call_reattaches(self, monkeypatch):
        function_call = FakeFunctionCall(
            object_id="fc-live", result={"ok": True}, running=True
        )
        from_id_calls = self._patch_from_id(monkeypatch, function_call)
        executor = _make_executor(FakeWorkerFunction(function_call))
        task = _make_task()

        handle = await executor.reattach(task, MODAL_EXECUTOR_NAME, "fc-live")

        assert handle is not None
        assert handle.ref == "fc-live"
        assert from_id_calls == ["fc-live"]
        # Tracked for cancel while re-attached.
        assert executor._in_flight[task.id] is function_call
        assert await handle.wait() == {"ok": True}
        assert task.id not in executor._in_flight

    async def test_already_finished_call_resolves_immediately(self, monkeypatch):
        function_call = FakeFunctionCall(result={"done": 1}, running=False)
        self._patch_from_id(monkeypatch, function_call)
        executor = _make_executor(FakeWorkerFunction(function_call))

        handle = await executor.reattach(_make_task(), MODAL_EXECUTOR_NAME, "fc-done")

        assert handle is not None
        assert await handle.wait() == {"done": 1}

    async def test_failed_call_returns_none(self, monkeypatch):
        function_call = FakeFunctionCall(error=RuntimeError("remote failure"))
        self._patch_from_id(monkeypatch, function_call)
        executor = _make_executor(FakeWorkerFunction(function_call))

        handle = await executor.reattach(_make_task(), MODAL_EXECUTOR_NAME, "fc-dead")

        assert handle is None

    async def test_unknown_executor_name_returns_none(self, monkeypatch):
        function_call = FakeFunctionCall(running=True)
        from_id_calls = self._patch_from_id(monkeypatch, function_call)
        executor = _make_executor(FakeWorkerFunction(function_call))

        handle = await executor.reattach(_make_task(), "kubernetes", "job-1")

        assert handle is None
        assert from_id_calls == []  # never touched Modal

    async def test_detached_off_returns_none(self, monkeypatch):
        function_call = FakeFunctionCall(running=True)
        self._patch_from_id(monkeypatch, function_call)
        executor = _make_executor(FakeWorkerFunction(function_call), detached=False)

        handle = await executor.reattach(_make_task(), MODAL_EXECUTOR_NAME, "fc-x")

        assert handle is None


class TestCancel:
    async def test_cancel_in_flight_call(self):
        function_call = FakeFunctionCall(object_id="fc-cancel", running=True)
        executor = _make_executor(FakeWorkerFunction(function_call))
        task = _make_task()
        await executor.submit_detached(task)

        await executor.cancel(task)

        assert function_call.cancel_count == 1
        assert task.id not in executor._in_flight

    async def test_cancel_unknown_task_is_noop(self):
        executor = _make_executor(FakeWorkerFunction(FakeFunctionCall()))
        await executor.cancel(_make_task())  # no raise

    async def test_cancelling_wait_cancels_remote_call(self):
        """asyncio cancellation of the awaiting wait() (FAIL_FAST / user
        cancel) must cancel the detached remote call itself — the in-flight
        entry is popped by wait()'s cleanup before the executor cancel()
        hook runs, so wait() owns this path."""
        import asyncio

        function_call = FakeFunctionCall(object_id="fc-block", block=True)
        executor = _make_executor(FakeWorkerFunction(function_call))
        task = _make_task()
        handle = await executor.submit_detached(task)

        waiter = asyncio.ensure_future(handle.wait())
        await asyncio.sleep(0)  # let it start awaiting get()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter

        assert function_call.cancel_count == 1  # remote call cancelled
        assert task.id not in executor._in_flight

    async def test_cancel_failure_is_swallowed(self):
        function_call = FakeFunctionCall(object_id="fc-err", running=True)

        async def failing_cancel():
            raise ConnectionError("network down")

        function_call.cancel = SimpleNamespace(aio=failing_cancel)
        executor = _make_executor(FakeWorkerFunction(function_call))
        task = _make_task()
        await executor.submit_detached(task)

        await executor.cancel(task)  # logs a warning, does not raise
