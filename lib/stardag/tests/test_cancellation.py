"""The cancellation checker, and the one property it must never break.

    A worker exits only on **positive evidence** that it is no longer
    wanted.

Everything here is a test of that invariant from one side or the other.
The throttle and the stickiness are performance and coherence; the
fail-open behaviour is the correctness property, and it is the one that
would be quietly lost by a refactor that "simplified" the error handling.
"""

from __future__ import annotations

import pytest

from stardag.cancellation import (
    CHECK_INTERVAL_ENV,
    DEFAULT_CHECK_INTERVAL_SECONDS,
    CancellationChecker,
    cancellation_requested,
    cancellation_scope,
    current_checker,
)
from stardag.exceptions import ExecutionCancelled


def _counting(answers: list[bool]):
    """An ``ask`` that returns each answer in turn, counting its calls."""
    calls: list[int] = []

    def ask() -> bool:
        calls.append(len(calls))
        return answers[min(len(calls) - 1, len(answers) - 1)]

    return ask, calls


class TestTheAnswer:
    def test_a_no_keeps_the_worker_running(self):
        ask, _ = _counting([False])
        assert CancellationChecker(ask, min_interval_seconds=0).requested() is False

    def test_a_yes_stops_it(self):
        ask, _ = _counting([True])
        assert CancellationChecker(ask, min_interval_seconds=0).requested() is True

    def test_a_yes_is_sticky(self):
        """Nothing recovers a task from another holder.

        Re-asking after a yes could only produce a "no" from a transport
        failure, which would restart a worker the build has stopped
        waiting for — the exact opposite of what the fail-open rule is
        protecting.
        """
        ask, calls = _counting([True, False])
        checker = CancellationChecker(ask, min_interval_seconds=0)

        assert checker.requested() is True
        assert checker.requested() is True
        assert len(calls) == 1, "a settled answer was re-asked"

    def test_an_unasked_answer_counts(self):
        """The 409 on the worker's own start is this question, answered.

        It arrives inside a request the worker was making anyway, which
        is what makes the start-of-attempt checkpoint free.
        """
        ask, calls = _counting([False])
        checker = CancellationChecker(ask, min_interval_seconds=0)

        checker.note_cancelled("the registry refused its start")

        assert checker.requested() is True
        assert calls == [], "the registry was asked a question already answered"


class TestTheThrottle:
    def test_a_recent_no_is_reused(self):
        ask, calls = _counting([False, True])
        checker = CancellationChecker(ask, min_interval_seconds=3600)

        assert checker.requested() is False
        assert checker.requested() is False

        assert len(calls) == 1, (
            "a generator yielding in a loop would put one request per "
            "iteration on the registry"
        )

    def test_force_skips_it(self):
        """The once-per-attempt checkpoint wants a fresh answer.

        There is nothing to throttle at that point, and a stale "no"
        carried over from a previous task in the same container would be
        answering about the wrong execution.
        """
        ask, calls = _counting([False, True])
        checker = CancellationChecker(ask, min_interval_seconds=3600)

        assert checker.requested() is False
        assert checker.requested(force=True) is True
        assert len(calls) == 2

    def test_the_interval_is_configurable(self, monkeypatch):
        monkeypatch.setenv(CHECK_INTERVAL_ENV, "7.5")
        assert CancellationChecker(lambda: False)._min_interval == 7.5

    @pytest.mark.parametrize("raw", ["not-a-number", "", "  "])
    def test_an_unreadable_interval_falls_back(self, monkeypatch, raw):
        monkeypatch.setenv(CHECK_INTERVAL_ENV, raw)
        checker = CancellationChecker(lambda: False)
        assert checker._min_interval == DEFAULT_CHECK_INTERVAL_SECONDS

    def test_a_negative_interval_is_clamped_rather_than_raised(self, monkeypatch):
        """Clamping beats raising inside a worker.

        A misconfigured env var must not be the reason a task fails
        before it runs.
        """
        monkeypatch.setenv(CHECK_INTERVAL_ENV, "-5")
        assert CancellationChecker(lambda: False)._min_interval == 0.0


class TestRaising:
    def test_raise_if_cancelled_is_quiet_on_a_no(self):
        CancellationChecker(lambda: False, min_interval_seconds=0).raise_if_cancelled(
            "start of attempt"
        )

    def test_raise_if_cancelled_names_the_checkpoint(self):
        checker = CancellationChecker(lambda: True, min_interval_seconds=0)
        with pytest.raises(ExecutionCancelled) as raised:
            checker.raise_if_cancelled("dynamic-dependency yield")
        assert "dynamic-dependency yield" in str(raised.value)


class TestTheAmbientScope:
    def test_the_helper_answers_false_outside_a_worker(self):
        """``run()`` called from a test, a notebook or the resident
        engine's in-process path has no checker, and asking must not be
        an error there."""
        assert current_checker() is None
        assert cancellation_requested() is False

    def test_the_helper_reads_the_installed_checker(self):
        checker = CancellationChecker(lambda: True, min_interval_seconds=0)
        with cancellation_scope(checker):
            assert cancellation_requested() is True
        assert cancellation_requested() is False

    def test_the_scope_restores_what_it_replaced(self):
        """A container serves many inputs, and the resident engine's pools
        drive several tasks at once; a leaked checker would have one
        task asking about another's execution."""
        outer = CancellationChecker(lambda: False, min_interval_seconds=0)
        inner = CancellationChecker(lambda: True, min_interval_seconds=0)
        with cancellation_scope(outer):
            with cancellation_scope(inner):
                assert current_checker() is inner
            assert current_checker() is outer


class TestFailOpen:
    def test_an_ask_that_raises_is_not_a_stop(self):
        """The invariant, at the seam where it is easiest to lose.

        ``CancellationChecker`` does not swallow this itself — the
        callable it is given is required to, because that is where the
        registry actually is and where "the server said no" can be told
        from "nothing answered". This test pins the contract from the
        caller's side: a checker built the way the worker builds it never
        turns a failure into a stop.
        """

        def ask() -> bool:
            raise ConnectionError("registry unreachable")

        checker = CancellationChecker(lambda: _swallowing(ask), min_interval_seconds=0)
        assert checker.requested() is False


def _swallowing(ask) -> bool:
    try:
        return ask()
    except Exception:
        return False
