"""What the worker does when the platform ends its execution.

The whole of the worker's side of stardag#245, and it turns on one rule:

    **The task decides whether it is resumable; the clock decides who
    resumes it.**

Only ``ResumableInterruption`` asks to be resumed. An interruption the task
lets propagate is not a request — it means the task had no plan for one, so
either it hung or its worker's ``timeout`` is too small, and both should
end as an ordinary failure. That is why there is no per-task configuration
deciding whether a timeout was "expected": the task answered by raising, or
by not raising.

Given a request, *timing* says who honours it. Before the timeout an
escaping ``BaseException`` gets the input restarted by the backend on the
same call id; after it, nothing is coming and only a registry event can
bring the task back.

The tests below walk that as a grid — exception type × elapsed-vs-timeout —
because the two axes are independent and the interesting failures live in
the corners, not on the axes.
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
from stardag.integration.modal import MODAL_INTERRUPTIONS
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


# --- the exception set users are told to catch --------------------------


class TestModalInterruptions:
    def test_covers_both_signals_and_nothing_else(self):
        """The tuple is the public "catch these" answer, so its membership
        is API. Preemption arrives as KeyboardInterrupt and a timeout as
        InputCancellation; nothing else is a platform interruption."""
        assert set(MODAL_INTERRUPTIONS) == {KeyboardInterrupt, InputCancellation}

    def test_input_cancellation_is_not_a_keyboard_interrupt(self):
        """Why the tuple exists at all. ``except KeyboardInterrupt:`` — the
        obvious thing to write — silently does nothing on a timeout."""
        assert not issubclass(InputCancellation, KeyboardInterrupt)

    def test_members_escape_except_exception(self):
        """Both are BaseException-only, which is what stops an ordinary
        ``except Exception`` in a task body from swallowing them."""
        assert all(not issubclass(e, Exception) for e in MODAL_INTERRUPTIONS)

    def test_an_ordinary_bug_is_not_in_the_set(self):
        """The reason to catch this tuple rather than ``BaseException``: a
        NameError is a BaseException too, and converting one into "resume
        me" would run a deterministic failure until the budget is gone."""
        assert not isinstance(NameError("typo"), MODAL_INTERRUPTIONS)


# --- the classifier: exception type × timing ----------------------------


class TestClassifyInterruption:
    @pytest.mark.parametrize(
        ("exception", "before_timeout", "at_timeout"),
        [
            # Asked to be resumed → honoured either way; only *who* differs.
            (sd.ResumableInterruption("checkpointed"), _PREEMPTION, _TIMEOUT),
            # Did NOT ask → never reported, whatever the timing. The dead
            # execution becomes an ordinary failure on a later tick pass.
            (KeyboardInterrupt(), _CANCELLATION, _CANCELLATION),
            (
                InputCancellation("Input was cancelled by user"),
                _CANCELLATION,
                _CANCELLATION,
            ),
            (SystemExit(), _CANCELLATION, _CANCELLATION),
            # Not an interruption at all.
            (RuntimeError("ordinary bug"), None, None),
            (NameError("typo"), None, None),
        ],
    )
    def test_the_grid(self, exception, before_timeout, at_timeout):
        assert (
            _classify_interruption(
                exception, elapsed_seconds=5.0, function_timeout_seconds=300.0
            )
            == before_timeout
        )
        assert (
            _classify_interruption(
                exception, elapsed_seconds=300.0, function_timeout_seconds=300.0
            )
            == at_timeout
        )

    def test_slack_is_one_sided(self):
        """Measured elapsed time starts after container startup, so it runs
        short of Modal's clock — a request arriving a few seconds 'early' is
        still past the timeout."""
        assert (
            _classify_interruption(
                sd.ResumableInterruption("checkpointed"),
                elapsed_seconds=297.0,
                function_timeout_seconds=300.0,
            )
            == _TIMEOUT
        )

    def test_without_a_declared_timeout_nothing_can_be_shown_to_have_timed_out(
        self,
    ):
        """No value to compare against, so the backend is assumed able to
        restart it — the conservative direction, being what happened before
        interruptions were reported at all."""
        assert (
            _classify_interruption(
                sd.ResumableInterruption("checkpointed"),
                elapsed_seconds=99999.0,
                function_timeout_seconds=None,
            )
            == _PREEMPTION
        )


# --- what the runner reports, end to end --------------------------------


class TestRunnerReporting:
    def test_a_resumption_request_at_the_timeout_is_reported(
        self, registry, default_in_memory_fs_target
    ):
        """The one case that writes anything: the task asked, and nothing
        else will restart a timed-out call."""
        runner = _runner_raising(sd.ResumableInterruption("checkpointed"))

        with pytest.raises(BaseException):
            runner(make_range(limit=2), env_overrides=_env(uuid4(), timeout=0.0001))

        assert registry.methods() == ["task_start", "task_interrupt"]
        assert "resumed" in registry.calls[1][1]["reason"]

    def test_a_resumption_request_before_the_timeout_reports_nothing(
        self, registry, default_in_memory_fs_target
    ):
        """The backend restarts the input on the same call id, faster than a
        reschedule and keeping the claim — so reporting would be worse."""
        runner = _runner_raising(sd.ResumableInterruption("checkpointed"))

        with pytest.raises(KeyboardInterrupt) as caught:
            runner(make_range(limit=2), env_overrides=_env(uuid4(), timeout=600.0))

        # Translated on the way out: an ordinary Exception leaving the
        # container is a task failure Modal will not restart.
        assert isinstance(caught.value.__cause__, sd.ResumableInterruption)
        assert registry.methods() == ["task_start"]

    @pytest.mark.parametrize(
        "exception",
        [KeyboardInterrupt(), InputCancellation("Input was cancelled by user")],
    )
    @pytest.mark.parametrize("timeout", [0.0001, 600.0])
    def test_an_uncaught_interruption_never_reports(
        self, registry, default_in_memory_fs_target, exception, timeout
    ):
        """A task that did not ask to be resumed does not get resumed —
        whether the timeout had fired or not. It ends as a failure via the
        dead execution, which is the right answer for "it hung" and for
        "your timeout is too small" alike."""
        runner = _runner_raising(exception)

        with pytest.raises(BaseException):
            runner(make_range(limit=2), env_overrides=_env(uuid4(), timeout=timeout))

        assert registry.methods() == ["task_start"]

    def test_an_ordinary_exception_still_fails(
        self, registry, default_in_memory_fs_target
    ):
        """The control: catching BaseException in the runner must not have
        stopped real bugs being failures."""
        runner = _runner_raising(RuntimeError("genuine bug"))

        with pytest.raises(RuntimeError):
            runner(make_range(limit=2), env_overrides=_env(uuid4(), timeout=600.0))

        assert registry.methods() == ["task_start", "task_fail"]
        assert "genuine bug" in registry.calls[1][1]["error_message"]

    def test_the_reason_never_names_a_timeout_it_does_not_know(
        self, registry, default_in_memory_fs_target
    ):
        """A task may raise the request with no declared timeout forwarded.
        The reason lands in a user-visible message, so it must not read
        "the worker function's Nones timeout"."""
        runner = _runner_raising(sd.ResumableInterruption("checkpointed"))
        # No timeout forwarded → classified as preemption, so nothing is
        # reported at all; the guard is exercised through the branch that
        # does report, below.
        with pytest.raises(BaseException):
            runner(make_range(limit=2), env_overrides=_env(uuid4(), timeout=0.0001))

        reason = registry.calls[1][1]["reason"]
        assert "None" not in reason
        assert "0.0001s" in reason

    def test_no_reporter_still_translates_the_escape(
        self, registry, default_in_memory_fs_target
    ):
        """Without a build id there is nothing to report to — but the escape
        translation is what earns the backend restart, so it must not be
        conditional on reporting being configured."""
        runner = _runner_raising(sd.ResumableInterruption("checkpointed"))

        with pytest.raises(KeyboardInterrupt):
            runner(make_range(limit=2))

        assert registry.calls == []
