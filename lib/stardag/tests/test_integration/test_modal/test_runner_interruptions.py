"""What the worker does when the platform ends its execution.

The whole of the worker's side of stardag#245, and it turns on one rule:

    **The task decides whether it is resumable; the exception it was
    raised from decides who resumes it.**

Only ``ResumableInterruption`` asks to be resumed. An interruption the task
lets propagate is not a request — it means the task had no plan for one, so
either it hung or its worker's ``timeout`` is too small, and both should
end as an ordinary failure. That is why there is no per-task configuration
deciding whether a timeout was "expected": the task answered by raising, or
by not raising.

Given a request, the *signal underneath it* says who honours it. Only a
preemption gets the input restarted by the backend on the same call id;
once a function timeout or a cancel has ended the call nothing is coming,
and only a registry event brings the task back. Modal separates exactly
those two by type — ``KeyboardInterrupt`` versus ``InputCancellation`` —
and the chain preserves it through the ``from None`` the docs recommend,
so the answer is read rather than guessed.

It used to be guessed, from ``elapsed >= timeout - slack``, on a clock that
starts after container boot and so systematically under-reads. An 86400s
worker measured 86392.0s, read as "before the timeout", reported nothing
and left the build stalled for a day. The clock survives only as a fallback
for a task that raises the request on its own initiative.

The tests below walk that as a grid — exception type × how it was raised ×
elapsed-vs-timeout — because the axes are independent and the interesting
failures live in the corners, not on the axes.
"""

from __future__ import annotations

import pytest

try:
    import modal
except ImportError:
    pytest.skip("Skipping modal tests (import not available)", allow_module_level=True)

from modal.exception import InputCancellation

import stardag as sd
from stardag.integration.modal import MODAL_INTERRUPTIONS
from stardag.integration.modal._metadata import (
    STARDAG_CLAIM_TTL_SECONDS_ENV,
    STARDAG_MODAL_FUNCTION_TIMEOUT_ENV,
)
from stardag.integration.modal._runner import (
    _CANCELLATION,
    _PREEMPTION,
    _TIMEOUT,
    Runner,
    _classify_interruption,
)
from stardag.registry import registry_provider
from stardag.testing import InMemoryRegistry
from stardag.testing.modal._tasks import make_range
from tests.test_integration.test_modal._planned import Planned, plan_and_claim


@pytest.fixture(autouse=True)
def fake_call_id(monkeypatch):
    monkeypatch.setattr(modal, "current_function_call_id", lambda: "fc-1")


def _run(runner: Runner, timeout: float | None = None) -> Planned:
    """Plan and claim a task, then run ``runner`` on it as the worker would,
    re-raising whatever escapes; returns the plan for assertions."""
    planned = plan_and_claim(make_range(limit=2))
    env = planned.env()
    if timeout is not None:
        env[STARDAG_MODAL_FUNCTION_TIMEOUT_ENV] = str(timeout)
    planned_box.append(planned)
    with registry_provider.override(planned.registry):
        runner(planned.task, env_overrides=env)
    return planned


planned_box: list[Planned] = []


@pytest.fixture(autouse=True)
def _clear_box():
    planned_box.clear()


def _last() -> Planned:
    return planned_box[-1]


def _runner_raising(exception: BaseException) -> Runner:
    class Raising(Runner):
        def run(self, task):
            raise exception

    return Raising()


def _checkpointed(
    signal: BaseException, *, form: str = "from_none"
) -> sd.ResumableInterruption:
    """A ``ResumableInterruption`` raised the way a task actually raises it.

    ``form`` walks the three shapes a user can write, which differ only in
    what they leave on the raised exception:

    - ``from_none`` — the documented recipe. Clears ``__cause__`` and
      suppresses the traceback's "During handling…" preamble, but keeps
      ``__context__``.
    - ``implicit`` — a plain ``raise`` inside the ``except``. Keeps both
      the context and the preamble.
    - ``explicit`` — ``raise ... from e``. Sets ``__cause__``.

    All three must classify identically; a reader choosing between them is
    choosing traceback cosmetics, not scheduling behaviour.
    """
    try:
        try:
            raise signal
        except BaseException as caught:
            if form == "from_none":
                raise sd.ResumableInterruption("checkpointed") from None
            if form == "explicit":
                raise sd.ResumableInterruption("checkpointed") from caught
            raise sd.ResumableInterruption("checkpointed")
    except sd.ResumableInterruption as request:
        return request


def _runner_checkpointing(signal: BaseException) -> Runner:
    """A runner whose task catches ``signal`` and asks to be resumed — the
    documented recipe, run for real rather than reconstructed."""

    class Checkpointing(Runner):
        def run(self, task):
            try:
                raise signal
            except MODAL_INTERRUPTIONS:
                raise sd.ResumableInterruption("checkpointed") from None

    return Checkpointing()


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


# --- the classifier: what the request was raised *from* -----------------


class TestClassifyByExceptionChain:
    """The axis the clock was standing in for.

    Every case here is a ``ResumableInterruption``, so the first question
    ("did the task ask?") is answered the same way throughout. What varies
    is the signal underneath it, which is the thing that actually decides
    whether a restart is coming.
    """

    @pytest.mark.parametrize("form", ["from_none", "implicit", "explicit"])
    @pytest.mark.parametrize(
        ("signal", "expected"),
        [
            # The backend restarts the same call id. Keep the claim.
            (KeyboardInterrupt(), _PREEMPTION),
            # Nothing restarts a call the platform has finished with,
            # whether it finished by timeout or by an explicit cancel —
            # indistinguishable here, and deliberately not distinguished.
            (InputCancellation("Input was cancelled by user"), _TIMEOUT),
        ],
    )
    def test_the_signal_decides_regardless_of_how_it_was_raised(
        self, signal, expected, form
    ):
        request = _checkpointed(signal, form=form)
        # Both far from the timeout and at it: the clock is not consulted.
        for elapsed in (5.0, 300.0):
            assert (
                _classify_interruption(
                    request, elapsed_seconds=elapsed, function_timeout_seconds=300.0
                )
                == expected
            )

    def test_a_system_exit_on_the_chain_is_not_a_platform_signal(self):
        """``SystemExit`` is not in ``MODAL_INTERRUPTIONS`` — the platform
        does not end an execution with one — so it must not be read off the
        chain as a preemption. Read as one, the runner would translate the
        request back into an interrupt, the backend would restart the input,
        the task would exit the same way, and the loop would repeat ungated
        by ``retries``, because a backend restart spends no attempt. Falling
        through to the clock is bounded."""
        request = _checkpointed(SystemExit())
        assert (
            _classify_interruption(
                request, elapsed_seconds=5.0, function_timeout_seconds=300.0
            )
            == _PREEMPTION  # the clock's answer, not the chain's
        )
        assert (
            _classify_interruption(
                request, elapsed_seconds=300.0, function_timeout_seconds=300.0
            )
            == _TIMEOUT
        )

    def test_the_incident(self):
        """STA-44, to the numbers. A 86400s worker whose timeout fired at
        86392.0s by our clock — 8s short, against a 5s tolerance — read as
        a preemption, reported nothing, and left the task RUNNING for a day
        because no restart was ever coming."""
        request = _checkpointed(InputCancellation("Input was cancelled by user"))
        assert (
            _classify_interruption(
                request,
                elapsed_seconds=86392.0,
                function_timeout_seconds=86400.0,
            )
            == _TIMEOUT
        )

    def test_a_request_raised_outside_the_except_falls_back_to_the_clock(self):
        """The chain is empty when a task raises the request on its own
        initiative — a self-imposed budget, a spot-price check — or when it
        leaves the ``except`` block before raising. Legitimate, and the
        reason the elapsed-time branch is kept rather than deleted."""
        bare = sd.ResumableInterruption("my own deadline")
        assert bare.__context__ is None and bare.__cause__ is None
        assert (
            _classify_interruption(
                bare, elapsed_seconds=5.0, function_timeout_seconds=300.0
            )
            == _PREEMPTION
        )
        assert (
            _classify_interruption(
                bare, elapsed_seconds=300.0, function_timeout_seconds=300.0
            )
            == _TIMEOUT
        )

    def test_an_unrelated_exception_in_the_chain_is_not_a_signal(self):
        """A checkpoint write that fails inside the ``except`` block leaves
        its own exception on the chain. That is not a platform signal and
        must not be read as one — the walk keeps going, and here finds
        nothing, so the clock decides."""
        try:
            try:
                raise ValueError("checkpoint write failed")
            except ValueError:
                raise sd.ResumableInterruption("checkpointed")
        except sd.ResumableInterruption as request:
            assert (
                _classify_interruption(
                    request, elapsed_seconds=5.0, function_timeout_seconds=300.0
                )
                == _PREEMPTION
            )

    def test_an_explicit_cause_does_not_hide_the_context(self):
        """``__cause__`` and ``__context__`` are not two names for one
        chain. A task that raises ``from`` an error of its own — a failed
        checkpoint write, say — still carries the platform signal on
        ``__context__``, and following only the cause walks away from the
        answer."""
        write_error = OSError("checkpoint write failed")
        try:
            try:
                raise InputCancellation("Input was cancelled by user")
            except BaseException:
                raise sd.ResumableInterruption("checkpointed") from write_error
        except sd.ResumableInterruption as request:
            assert request.__cause__ is write_error
            assert isinstance(request.__context__, InputCancellation)
            assert (
                _classify_interruption(
                    request, elapsed_seconds=5.0, function_timeout_seconds=300.0
                )
                == _TIMEOUT
            )

    def test_a_cyclic_chain_terminates(self):
        """A dying container must not spin. The walk is bounded and
        remembers what it has seen, so a chain that loops back on itself
        simply runs out rather than hanging the report."""
        first = RuntimeError("first")
        second = RuntimeError("second")
        first.__context__ = second
        second.__context__ = first
        request = sd.ResumableInterruption("checkpointed")
        request.__context__ = first

        assert (
            _classify_interruption(
                request, elapsed_seconds=5.0, function_timeout_seconds=300.0
            )
            == _PREEMPTION
        )

    def test_the_signal_is_found_past_an_unrelated_link(self):
        """...and when the platform signal *is* further down the chain, it
        still decides. This is the shape a failing checkpoint write
        actually produces: signal, then the write's own error, then the
        request."""
        try:
            try:
                try:
                    raise InputCancellation("Input was cancelled by user")
                except BaseException:
                    raise ValueError("checkpoint write failed")
            except ValueError:
                raise sd.ResumableInterruption("checkpointed") from None
        except sd.ResumableInterruption as request:
            assert (
                _classify_interruption(
                    request, elapsed_seconds=5.0, function_timeout_seconds=300.0
                )
                == _TIMEOUT
            )

    def test_without_a_declared_timeout_a_request_is_still_reported(self):
        """The orchestrator forwards the worker's ``timeout`` only when the
        app declares one, but the backend applies its own default anyway —
        so "unknown" does not mean "no timeout fired".

        Reporting is the recoverable guess: if a restart IS coming the
        scheduler's probe finds the ref live and leaves it alone, whereas
        guessing preemption when no restart is coming strands the task
        until its claim lapses. That asymmetry is the whole reason this
        defaults the way it does."""
        assert (
            _classify_interruption(
                sd.ResumableInterruption("checkpointed"),
                elapsed_seconds=1.0,
                function_timeout_seconds=None,
            )
            == _TIMEOUT
        )

    def test_without_a_declared_timeout_an_uncaught_one_is_still_silent(self):
        """The control. Nobody asked to be resumed, so there is nothing to
        report in either direction."""
        assert (
            _classify_interruption(
                InputCancellation("Input was cancelled by user"),
                elapsed_seconds=1.0,
                function_timeout_seconds=None,
            )
            == _CANCELLATION
        )


# --- what the runner reports, end to end --------------------------------


class TestRunnerReporting:
    """What the worker reports, validated by the fake's seams: an interrupt
    releases the claim and leaves the task INTERRUPTED (actionable); a
    preemption keeps status and claim, pulls the expiry forward, and the
    restart's non-claiming start under the same execution restores it."""

    def test_a_resumption_request_at_the_timeout_is_reported(
        self, default_in_memory_fs_target
    ):
        """The one case that ends the execution: the task asked, and nothing
        else will restart a timed-out call."""
        with pytest.raises(BaseException):
            _run(_runner_raising(sd.ResumableInterruption("checkpointed")), 0.0001)

        planned = _last()
        assert planned.reports() == ["member_start", "member_interrupt"]
        (interrupt,) = planned.registry.calls_to("member_interrupt")
        assert interrupt["execution_id"] == planned.execution_id
        assert "resumed" in interrupt["error_message"]
        assert planned.registry.status_of(planned.task.id) == "interrupted"
        assert planned.registry.executions[planned.execution_id].outcome == (
            "interrupted"
        )

    def test_a_preemption_records_the_preemption_and_gets_out_of_the_way(
        self, default_in_memory_fs_target
    ):
        """The backend restarts the input on the same call id, keeping the
        claim — so a terminal event would be worse. The preemption record
        releases nothing: it makes a restart that never arrives visible."""
        with pytest.raises(KeyboardInterrupt) as caught:
            _run(_runner_raising(sd.ResumableInterruption("checkpointed")), 600.0)

        # Translated on the way out: an ordinary Exception leaving the
        # container is a task failure Modal will not restart.
        assert isinstance(caught.value.__cause__, sd.ResumableInterruption)
        planned = _last()
        assert planned.reports() == ["member_start", "member_preempt"]
        task_row = planned.registry.tasks[planned.task_id]
        assert task_row.status == "running"
        assert task_row.execution_id == planned.execution_id
        assert task_row.preempted_at is not None
        assert planned.registry.executions[planned.execution_id].ended_at is None

    def test_the_restart_after_a_preemption_reports_under_the_same_execution(
        self, default_in_memory_fs_target
    ):
        """Not an end: the restarted input carries the same env, so its
        start names the same execution — which restores the claim's TTL —
        and its completion lands."""
        with pytest.raises(KeyboardInterrupt):
            _run(_runner_raising(sd.ResumableInterruption("checkpointed")), 600.0)
        planned = _last()

        preempted_expiry = planned.registry.tasks[planned.task_id].claim_expires_at
        assert preempted_expiry is not None
        env = {**planned.env(), STARDAG_CLAIM_TTL_SECONDS_ENV: "5000"}
        restarted_at = planned.registry.now()

        class Restart(Runner):
            def run(self, task):
                # Observed mid-run: the restart's start restored the claim to
                # the forwarded TTL, not the registry default.
                row = planned.registry.tasks[planned_task_id]
                restored.append(row.claim_expires_at)
                return super().run(task)

        planned_task_id = planned.task_id
        restored: list = []
        with registry_provider.override(planned.registry):
            assert Restart()(planned.task, env_overrides=env) is None

        (expiry,) = restored
        assert expiry > preempted_expiry
        assert (expiry - restarted_at).total_seconds() == pytest.approx(5000, abs=5)
        assert planned.registry.tasks[planned.task_id].preempted_at is None
        assert planned.registry.status_of(planned.task.id) == "completed"
        starts = planned.registry.calls_to(
            "member_start", task_id=planned.task_id, claim=False
        )
        assert [s["execution_id"] for s in starts] == [planned.execution_id] * 2

    def test_the_documented_recipe_reports_a_timeout_not_a_preemption(
        self, default_in_memory_fs_target
    ):
        """STA-44 end to end, through the documented recipe: the declared
        timeout is far from elapsed, but the InputCancellation on the chain
        says no restart is coming."""
        with pytest.raises(BaseException):
            _run(
                _runner_checkpointing(InputCancellation("Input was cancelled")), 86400.0
            )

        assert _last().reports() == ["member_start", "member_interrupt"]

    def test_the_documented_recipe_on_a_real_preemption(
        self, default_in_memory_fs_target
    ):
        """The control: same recipe, same timing, the other signal."""
        with pytest.raises(KeyboardInterrupt):
            _run(_runner_checkpointing(KeyboardInterrupt()), 86400.0)

        assert _last().reports() == ["member_start", "member_preempt"]

    @pytest.mark.parametrize(
        "exception",
        [KeyboardInterrupt(), InputCancellation("Input was cancelled by user")],
    )
    @pytest.mark.parametrize("timeout", [0.0001, 600.0])
    def test_an_uncaught_interruption_never_reports(
        self, default_in_memory_fs_target, exception, timeout
    ):
        """A task that did not ask to be resumed does not get resumed; its
        dead execution ends as a failure once the claim lapses."""
        with pytest.raises(BaseException):
            _run(_runner_raising(exception), timeout)

        planned = _last()
        assert planned.reports() == ["member_start"]
        assert planned.registry.status_of(planned.task.id) == "running"

    def test_an_ordinary_exception_still_fails(self, default_in_memory_fs_target):
        with pytest.raises(RuntimeError):
            _run(_runner_raising(RuntimeError("genuine bug")), 600.0)

        planned = _last()
        assert planned.reports() == ["member_start", "member_fail"]
        (fail,) = planned.registry.calls_to("member_fail")
        assert "genuine bug" in fail["error_message"]

    def test_the_reason_never_names_a_timeout_it_does_not_know(
        self, default_in_memory_fs_target
    ):
        """The reason lands in a user-visible message, so it must not read
        "the worker function's Nones timeout"."""
        with pytest.raises(BaseException):
            _run(_runner_raising(sd.ResumableInterruption("checkpointed")), 0.0001)

        (interrupt,) = _last().registry.calls_to("member_interrupt")
        assert "None" not in interrupt["error_message"]
        assert "0.0001s" in interrupt["error_message"]

    def test_no_reporter_still_translates_the_escape(self, default_in_memory_fs_target):
        """Without forwarded ids there is nothing to report to — but the
        escape translation is what earns the backend restart."""
        registry = InMemoryRegistry()
        runner = _runner_raising(sd.ResumableInterruption("checkpointed"))

        with registry_provider.override(registry):
            with pytest.raises(KeyboardInterrupt):
                runner(
                    make_range(limit=2),
                    env_overrides={STARDAG_MODAL_FUNCTION_TIMEOUT_ENV: "600"},
                )

        assert registry.calls == []
