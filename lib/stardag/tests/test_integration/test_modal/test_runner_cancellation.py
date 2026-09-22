"""The worker's two automatic checkpoints, and what a cancelled one does.

A cancel does not reach into the container. It marks the build and
releases its claims; this is the other half — the container asking, at
points where stopping is safe, and stopping cleanly when the answer is
no.

"Cleanly" is precise and each part of it is tested here: **no output
written, no completion reported, no end-of-attempt event, and not a
normal return**. The last one is the least obvious and the most
important. A backend call that returns successfully with no output is
read by a scheduler's probe as "the worker wrote it, eventual
consistency" and recorded as a completion — a completion for a target
that does not exist. So the checkpoint raises.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

try:
    import modal  # noqa: F401
except ImportError:
    pytest.skip("Skipping modal tests (import not available)", allow_module_level=True)

from stardag.cancellation import CHECK_INTERVAL_ENV
from stardag.exceptions import APIError, ExecutionCancelled
from stardag.integration.modal._metadata import (
    STARDAG_BUILD_ID_ENV,
    STARDAG_EXECUTION_ID_ENV,
)
from stardag.integration.modal._runner import Runner
from stardag.registry import ExecutionStatus, NoOpRegistry, registry_provider
from stardag.testing.modal._tasks import SyncDynamicRangeSumTask, make_range

WORKER_CALL_ID = "fc-worker-call-1"


class CancellingRegistry(NoOpRegistry):
    """A registry that answers the one question, and records the asking."""

    def __init__(
        self,
        *,
        status: ExecutionStatus | None = None,
        start_error: Exception | None = None,
    ) -> None:
        super().__init__()
        self.status = status or ExecutionStatus()
        self.start_error = start_error
        self.calls: list[str] = []
        self.asked: list[UUID | None] = []
        self.started_with: list[UUID | None] = []

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
        self.calls.append("task_start")
        self.started_with.append(execution_id)
        if self.start_error is not None:
            raise self.start_error

    def task_complete(self, build_id, task) -> None:
        self.calls.append("task_complete")

    def task_suspend(self, build_id, task) -> None:
        self.calls.append("task_suspend")

    def task_fail(self, build_id, task, error_message=None) -> None:
        self.calls.append("task_fail")

    def task_interrupt(
        self, build_id, task, reason=None, executor_ref=None, execution_id=None
    ) -> None:
        self.calls.append("task_interrupt")

    def task_add_dependencies(
        self, build_id, task, upstream_tasks, is_dynamic=True, *, scope_key=None
    ) -> None:
        self.calls.append("task_add_dependencies")

    def execution_status(self, build_id, task, execution_id=None) -> ExecutionStatus:
        self.calls.append("execution_status")
        self.asked.append(execution_id)
        return self.status


@pytest.fixture
def fake_call_id(monkeypatch):
    monkeypatch.setattr(modal, "current_function_call_id", lambda: WORKER_CALL_ID)
    return WORKER_CALL_ID


@pytest.fixture(autouse=True)
def no_throttle(monkeypatch):
    """Every checkpoint asks, so a test never passes on a cached answer."""
    monkeypatch.setenv(CHECK_INTERVAL_ENV, "0")


def _env(build_id: UUID, execution_id: UUID | None = None) -> dict[str, str]:
    env = {STARDAG_BUILD_ID_ENV: str(build_id)}
    if execution_id is not None:
        env[STARDAG_EXECUTION_ID_ENV] = str(execution_id)
    return env


def _cancelled(reason: str = "build_not_running") -> ExecutionStatus:
    return ExecutionStatus(still_current=False, reason=reason, build_status="cancelled")


class TestTheIdentityReachesTheWorker:
    def test_the_start_names_the_forwarded_execution(
        self, fake_call_id, default_in_memory_fs_target
    ):
        """Without this the worker's self-report cannot be matched to the
        claim, and both rules the identity buys fall back to the ref."""
        registry = CancellingRegistry()
        build_id, execution_id = uuid4(), uuid4()

        with registry_provider.override(registry):
            Runner()(make_range(limit=3), env_overrides=_env(build_id, execution_id))

        assert registry.started_with == [execution_id]

    def test_a_malformed_identity_is_dropped_rather_than_fatal(
        self, fake_call_id, default_in_memory_fs_target, caplog
    ):
        """No worker should fail to report its own start over an env var.

        Dropping it costs only the identity-based rules, which is how a
        worker behaved before they existed.
        """
        registry = CancellingRegistry()
        build_id = uuid4()
        env = _env(build_id)
        env[STARDAG_EXECUTION_ID_ENV] = "not-a-uuid"

        with registry_provider.override(registry):
            result = Runner()(make_range(limit=3), env_overrides=env)

        assert result is None
        assert registry.started_with == [None]
        assert "task_complete" in registry.calls


class TestCheckpointOne:
    def test_a_cancelled_build_stops_before_run(
        self, fake_call_id, default_in_memory_fs_target
    ):
        """Catches a cancel that landed between the spawn and the start —
        the common case on a queued fan-out, where a container may sit
        in a backend queue for minutes."""
        registry = CancellingRegistry(status=_cancelled())
        task = make_range(limit=3)

        with registry_provider.override(registry):
            with pytest.raises(ExecutionCancelled):
                Runner()(task, env_overrides=_env(uuid4(), uuid4()))

        assert not task.complete(), "the cancelled execution wrote its output"
        assert "task_complete" not in registry.calls
        assert "task_fail" not in registry.calls
        assert "task_interrupt" not in registry.calls

    def test_the_refused_start_is_the_checkpoint_and_costs_no_extra_call(
        self, fake_call_id, default_in_memory_fs_target
    ):
        """The cheapest version of the checkpoint there is.

        A worker's own start is non-claiming, and the registry refuses one
        naming a superseded execution. That 409 *is* "you are no longer
        wanted", arriving inside a request the worker was making anyway —
        so the checkpoint reads it instead of asking again.
        """
        registry = CancellingRegistry(
            start_error=APIError(
                "superseded",
                status_code=409,
                payload={"error_code": "execution_superseded"},
            )
        )

        with registry_provider.override(registry):
            with pytest.raises(ExecutionCancelled):
                Runner()(make_range(limit=3), env_overrides=_env(uuid4(), uuid4()))

        assert registry.calls == ["task_start"], (
            "the worker asked a question the 409 had already answered"
        )

    def test_an_unrelated_start_failure_is_not_a_cancellation(
        self, fake_call_id, default_in_memory_fs_target
    ):
        """Lifecycle reporting stays best-effort for everything else.

        A registry that is down must not stop a task that is about to run
        perfectly well — the failure carries no information about whether
        this execution is wanted.
        """
        registry = CancellingRegistry(start_error=ConnectionError("registry down"))
        task = make_range(limit=3)

        with registry_provider.override(registry):
            result = Runner()(task, env_overrides=_env(uuid4(), uuid4()))

        assert result is None
        assert task.complete()

    def test_a_registry_that_cannot_answer_lets_the_worker_run(
        self, fake_call_id, default_in_memory_fs_target
    ):
        """The invariant, from the worker's side.

        ``NoOpRegistry`` has no opinion, which is what a custom registry
        and a server predating the endpoint both look like from here.
        """
        task = make_range(limit=3)

        with registry_provider.override(NoOpRegistry()):
            result = Runner()(task, env_overrides=_env(uuid4(), uuid4()))

        assert result is None
        assert task.complete()

    def test_a_worker_that_reports_nothing_has_no_checkpoints(
        self, fake_call_id, default_in_memory_fs_target
    ):
        """``report_lifecycle=False`` means a resident orchestrator is
        doing the reporting — and a resident orchestrator holds its own
        handles, so it never needed the container to ask."""
        registry = CancellingRegistry(status=_cancelled())
        task = make_range(limit=3)

        with registry_provider.override(registry):
            result = Runner(report_lifecycle=False)(
                task, env_overrides=_env(uuid4(), uuid4())
            )

        assert result is None
        assert registry.calls == []


class TestCheckpointTwo:
    def test_a_cancel_lands_at_a_dynamic_dependency_yield(
        self, fake_call_id, default_in_memory_fs_target
    ):
        """The yield is where a worker is about to register children and
        suspend, so a build that has stopped should not pay for a whole
        new layer of the DAG.

        The cancel is made to arrive *after* the start, so the first
        checkpoint passes and only the yield can catch it — which is what
        makes this a test of the second checkpoint rather than the first.
        """
        answers = [ExecutionStatus(), _cancelled()]
        registry = CancellingRegistry()

        def answer(build_id, task, execution_id=None):
            registry.calls.append("execution_status")
            return answers.pop(0) if answers else _cancelled()

        registry.execution_status = answer  # type: ignore[method-assign]
        task = SyncDynamicRangeSumTask(limit=3)

        with registry_provider.override(registry):
            with pytest.raises(ExecutionCancelled):
                Runner()(task, env_overrides=_env(uuid4(), uuid4()))

        assert "task_add_dependencies" not in registry.calls, (
            "a cancelled build still registered a layer of children"
        )
        assert "task_suspend" not in registry.calls
        assert not task.complete()

    def test_a_live_build_passes_straight_through_the_yield(
        self, fake_call_id, default_in_memory_fs_target
    ):
        registry = CancellingRegistry()
        task = SyncDynamicRangeSumTask(limit=3)

        with registry_provider.override(registry):
            result = Runner()(task, env_overrides=_env(uuid4(), uuid4()))

        assert result is not None, "the task should have suspended on its deps"
        assert "task_suspend" in registry.calls


class TestTheUserFacingHelper:
    def test_a_task_can_ask_and_stop_where_it_knows_it_is_safe(
        self, fake_call_id, default_in_memory_fs_target
    ):
        """The opt-in for a long ``run()`` body.

        Deliberately not a background thread raising into the task: that
        can interrupt a write halfway, which is the one thing
        content-addressed targets exist to prevent.
        """
        import stardag as sd

        asked: list[bool] = []

        class AskingRunner(Runner):
            def run(self, task):
                asked.append(sd.cancellation_requested())
                if asked[-1]:
                    raise ExecutionCancelled("stopping at a safe point")
                return super().run(task)

        # The cancel lands after the start, so checkpoint one passes and
        # only the task's own ask can catch it.
        answers = [ExecutionStatus(), _cancelled("superseded")]
        registry = CancellingRegistry()
        registry.execution_status = (  # type: ignore[method-assign]
            lambda build_id, task, execution_id=None: (
                answers.pop(0) if answers else _cancelled("superseded")
            )
        )
        task = make_range(limit=3)

        with registry_provider.override(registry):
            with pytest.raises(ExecutionCancelled):
                AskingRunner()(task, env_overrides=_env(uuid4(), uuid4()))

        assert asked == [True]
        assert not task.complete()
        assert "task_complete" not in registry.calls
