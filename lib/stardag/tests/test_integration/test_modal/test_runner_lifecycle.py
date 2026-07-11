"""Unit tests for worker-side lifecycle reporting in ``Runner``.

No real Modal account needed — ``modal.current_function_call_id`` and the
registry are faked. Verifies that the worker reports started (with its own
function call id as executor ref), completed (+ artifacts), suspended, and
failed events when a build id is forwarded via ``STARDAG_BUILD_ID`` — and
stays silent otherwise (no build id, NoOp registry, or opt-out).
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

try:
    import modal  # noqa: F401
except ImportError:
    pytest.skip("Skipping modal tests (import not available)", allow_module_level=True)

from stardag.integration.modal._app import (
    MODAL_EXECUTOR_NAME,
    STARDAG_BUILD_ID_ENV,
    Runner,
    _WorkerLifecycleReporter,
)
from stardag.registry import NoOpRegistry, registry_provider
from stardag.testing.modal._tasks import SyncDynamicRangeSumTask, make_range

WORKER_CALL_ID = "fc-worker-call-1"


class RecordingSyncRegistry(NoOpRegistry):
    """Records the sync lifecycle calls the worker reporter makes."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, dict]] = []
        self.raise_on: set[str] = set()

    def _record(self, method: str, **extra) -> None:
        if method in self.raise_on:
            raise ConnectionError(f"registry down during {method}")
        self.calls.append((method, extra))

    def task_start(self, build_id, task, executor=None, executor_ref=None) -> None:
        self._record(
            "task_start",
            build_id=build_id,
            executor=executor,
            executor_ref=executor_ref,
        )

    def task_complete(self, build_id, task) -> None:
        self._record("task_complete", build_id=build_id)

    def task_suspend(self, build_id, task) -> None:
        self._record("task_suspend", build_id=build_id)

    def task_fail(self, build_id, task, error_message=None) -> None:
        self._record("task_fail", build_id=build_id, error_message=error_message)

    def task_upload_artifacts(self, build_id, task, artifacts) -> None:
        self._record("task_upload_artifacts", artifacts=artifacts)

    def methods(self) -> list[str]:
        return [m for (m, _) in self.calls]


@pytest.fixture
def recording_registry():
    registry = RecordingSyncRegistry()
    with registry_provider.override(registry):
        yield registry


@pytest.fixture
def fake_call_id(monkeypatch):
    monkeypatch.setattr(modal, "current_function_call_id", lambda: WORKER_CALL_ID)
    return WORKER_CALL_ID


def _env(build_id: UUID) -> dict[str, str]:
    return {STARDAG_BUILD_ID_ENV: str(build_id)}


class TestRunnerLifecycleReporting:
    def test_success_reports_start_and_complete(
        self, recording_registry, fake_call_id, default_in_memory_fs_target
    ):
        build_id = uuid4()
        task = make_range(limit=3)

        result = Runner()(task, env_overrides=_env(build_id))

        assert result is None
        assert task.complete()
        assert recording_registry.methods() == ["task_start", "task_complete"]
        start_extra = recording_registry.calls[0][1]
        assert start_extra["build_id"] == build_id
        assert start_extra["executor"] == MODAL_EXECUTOR_NAME
        assert start_extra["executor_ref"] == WORKER_CALL_ID

    def test_failure_reports_fail_and_reraises(
        self, recording_registry, fake_call_id, default_in_memory_fs_target
    ):
        class Boom(Exception):
            pass

        class FailingRunner(Runner):
            def run(self, task):
                raise Boom("task exploded")

        with pytest.raises(Boom):
            FailingRunner()(make_range(limit=2), env_overrides=_env(uuid4()))

        assert recording_registry.methods() == ["task_start", "task_fail"]
        assert "task exploded" in recording_registry.calls[1][1]["error_message"]

    def test_dynamic_deps_yield_reports_suspend(
        self, recording_registry, fake_call_id, default_in_memory_fs_target
    ):
        # First invocation: the yielded range task is incomplete → the
        # runner returns the TaskStruct and the task is suspended.
        task = SyncDynamicRangeSumTask(limit=3)

        result = Runner()(task, env_overrides=_env(uuid4()))

        assert result is not None  # TaskStruct of incomplete deps
        assert recording_registry.methods() == ["task_start", "task_suspend"]

    def test_no_build_id_no_reporting(
        self, recording_registry, fake_call_id, default_in_memory_fs_target
    ):
        result = Runner()(make_range(limit=3))

        assert result is None
        assert recording_registry.calls == []

    def test_opt_out_no_reporting(
        self, recording_registry, fake_call_id, default_in_memory_fs_target
    ):
        result = Runner(report_lifecycle=False)(
            make_range(limit=3), env_overrides=_env(uuid4())
        )

        assert result is None
        assert recording_registry.calls == []

    def test_registry_errors_never_fail_the_task(
        self, recording_registry, fake_call_id, default_in_memory_fs_target
    ):
        recording_registry.raise_on = {"task_start", "task_complete"}
        task = make_range(limit=3)

        result = Runner()(task, env_overrides=_env(uuid4()))

        assert result is None
        assert task.complete()  # the actual work still succeeded


class TestReporterCreate:
    def test_none_without_build_id(self, recording_registry):
        assert _WorkerLifecycleReporter.create(make_range(limit=1), None) is None
        assert _WorkerLifecycleReporter.create(make_range(limit=1), {}) is None

    def test_none_with_noop_registry(self):
        with registry_provider.override(NoOpRegistry()):
            reporter = _WorkerLifecycleReporter.create(
                make_range(limit=1), _env(uuid4())
            )
        assert reporter is None

    def test_none_with_invalid_build_id(self, recording_registry):
        reporter = _WorkerLifecycleReporter.create(
            make_range(limit=1), {STARDAG_BUILD_ID_ENV: "not-a-uuid"}
        )
        assert reporter is None

    def test_build_id_from_process_env(self, recording_registry, monkeypatch):
        """Older deployed apps apply env_overrides as process env vars around
        the run call — the reporter also accepts the env-var form."""
        build_id = uuid4()
        monkeypatch.setenv(STARDAG_BUILD_ID_ENV, str(build_id))
        reporter = _WorkerLifecycleReporter.create(make_range(limit=1), None)
        assert reporter is not None
        assert reporter.build_id == build_id
