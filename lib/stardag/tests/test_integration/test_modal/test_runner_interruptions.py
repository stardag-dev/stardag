"""What the worker does when the platform ends its execution.

The classification matrix, which is the whole of the worker's side of
stardag#245. Two rows are the ones a future refactor is most likely to
break, and both are load-bearing:

- **A cancellation must report nothing.** Modal delivers a function
  timeout and a ``FunctionCall.cancel()`` identically — same signal, same
  ``InputCancellation``, same message — and stardag cancels its own
  workers on FAIL_FAST and on a UI cancel. A worker that read every
  cancellation as an interruption would resurrect tasks the build just
  cancelled. The only discriminator is elapsed-vs-declared-timeout.
- **``TaskInterrupted`` must escape as a ``BaseException``.** An ordinary
  exception leaving the container is a task failure to Modal, which will
  not restart the input; a ``BaseException`` reads as a crashed container,
  which it will. That translation is what makes the documented recipe
  ("catch the interrupt, checkpoint, raise ``sd.TaskInterrupted``") do
  what it says instead of permanently failing the build.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

try:
    import modal  # noqa: F401
except ImportError:
    pytest.skip("Skipping modal tests (import not available)", allow_module_level=True)

from modal.exception import InputCancellation

import stardag as sd
from stardag.integration.modal._metadata import (
    STARDAG_BUILD_ID_ENV,
    STARDAG_MODAL_FUNCTION_TIMEOUT_ENV,
)
from stardag.integration.modal._runner import (
    _CANCELLATION,
    _PREEMPTION,
    _TIMEOUT,
    Runner,
    _classify_interruption,
)
from stardag.registry import NoOpRegistry, registry_provider
from stardag.testing.modal._tasks import make_range


class RecordingRegistry(NoOpRegistry):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, dict]] = []

    def task_start(
        self,
        build_id,
        task,
        executor=None,
        executor_ref=None,
        executor_metadata=None,
        claim_ttl_seconds=None,
    ) -> None:
        self.calls.append(("task_start", {"executor_ref": executor_ref}))

    def task_complete(self, build_id, task) -> None:
        self.calls.append(("task_complete", {}))

    def task_fail(self, build_id, task, error_message=None) -> None:
        self.calls.append(("task_fail", {"error_message": error_message}))

    def task_interrupt(self, build_id, task, reason=None) -> None:
        self.calls.append(("task_interrupt", {"reason": reason}))

    def methods(self) -> list[str]:
        return [m for (m, _) in self.calls]


@pytest.fixture
def registry():
    instance = RecordingRegistry()
    with registry_provider.override(instance):
        yield instance


@pytest.fixture(autouse=True)
def fake_call_id(monkeypatch):
    monkeypatch.setattr(modal, "current_function_call_id", lambda: "fc-1")


def _env(build_id: UUID, timeout: float | None = None) -> dict[str, str]:
    env = {STARDAG_BUILD_ID_ENV: str(build_id)}
    if timeout is not None:
        env[STARDAG_MODAL_FUNCTION_TIMEOUT_ENV] = str(timeout)
    return env


def _runner_raising(exception: BaseException) -> Runner:
    class Raising(Runner):
        def run(self, task):
            raise exception

    return Raising()


# --- the classifier, in isolation ---------------------------------------


class TestClassifyInterruption:
    @pytest.mark.parametrize(
        ("exception", "expected"),
        [
            (KeyboardInterrupt(), _PREEMPTION),
            (SystemExit(), _PREEMPTION),
            (sd.TaskInterrupted("checkpointed"), _PREEMPTION),
            (sd.TaskPreempted("reclaimed"), _PREEMPTION),
            (sd.TaskTimedOut("ran long"), _TIMEOUT),
            (RuntimeError("ordinary bug"), None),
        ],
    )
    def test_unambiguous_exceptions(self, exception, expected):
        assert (
            _classify_interruption(
                exception, elapsed_seconds=1.0, function_timeout_seconds=600.0
            )
            == expected
        )

    def test_input_cancellation_at_the_timeout_is_a_timeout(self):
        assert (
            _classify_interruption(
                InputCancellation("Input was cancelled by user"),
                elapsed_seconds=600.0,
                function_timeout_seconds=600.0,
            )
            == _TIMEOUT
        )

    def test_input_cancellation_well_before_it_is_a_cancellation(self):
        """The FAIL_FAST / UI-cancel case. Reporting an interruption here
        would put a task the build just cancelled back in the frontier."""
        assert (
            _classify_interruption(
                InputCancellation("Input was cancelled by user"),
                elapsed_seconds=8.0,
                function_timeout_seconds=600.0,
            )
            == _CANCELLATION
        )

    def test_slack_is_one_sided(self):
        """Measured elapsed time starts later than Modal's clock (which
        includes container startup), so it runs short — a signal a few
        seconds 'early' is still the timeout."""
        assert (
            _classify_interruption(
                InputCancellation("Input was cancelled by user"),
                elapsed_seconds=597.0,
                function_timeout_seconds=600.0,
            )
            == _TIMEOUT
        )

    def test_without_a_declared_timeout_it_is_a_cancellation(self):
        """No value to compare against → the conservative reading, which
        is also the behaviour that predates interruption reporting."""
        assert (
            _classify_interruption(
                InputCancellation("Input was cancelled by user"),
                elapsed_seconds=99999.0,
                function_timeout_seconds=None,
            )
            == _CANCELLATION
        )


# --- what the runner reports, end to end --------------------------------


class TestRunnerInterruptionReporting:
    def test_timeout_reports_an_interruption_not_a_failure(
        self, registry, default_in_memory_fs_target
    ):
        runner = _runner_raising(InputCancellation("Input was cancelled by user"))

        with pytest.raises(InputCancellation):
            runner(make_range(limit=2), env_overrides=_env(uuid4(), timeout=0.0001))

        assert registry.methods() == ["task_start", "task_interrupt"]
        assert "timeout" in registry.calls[1][1]["reason"]

    def test_cancellation_reports_nothing(
        self, registry, default_in_memory_fs_target
    ):
        runner = _runner_raising(InputCancellation("Input was cancelled by user"))

        with pytest.raises(InputCancellation):
            runner(make_range(limit=2), env_overrides=_env(uuid4(), timeout=600.0))

        assert registry.methods() == ["task_start"]

    def test_preemption_reports_nothing_and_propagates(
        self, registry, default_in_memory_fs_target
    ):
        """Modal restarts a crashed container's input itself, on the same
        call id and without spending an attempt. Recording a terminal event
        would replace that with a slower reschedule and release a claim the
        restart still needs."""
        runner = _runner_raising(KeyboardInterrupt())

        with pytest.raises(KeyboardInterrupt):
            runner(make_range(limit=2), env_overrides=_env(uuid4(), timeout=600.0))

        assert registry.methods() == ["task_start"]

    def test_task_interrupted_escapes_as_a_base_exception(
        self, registry, default_in_memory_fs_target
    ):
        """The documented recipe. ``sd.TaskInterrupted`` is an ordinary
        Exception by design — so it does not slip past the user's own error
        handling — but an ordinary exception leaving the container is a
        task failure Modal will not restart. The runner translates."""
        runner = _runner_raising(sd.TaskInterrupted("checkpointed; reschedule me"))

        with pytest.raises(KeyboardInterrupt) as caught:
            runner(make_range(limit=2), env_overrides=_env(uuid4(), timeout=600.0))

        assert isinstance(caught.value.__cause__, sd.TaskInterrupted)
        assert registry.methods() == ["task_start"]

    def test_the_footgun_is_closed(self, registry, default_in_memory_fs_target):
        """The regression this whole change exists to prevent: a task that
        catches the interrupt to checkpoint and reports an interruption
        must not end up FAILED."""
        runner = _runner_raising(sd.TaskInterrupted("checkpointed"))

        with pytest.raises(BaseException):
            runner(make_range(limit=2), env_overrides=_env(uuid4(), timeout=600.0))

        assert "task_fail" not in registry.methods()

    def test_an_ordinary_exception_still_fails(
        self, registry, default_in_memory_fs_target
    ):
        """The control. Widening the wrapper to BaseException must not have
        made real bugs stop being failures."""
        runner = _runner_raising(RuntimeError("genuine bug"))

        with pytest.raises(RuntimeError):
            runner(make_range(limit=2), env_overrides=_env(uuid4(), timeout=600.0))

        assert registry.methods() == ["task_start", "task_fail"]
        assert "genuine bug" in registry.calls[1][1]["error_message"]

    def test_task_timed_out_reports_regardless_of_elapsed_time(
        self, registry, default_in_memory_fs_target
    ):
        """An explicit exception outranks the timing heuristic — the task
        knows something the clock does not."""
        runner = _runner_raising(sd.TaskTimedOut("I know I am out of time"))

        with pytest.raises(BaseException):
            runner(make_range(limit=2), env_overrides=_env(uuid4(), timeout=600.0))

        assert registry.methods() == ["task_start", "task_interrupt"]

    def test_no_reporter_still_translates_the_escape(
        self, registry, default_in_memory_fs_target
    ):
        """Without a build id there is nothing to report to — but the
        escape translation is what earns the backend restart, so it must
        not be conditional on reporting being configured."""
        runner = _runner_raising(sd.TaskInterrupted("checkpointed"))

        with pytest.raises(KeyboardInterrupt):
            runner(make_range(limit=2))

        assert registry.calls == []
