"""Unit tests for ModalTaskExecutor's detached execution surface: spawn
under the execution id the engine claimed, cancel by recorded ref, and the
env a worker receives from the build context.

Modal primitives are faked; the live behaviors these mocks encode
(spawned-call survival, call-id stability, cancel) are pinned against a
real workspace in ``test_live_semantics.py``.
"""

import contextlib
from collections.abc import Iterator, Mapping
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest

try:
    import modal  # noqa: F401
except ImportError:
    pytest.skip("Skipping modal tests (import not available)", allow_module_level=True)

from stardag import BaseTask
from stardag.build import TaskExecutionError
from stardag.build._base import BuildContext, current_build_context_var
from stardag.integration.modal._executor import ModalTaskExecutor
from stardag.integration.modal._metadata import MODAL_EXECUTOR_NAME


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
    task.id = uuid4()
    return task


@contextlib.contextmanager
def _in_build(
    *,
    build_id: UUID | None = None,
    plan_id: UUID | None = None,
    settings: Mapping[str, str] | None = None,
) -> Iterator[BuildContext]:
    """Run inside a build context, as the resident engine or a tick sets it."""
    context = BuildContext(
        build_id=build_id or uuid4(),
        plan_id=plan_id,
        deployment_id=uuid4(),
        settings=dict(settings or {}),
    )
    token = current_build_context_var.set(context)
    try:
        yield context
    finally:
        current_build_context_var.reset(token)


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

        handle = await executor.submit_detached(task, execution_id=uuid4())

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

        handle = await executor.submit_detached(task, execution_id=uuid4())
        result = await handle.wait()

        assert isinstance(result, TaskExecutionError)
        assert isinstance(result.exception, ValueError)
        assert task.id not in executor._in_flight


class TestCancel:
    async def test_cancel_in_flight_call(self):
        function_call = FakeFunctionCall(object_id="fc-cancel", running=True)
        executor = _make_executor(FakeWorkerFunction(function_call))
        task = _make_task()
        await executor.submit_detached(task, execution_id=uuid4())

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
        handle = await executor.submit_detached(task, execution_id=uuid4())

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
        await executor.submit_detached(task, execution_id=uuid4())

        await executor.cancel(task)  # logs a warning, does not raise

    async def test_cancel_detached_cancels_the_recorded_call(self, monkeypatch):
        """The resident engine stops an orphaned spawn (its ref start was
        refused) by the ref it recorded, not by the in-flight map."""
        function_call = FakeFunctionCall(object_id="fc-orphan")
        refs: list[str] = []

        def from_id(ref: str):
            refs.append(ref)
            return function_call

        monkeypatch.setattr(modal.FunctionCall, "from_id", staticmethod(from_id))
        executor = _make_executor(FakeWorkerFunction(function_call))
        await executor.cancel_detached(_make_task(), MODAL_EXECUTOR_NAME, "fc-orphan")
        assert refs == ["fc-orphan"]
        assert function_call.cancel_count == 1

    async def test_cancel_detached_ignores_another_executors_ref(self, monkeypatch):
        refs: list[str] = []
        monkeypatch.setattr(
            modal.FunctionCall, "from_id", staticmethod(lambda ref: refs.append(ref))
        )
        executor = _make_executor(FakeWorkerFunction(FakeFunctionCall()))
        await executor.cancel_detached(_make_task(), "kubernetes", "job-1")
        assert refs == []


class TestWorkerEnv:
    """What a spawned worker receives through ``env_overrides``, layered in
    the design's precedence: the selector's per-task env, then the build's
    settings, then the framework's own identifiers **last** — so neither
    user code nor settings can redirect a worker's reports.
    ``STARDAG_DEPLOYMENT_ID`` is never forwarded: it is the container's own,
    baked into the app's secret by ``stardag modal deploy``."""

    async def _spawn_env(
        self, executor: ModalTaskExecutor, worker: FakeWorkerFunction, **context
    ) -> tuple[dict[str, str] | None, UUID]:
        execution_id = uuid4()
        if context.pop("outside", False):
            await executor.submit_detached(_make_task(), execution_id=execution_id)
        else:
            with _in_build(**context):
                await executor.submit_detached(_make_task(), execution_id=execution_id)
        _, env_overrides = worker.spawn_calls[-1]
        return env_overrides, execution_id

    async def test_the_build_plan_and_execution_ids_are_forwarded(self):
        from stardag.integration.modal._metadata import (
            STARDAG_BUILD_ID_ENV,
            STARDAG_EXECUTION_ID_ENV,
            STARDAG_MODAL_APP_NAME_ENV,
            STARDAG_PLAN_ID_ENV,
            STARDAG_WORKER_REPORTS_LIFECYCLE_ENV,
        )

        worker = FakeWorkerFunction(FakeFunctionCall())
        executor = _make_executor(worker)
        build_id, plan_id = uuid4(), uuid4()
        env, execution_id = await self._spawn_env(
            executor, worker, build_id=build_id, plan_id=plan_id
        )
        assert env is not None
        assert env[STARDAG_BUILD_ID_ENV] == str(build_id)
        assert env[STARDAG_PLAN_ID_ENV] == str(plan_id)
        assert env[STARDAG_EXECUTION_ID_ENV] == str(execution_id)
        assert env[STARDAG_MODAL_APP_NAME_ENV] == "test-app"
        assert STARDAG_WORKER_REPORTS_LIFECYCLE_ENV not in env

    async def test_settings_are_forwarded_and_framework_ids_win(self):
        from stardag.build._deployment import STARDAG_DEPLOYMENT_ID_ENV
        from stardag.integration.modal._metadata import (
            STARDAG_BUILD_ID_ENV,
            STARDAG_EXECUTION_ID_ENV,
        )

        worker = FakeWorkerFunction(FakeFunctionCall())
        executor = ModalTaskExecutor(
            modal_app_name="test-app",
            worker_selector=lambda task: (
                "default",
                {
                    "FROM_SELECTOR": "s",
                    "SHARED": "selector",
                    STARDAG_BUILD_ID_ENV: "hijacked",
                    STARDAG_EXECUTION_ID_ENV: "hijacked",
                    STARDAG_DEPLOYMENT_ID_ENV: "hijacked",
                },
            ),
        )
        executor._worker_functions["default"] = worker  # pyright: ignore[reportArgumentType]
        build_id = uuid4()
        env, execution_id = await self._spawn_env(
            executor,
            worker,
            build_id=build_id,
            plan_id=uuid4(),
            settings={"SHARED": "settings", "MODEL_SIZE": "large"},
        )
        assert env is not None
        assert env["FROM_SELECTOR"] == "s"
        # Settings layer over the selector's env ...
        assert env["SHARED"] == "settings"
        assert env["MODEL_SIZE"] == "large"
        # ... and the framework's ids over both.
        assert env[STARDAG_BUILD_ID_ENV] == str(build_id)
        assert env[STARDAG_EXECUTION_ID_ENV] == str(execution_id)
        assert STARDAG_DEPLOYMENT_ID_ENV not in env

    async def test_a_stale_reports_lifecycle_override_from_the_selector_is_cleared(
        self,
    ):
        """``STARDAG_WORKER_REPORTS_LIFECYCLE`` is framework-owned like the
        ids above. A worker selector (or a deployment's baked env) that
        happens to carry a stale ``=0`` must not survive into a worker
        invocation where this engine expects self-reporting — otherwise the
        worker suppresses its reports while the engine also does not report
        for it, and the task sits RUNNING until its claim lapses."""
        from stardag.integration.modal._metadata import (
            STARDAG_PLAN_ID_ENV,
            STARDAG_WORKER_REPORTS_LIFECYCLE_ENV,
        )

        worker = FakeWorkerFunction(FakeFunctionCall())
        executor = ModalTaskExecutor(
            modal_app_name="test-app",
            worker_selector=lambda task: (
                "default",
                {STARDAG_WORKER_REPORTS_LIFECYCLE_ENV: "0"},
            ),
        )
        executor._worker_functions["default"] = worker  # pyright: ignore[reportArgumentType]
        with _in_build(plan_id=uuid4()):
            assert executor.reports_lifecycle(_make_task()) is True
        env, _ = await self._spawn_env(executor, worker, plan_id=uuid4())
        assert env is not None
        assert STARDAG_WORKER_REPORTS_LIFECYCLE_ENV not in env
        assert STARDAG_PLAN_ID_ENV in env

    async def test_a_context_without_a_plan_never_forwards_selector_ids(self):
        """A build context with no plan (a registration failure degraded to
        ``warn``): the plan and execution ids are not the context's to give,
        and a selector's values for them are removed, not forwarded — the
        worker must not report through a selector-chosen plan/execution."""
        from stardag.integration.modal._metadata import (
            STARDAG_BUILD_ID_ENV,
            STARDAG_CLAIM_TTL_SECONDS_ENV,
            STARDAG_EXECUTION_ID_ENV,
            STARDAG_PLAN_ID_ENV,
        )

        worker = FakeWorkerFunction(FakeFunctionCall())
        hijack = {
            STARDAG_PLAN_ID_ENV: "hijacked",
            STARDAG_EXECUTION_ID_ENV: "hijacked",
            STARDAG_CLAIM_TTL_SECONDS_ENV: "1",
        }
        executor = ModalTaskExecutor(
            modal_app_name="test-app",
            worker_selector=lambda task: ("default", dict(hijack)),
        )
        executor._worker_functions["default"] = worker  # pyright: ignore[reportArgumentType]
        build_id = uuid4()
        with _in_build(build_id=build_id):
            await executor.submit_detached(_make_task(), execution_id=uuid4())
        _, env = worker.spawn_calls[-1]
        assert env is not None
        assert env[STARDAG_BUILD_ID_ENV] == str(build_id)
        assert STARDAG_PLAN_ID_ENV not in env
        assert STARDAG_EXECUTION_ID_ENV not in env
        assert env.get(STARDAG_CLAIM_TTL_SECONDS_ENV) != "1"

        # No context at all: the selector's framework ids are removed too.
        await executor.submit_detached(_make_task(), execution_id=uuid4())
        _, env = worker.spawn_calls[-1]
        assert env is None or not (set(hijack) & set(env))

    async def test_outside_a_build_only_the_selector_env_is_sent(self):
        from stardag.build._deployment import STARDAG_DEPLOYMENT_ID_ENV

        worker = FakeWorkerFunction(FakeFunctionCall())
        executor = _make_executor(worker)
        env, _ = await self._spawn_env(executor, worker, outside=True)
        assert env is None

        executor = ModalTaskExecutor(
            modal_app_name="test-app",
            worker_selector=lambda task: (
                "default",
                {"A": "1", STARDAG_DEPLOYMENT_ID_ENV: "x"},
            ),
        )
        executor._worker_functions["default"] = worker  # pyright: ignore[reportArgumentType]
        env, _ = await self._spawn_env(executor, worker, outside=True)
        # Exactly the selector's env: no framework ids, no deployment id.
        assert env == {"A": "1"}

    async def test_reporting_disabled_is_an_explicit_switch(self):
        """A non-reporting worker still gets the build id, the app name and
        the settings, and is told not to report by
        ``STARDAG_WORKER_REPORTS_LIFECYCLE=0``; the reporter's inputs (plan,
        execution, claim TTL, function timeout) are not sent."""
        from stardag.integration.modal._metadata import (
            STARDAG_BUILD_ID_ENV,
            STARDAG_CLAIM_TTL_SECONDS_ENV,
            STARDAG_EXECUTION_ID_ENV,
            STARDAG_MODAL_APP_NAME_ENV,
            STARDAG_MODAL_FUNCTION_TIMEOUT_ENV,
            STARDAG_PLAN_ID_ENV,
            STARDAG_WORKER_REPORTS_LIFECYCLE_ENV,
        )

        worker = FakeWorkerFunction(FakeFunctionCall())
        executor = ModalTaskExecutor(
            modal_app_name="test-app",
            worker_selector=lambda task: "default",
            worker_reports_lifecycle=False,
            worker_timeouts={"default": 600},
        )
        executor._worker_functions["default"] = worker  # pyright: ignore[reportArgumentType]
        build_id = uuid4()
        with _in_build(build_id=build_id, plan_id=uuid4(), settings={"S": "1"}):
            assert executor.reports_lifecycle(_make_task()) is False
        env, _ = await self._spawn_env(
            executor, worker, build_id=build_id, plan_id=uuid4(), settings={"S": "1"}
        )
        assert env is not None
        assert env[STARDAG_BUILD_ID_ENV] == str(build_id)
        assert env[STARDAG_MODAL_APP_NAME_ENV] == "test-app"
        assert env[STARDAG_WORKER_REPORTS_LIFECYCLE_ENV] == "0"
        assert env["S"] == "1"
        for absent in (
            STARDAG_PLAN_ID_ENV,
            STARDAG_EXECUTION_ID_ENV,
            STARDAG_CLAIM_TTL_SECONDS_ENV,
            STARDAG_MODAL_FUNCTION_TIMEOUT_ENV,
        ):
            assert absent not in env

    async def test_the_claim_ttl_and_function_timeout_come_from_the_worker(self):
        from stardag.integration.modal._metadata import (
            STARDAG_CLAIM_TTL_SECONDS_ENV,
            STARDAG_MODAL_FUNCTION_TIMEOUT_ENV,
        )

        worker = FakeWorkerFunction(FakeFunctionCall())
        executor = ModalTaskExecutor(
            modal_app_name="test-app",
            worker_selector=lambda task: "default",
            worker_timeouts={"default": 600},
        )
        executor._worker_functions["default"] = worker  # pyright: ignore[reportArgumentType]
        env, _ = await self._spawn_env(executor, worker, plan_id=uuid4())
        assert env is not None
        # The worker's Modal timeout plus the claim grace (build._claims).
        assert env[STARDAG_CLAIM_TTL_SECONDS_ENV] == str(600 + 900)
        assert float(env[STARDAG_MODAL_FUNCTION_TIMEOUT_ENV]) == 600.0

    async def test_reports_lifecycle_requires_build_context(self):
        executor = _make_executor(FakeWorkerFunction(FakeFunctionCall()))
        assert executor.reports_lifecycle(_make_task()) is False
        with _in_build():
            assert executor.reports_lifecycle(_make_task()) is True
        detached_off = _make_executor(
            FakeWorkerFunction(FakeFunctionCall()), detached=False
        )
        with _in_build():
            assert detached_off.reports_lifecycle(_make_task()) is False

    def test_the_build_plans_under_the_apps_deployment(self):
        executor = _make_executor(FakeWorkerFunction(FakeFunctionCall()))
        assert executor.deployment_app_name() == "test-app"


def test_reactive_scheduling_needs_self_reporting_workers():
    """A reactive build has no resident orchestrator: the worker registers
    the dependencies it yields and wakes the tick. A worker that does not
    report can do neither, so the combination would stall on the first
    dynamic yield; it is refused at construction."""
    with pytest.raises(ValueError, match="self-reporting workers"):
        ModalTaskExecutor(
            modal_app_name="app",
            worker_selector=lambda task: "default",
            reactive=True,
            worker_reports_lifecycle=False,
        )
    # Either alone is fine.
    ModalTaskExecutor(
        modal_app_name="app", worker_selector=lambda task: "default", reactive=True
    )
    ModalTaskExecutor(
        modal_app_name="app",
        worker_selector=lambda task: "default",
        worker_reports_lifecycle=False,
    )
