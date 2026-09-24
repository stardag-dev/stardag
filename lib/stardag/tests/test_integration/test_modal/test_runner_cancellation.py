"""The worker's two automatic checkpoints, and what a cancelled one does.

A cancel does not reach into the container. It releases the build's claims;
this is the other half — the container asking, at points where stopping is
safe, whether its execution is still the one the task is waiting for (one
read of the build's unended executions, ``GET /builds/{id}/executions``),
and stopping cleanly when the answer is no.

"Cleanly" is precise and each part of it is tested here: **no output
written, no completion reported, no end-of-attempt event, and not a
normal return** — a backend call that returns successfully with no output
would read to a scheduler's probe as a completion. So the checkpoint
raises.
"""

from __future__ import annotations

import pytest

try:
    import modal
except ImportError:
    pytest.skip("Skipping modal tests (import not available)", allow_module_level=True)

import stardag as sd
from stardag.build._registration import new_id
from stardag.cancellation import CHECK_INTERVAL_ENV
from stardag.exceptions import ExecutionCancelled
from stardag.integration.modal._runner import Runner
from stardag.registry import registry_provider
from stardag.testing import InMemoryRegistry
from stardag.testing.modal._tasks import SyncDynamicRangeSumTask, make_range
from tests.test_integration.test_modal._planned import Planned, plan_and_claim

_END_REPORTS = ("member_complete", "member_fail", "member_interrupt", "member_yield")


@pytest.fixture(autouse=True)
def fake_call_id(monkeypatch):
    monkeypatch.setattr(modal, "current_function_call_id", lambda: "fc-worker-call-1")


@pytest.fixture(autouse=True)
def no_throttle(monkeypatch):
    """Every checkpoint asks, so a test never passes on a cached answer."""
    monkeypatch.setenv(CHECK_INTERVAL_ENV, "0")


def _run(planned: Planned, runner: Runner | None = None):
    with registry_provider.override(planned.registry):
        return (runner or Runner())(planned.task, env_overrides=planned.env())


def _cancel_after(planned: Planned, reads: int) -> None:
    """Cancel the build once the worker has read its executions ``reads``
    times: the first checkpoint(s) pass, the next one sees the release."""
    registry = planned.registry
    original = registry.build_list_executions
    count = [0]

    def reading(build_id, *, not_in_current_plan=False):
        count[0] += 1
        if count[0] > reads and registry.builds[build_id].status == "running":
            registry.build_cancel(build_id)
        return original(build_id, not_in_current_plan=not_in_current_plan)

    registry.build_list_executions = reading  # type: ignore[method-assign]


def _assert_stopped_cleanly(planned: Planned) -> None:
    assert not planned.task.complete(), "the cancelled execution wrote its output"
    for method in _END_REPORTS:
        assert not planned.registry.called(method, task_id=planned.task_id), method


class TestTheIdentityReachesTheWorker:
    def test_the_start_names_the_forwarded_execution(self, default_in_memory_fs_target):
        planned = plan_and_claim(make_range(limit=3))

        _run(planned)

        (start,) = planned.registry.calls_to(
            "member_start", task_id=planned.task_id, claim=False
        )
        assert start["execution_id"] == planned.execution_id


class TestCheckpointOne:
    def test_a_cancelled_build_stops_before_run(self, default_in_memory_fs_target):
        """Catches a cancel that landed between the spawn and the start —
        the common case on a queued fan-out. The released claim makes the
        start itself refused, which is the checkpoint."""
        planned = plan_and_claim(make_range(limit=3))
        planned.registry.build_cancel(planned.build_id)

        with pytest.raises(ExecutionCancelled):
            _run(planned)

        _assert_stopped_cleanly(planned)

    def test_the_refused_start_is_the_checkpoint_and_costs_no_extra_call(
        self, default_in_memory_fs_target
    ):
        """A start naming an execution whose claim was taken over is refused
        (``execution_not_current``): that refusal *is* "you are no longer
        wanted", so the checkpoint reads it instead of asking again."""
        planned = plan_and_claim(make_range(limit=3))
        planned.registry.build_cancel(planned.build_id)

        with pytest.raises(ExecutionCancelled):
            _run(planned)

        assert not planned.registry.called("build_list_executions"), (
            "the worker asked a question the refusal had already answered"
        )

    def test_an_unknown_execution_is_not_wanted(self, default_in_memory_fs_target):
        planned = plan_and_claim(make_range(limit=3))
        planned.execution_id = new_id()  # never started by anybody

        with pytest.raises(ExecutionCancelled):
            _run(planned)

        _assert_stopped_cleanly(planned)

    def test_a_live_execution_runs(self, default_in_memory_fs_target):
        planned = plan_and_claim(make_range(limit=3))

        assert _run(planned) is None

        assert planned.registry.called("build_list_executions")
        assert planned.registry.status_of(planned.task.id) == "completed"

    def test_an_unrelated_start_failure_is_not_a_cancellation(
        self, default_in_memory_fs_target
    ):
        """A registry that is down carries no information about whether
        this execution is wanted, so it must not stop the task."""
        planned = plan_and_claim(make_range(limit=3))

        def down(*args, **kwargs):
            raise ConnectionError("registry down")

        planned.registry.member_start = down  # type: ignore[method-assign]
        planned.registry.build_list_executions = down  # type: ignore[method-assign]

        assert _run(planned) is None
        assert planned.task.complete()

    def test_a_registry_that_cannot_answer_lets_the_worker_run(
        self, default_in_memory_fs_target
    ):
        """The fail-open invariant: a registry without the read (a custom
        one) has no opinion."""

        class NoExecutionsRead(InMemoryRegistry):
            def build_list_executions(self, build_id, *, not_in_current_plan=False):
                raise NotImplementedError

        planned = plan_and_claim(make_range(limit=3), NoExecutionsRead())

        assert _run(planned) is None
        assert planned.task.complete()

    def test_a_worker_that_reports_nothing_has_no_checkpoints(
        self, default_in_memory_fs_target
    ):
        """``report_lifecycle=False``: a resident orchestrator reports, and
        holds its own handles, so the container never needs to ask."""
        planned = plan_and_claim(make_range(limit=3))
        planned.registry.build_cancel(planned.build_id)

        assert _run(planned, Runner(report_lifecycle=False)) is None

        assert planned.reports() == []
        assert not planned.registry.called("build_list_executions")


class TestCheckpointTwo:
    def test_a_cancel_lands_at_a_dynamic_dependency_yield(
        self, default_in_memory_fs_target
    ):
        """The yield is where a worker is about to register children and
        suspend; a build that has stopped should not pay for a new layer.
        The cancel arrives after the start-of-attempt read, so only the
        yield's checkpoint can catch it."""
        planned = plan_and_claim(SyncDynamicRangeSumTask(limit=3))
        _cancel_after(planned, reads=1)

        with pytest.raises(ExecutionCancelled):
            _run(planned)

        _assert_stopped_cleanly(planned)
        assert not planned.registry.called("member_yield")

    def test_a_live_build_passes_straight_through_the_yield(
        self, default_in_memory_fs_target
    ):
        planned = plan_and_claim(SyncDynamicRangeSumTask(limit=3))

        assert _run(planned) is not None, "the task should have suspended"

        assert planned.registry.status_of(planned.task.id) == "suspended"


class TestTheUserFacingHelper:
    def test_a_task_can_ask_and_stop_where_it_knows_it_is_safe(
        self, default_in_memory_fs_target
    ):
        """The opt-in for a long ``run()`` body — deliberately not a
        background thread raising into the task, which could interrupt a
        write halfway."""
        asked: list[bool] = []

        class AskingRunner(Runner):
            def run(self, task):
                asked.append(sd.cancellation_requested())
                if asked[-1]:
                    raise ExecutionCancelled("stopping at a safe point")
                return super().run(task)

        planned = plan_and_claim(make_range(limit=3))
        _cancel_after(planned, reads=1)

        with pytest.raises(ExecutionCancelled):
            _run(planned, AskingRunner())

        assert asked == [True]
        _assert_stopped_cleanly(planned)
