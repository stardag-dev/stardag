"""Who reports a resident build's task lifecycle when the deployed builder
drives it: the worker (the default :class:`Runner`) or the build engine (a
run function that does not report).

The deployed ``build`` derives that from the app's run function at
``finalize()`` and hands it to the executor its builder constructs. If the
executor assumed self-reporting workers for a run function that does not
report, a task that succeeded would sit RUNNING in the registry forever.

Modal is faked at the worker-function boundary only: the spawn runs the
app's own deployed ``worker_default`` wrapper in-process, so the run
function, the env the executor forwards and the reporter are all real
(only the one-build-per-process settings guard is bypassed, since the
worker shares the builder's process here).
"""

from __future__ import annotations

import asyncio
import contextlib
import types
import typing
from unittest.mock import patch

import pytest

try:
    import modal  # noqa: F401
except ImportError:
    pytest.skip("Skipping modal tests (import not available)", allow_module_level=True)

from stardag import BaseTask, auto_namespace
from stardag.build import BuildExitStatus
from stardag.build._registration import new_id
from stardag.integration.modal import (
    FunctionSettings,
    ModalTaskExecutor,
    Runner,
    StardagApp,
)
from stardag.integration.modal._protocols import _run_function_reports_lifecycle
from stardag.registry import registry_provider
from stardag.target import InMemoryFileTarget
from stardag.testing import InMemoryRegistry
from stardag.utils.testing.helper_tasks import SyncOnlyTask

from tests.test_integration.test_modal._app_helpers import (  # noqa: F401
    _finalize_capturing_functions,
    _make_image,
    _mock_secret_hydrate,
    _stub_modal_concurrent,
)

auto_namespace(__name__)

APP_NAME = "lifecycle-app"


def plain_run_function(task: BaseTask) -> None:
    """A run function implementing the protocol directly: it runs the task
    and reports nothing."""
    task.run()


class _ReportingDelegate:
    """A custom run function that does report, by delegating to a Runner,
    and says so."""

    reports_lifecycle = True

    def __init__(self) -> None:
        self._runner = Runner()

    def __call__(self, task, *, env_overrides=None):
        return self._runner(task, env_overrides=env_overrides)


class _LazyFunctionCall:
    """A spawned call whose container runs once the engine waits on it, so
    the engine has recorded the execution's ref before the worker reports
    (a real container's cold start gives the same order)."""

    def __init__(self, run: typing.Callable[[], typing.Any]):
        self.object_id = f"fc-{new_id()}"
        self._run = run
        self.get = types.SimpleNamespace(aio=self._get_aio)
        self.cancel = types.SimpleNamespace(aio=self._cancel_aio)

    async def _get_aio(self, timeout: float | None = None):
        return await asyncio.to_thread(self._run)

    async def _cancel_aio(self):
        pass


class _InProcessWorker:
    """A deployed worker function whose ``spawn`` runs the app's registered
    ``worker_*`` wrapper in a thread, as a Modal container would run it."""

    def __init__(self, deployed_run: typing.Callable[..., typing.Any]):
        self._deployed_run = deployed_run
        self.spawn = types.SimpleNamespace(aio=self._spawn_aio)

    async def _spawn_aio(self, task, env_overrides=None):
        return _LazyFunctionCall(
            lambda: self._deployed_run(task, env_overrides=env_overrides)
        )


def _deploy(run_function) -> dict:
    app = StardagApp(
        APP_NAME,
        run_function=run_function,
        builder_settings=FunctionSettings(image=_make_image()),
        worker_settings={"default": FunctionSettings(image=_make_image())},
        task_modules=[],
    )
    return _finalize_capturing_functions(app)


def _build_through_the_deployed_builder(
    registered: dict, task: BaseTask, registry: InMemoryRegistry
) -> tuple[typing.Any, list[ModalTaskExecutor]]:
    worker = _InProcessWorker(registered["worker_default"])
    executors: list[ModalTaskExecutor] = []
    real_init = ModalTaskExecutor.__init__

    def recording_init(self, **kwargs):
        real_init(self, **kwargs)
        executors.append(self)

    with (
        registry_provider.override(registry),
        patch.object(ModalTaskExecutor, "__init__", recording_init),
        patch.object(
            ModalTaskExecutor, "_get_worker_function", lambda self, name: worker
        ),
        patch.object(
            ModalTaskExecutor, "_get_base_executor_metadata", return_value=None
        ),
        patch("stardag.integration.modal._executor.spawn_tick"),
        # The worker runs in the builder's process here, where the resident
        # build already owns the process's settings; in Modal it is its own
        # container.
        patch(
            "stardag.integration.modal._functions.settings_owner",
            lambda build_id: contextlib.nullcontext(),
        ),
    ):
        summary = registered["build"](task, lambda t: "default", APP_NAME, None)
    return summary, executors


@pytest.fixture
def registry() -> InMemoryRegistry:
    registry = InMemoryRegistry()
    registry.add_deployment(app_name=APP_NAME)
    return registry


@pytest.mark.parametrize(
    ("run_function", "worker_reports"),
    [
        (plain_run_function, False),
        (Runner(report_lifecycle=False), False),
        (Runner(), True),
        (_ReportingDelegate(), True),
    ],
    ids=["plain-function", "runner-not-reporting", "runner", "declared-reporting"],
)
def test_the_deployed_builder_reports_for_a_run_function_that_does_not(
    run_function,
    worker_reports: bool,
    registry: InMemoryRegistry,
    default_in_memory_fs_target: type[InMemoryFileTarget],
):
    task = SyncOnlyTask(name=f"lifecycle-{new_id()}")
    summary, executors = _build_through_the_deployed_builder(
        _deploy(run_function), task, registry
    )

    assert summary.status == BuildExitStatus.SUCCESS
    (executor,) = executors
    assert executor.worker_reports_lifecycle is worker_reports
    # Exactly one completion, whoever sent it — and the task is completed,
    # not left RUNNING.
    assert len(registry.calls_to("member_complete", task_id=task.id)) == 1
    assert registry.status_of(task.id) == "completed"


class TestRunFunctionReportsLifecycle:
    def test_the_default_runner_reports(self):
        assert _run_function_reports_lifecycle(Runner()) is True

    def test_a_runner_constructed_not_to_report_does_not(self):
        assert _run_function_reports_lifecycle(Runner(report_lifecycle=False)) is False

    def test_a_runner_subclass_skipping_super_init_still_reports(self):
        class Bare(Runner):
            def __init__(self):
                pass

        assert _run_function_reports_lifecycle(Bare()) is True

    def test_a_plain_function_does_not(self):
        assert _run_function_reports_lifecycle(plain_run_function) is False

    def test_a_custom_implementation_can_declare_it(self):
        assert _run_function_reports_lifecycle(_ReportingDelegate()) is True


class TestReactiveNeedsAReportingRunFunction:
    def test_build_trigger_refuses_reactive_with_a_non_reporting_run_function(
        self,
    ):
        app = StardagApp(
            APP_NAME,
            run_function=plain_run_function,
            builder_settings=FunctionSettings(image=_make_image()),
            worker_settings={"default": FunctionSettings(image=_make_image())},
            task_modules=["stardag.utils.testing.helper_tasks"],
        )
        with pytest.raises(ValueError, match="reports the task lifecycle"):
            app.build_trigger(SyncOnlyTask(name="refused"), reactive=True)

    def test_the_tick_executor_refuses_it_too(self):
        """A build triggered before a redeploy swapped in a non-reporting
        run function: the deployed tick carries the flag, and the executor
        it constructs refuses reactive mode rather than leaving tasks
        RUNNING."""
        registered = _deploy(plain_run_function)
        tick = registered["tick"]
        deployment = next(
            cell.cell_contents
            for cell in tick.__closure__ or ()
            if type(cell.cell_contents).__name__ == "_TickDeployment"
        )
        assert deployment.worker_reports_lifecycle is False
        with pytest.raises(ValueError, match="self-reporting workers"):
            ModalTaskExecutor(
                modal_app_name=APP_NAME,
                worker_selector=deployment.worker_selector,
                reactive=True,
                worker_reports_lifecycle=deployment.worker_reports_lifecycle,
            )
