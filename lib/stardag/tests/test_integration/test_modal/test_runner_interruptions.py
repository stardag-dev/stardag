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
        execution_id=None,
    ) -> None:
        self.calls.append(("task_start", {"executor_ref": executor_ref}))

    def task_complete(self, build_id, task) -> None:
        self.calls.append(("task_complete", {}))

    def task_fail(self, build_id, task, error_message=None) -> None:
        self.calls.append(("task_fail", {"error_message": error_message}))

    def task_interrupt(
        self, build_id, task, reason=None, executor_ref=None, execution_id=None
    ) -> None:
        self.calls.append(
            ("task_interrupt", {"reason": reason, "executor_ref": executor_ref})
        )

    def task_preempt(
        self, build_id, task, reason=None, executor_ref=None, execution_id=None
    ) -> None:
        self.calls.append(
            ("task_preempt", {"reason": reason, "executor_ref": executor_ref})
        )

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

    def test_a_preemption_records_the_preemption_and_gets_out_of_the_way(
        self, registry, default_in_memory_fs_target
    ):
        """The backend restarts the input on the same call id, faster than a
        reschedule and keeping the claim — so a *terminal* event would be
        worse. Recording the preemption is not terminal and releases
        nothing: it is what makes a restart that never arrives visible."""
        runner = _runner_raising(sd.ResumableInterruption("checkpointed"))

        with pytest.raises(KeyboardInterrupt) as caught:
            runner(make_range(limit=2), env_overrides=_env(uuid4(), timeout=600.0))

        # Translated on the way out: an ordinary Exception leaving the
        # container is a task failure Modal will not restart.
        assert isinstance(caught.value.__cause__, sd.ResumableInterruption)
        assert registry.methods() == ["task_start", "task_preempt"]
        assert "preempted" in registry.calls[1][1]["reason"]

    def test_the_documented_recipe_reports_a_timeout_not_a_preemption(
        self, registry, default_in_memory_fs_target
    ):
        """STA-44 end to end, through the recipe the docs actually tell
        people to write. The declared timeout is far from elapsed, so the
        old clock-based rule called this a preemption and wrote nothing;
        the InputCancellation on the chain says otherwise."""
        runner = _runner_checkpointing(InputCancellation("Input was cancelled"))

        with pytest.raises(BaseException):
            runner(make_range(limit=2), env_overrides=_env(uuid4(), timeout=86400.0))

        assert registry.methods() == ["task_start", "task_interrupt"]

    def test_the_documented_recipe_on_a_real_preemption(
        self, registry, default_in_memory_fs_target
    ):
        """The control for the test above: same recipe, same timing, the
        other signal — and the opposite answer."""
        runner = _runner_checkpointing(KeyboardInterrupt())

        with pytest.raises(KeyboardInterrupt):
            runner(make_range(limit=2), env_overrides=_env(uuid4(), timeout=86400.0))

        assert registry.methods() == ["task_start", "task_preempt"]

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
        # Raised bare, so the chain says nothing and the clock decides;
        # a tiny declared timeout puts it on the branch that reports, which
        # is where the reason string is built.
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
        conditional on reporting being configured.

        A long declared timeout keeps this on the preemption branch; the
        translation is what is under test, not the classification."""
        runner = _runner_raising(sd.ResumableInterruption("checkpointed"))

        with pytest.raises(KeyboardInterrupt):
            runner(
                make_range(limit=2),
                env_overrides={STARDAG_MODAL_FUNCTION_TIMEOUT_ENV: "600"},
            )

        assert registry.calls == []
