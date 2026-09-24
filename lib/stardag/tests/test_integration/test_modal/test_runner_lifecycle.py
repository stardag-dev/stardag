"""Worker-side lifecycle reporting in ``Runner``, against a real plan.

No Modal account needed — ``modal.current_function_call_id`` is faked and
the registry is :class:`~stardag.testing.InMemoryRegistry` with the task
planned and claimed (``_planned.plan_and_claim``), so every report is
checked by the fake's server seams: it names the claim's execution, goes
through the plan holding the claim, and a yield carries the plan's
deployment. The worker reports its non-claiming start (with its call id as
executor ref), then completed (+ artifacts), a yield that suspends, or
failed — and stays silent when nothing was forwarded, the registry is
NoOp, or reporting is switched off.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

try:
    import modal
except ImportError:
    pytest.skip("Skipping modal tests (import not available)", allow_module_level=True)

from stardag.build._deployment import STARDAG_DEPLOYMENT_ID_ENV
from stardag.integration.modal._metadata import (
    MODAL_EXECUTOR_NAME,
    STARDAG_BUILD_ID_ENV,
    STARDAG_CLAIM_TTL_SECONDS_ENV,
    STARDAG_EXECUTION_ID_ENV,
    STARDAG_PLAN_ID_ENV,
)
from stardag.integration.modal._reporter import _WorkerLifecycleReporter
from stardag.integration.modal._runner import Runner
from stardag.registry import BuildNotifyResult, NoOpRegistry, registry_provider
from stardag.testing import InMemoryRegistry
from stardag.testing.modal._tasks import SyncDynamicRangeSumTask, make_range
from tests.test_integration.test_modal._planned import Planned, plan_and_claim

WORKER_CALL_ID = "fc-worker-call-1"


@pytest.fixture(autouse=True)
def fake_call_id(monkeypatch):
    monkeypatch.setattr(modal, "current_function_call_id", lambda: WORKER_CALL_ID)
    return WORKER_CALL_ID


def _run(
    planned: Planned,
    runner: Runner | None = None,
    *,
    reactive: bool = False,
    extra_env: dict[str, str] | None = None,
):
    env = planned.env(reactive=reactive, **(extra_env or {}))
    with registry_provider.override(planned.registry):
        return (runner or Runner())(planned.task, env_overrides=env)


def _boom_runner(message: str = "task exploded") -> Runner:
    class Boom(Exception):
        pass

    class FailingRunner(Runner):
        def run(self, task):
            raise Boom(message)

    return FailingRunner()


class TestRunnerLifecycleReporting:
    def test_success_reports_start_and_complete(self, default_in_memory_fs_target):
        planned = plan_and_claim(make_range(limit=3))

        assert _run(planned) is None

        assert planned.task.complete()
        assert planned.reports() == ["member_start", "member_complete"]
        (start,) = planned.registry.calls_to(
            "member_start", task_id=planned.task_id, claim=False
        )
        assert start["plan_id"] == planned.plan_id
        assert start["execution_id"] == planned.execution_id
        assert start["executor"] == MODAL_EXECUTOR_NAME
        assert start["executor_ref"] == WORKER_CALL_ID
        assert start["executor_metadata"]["kind"] == MODAL_EXECUTOR_NAME
        execution = planned.registry.executions[planned.execution_id]
        assert execution.executor_ref == WORKER_CALL_ID
        assert execution.outcome == "completed"
        assert planned.registry.status_of(planned.task.id) == "completed"

    def test_the_start_carries_the_forwarded_claim_ttl(
        self, default_in_memory_fs_target
    ):
        planned = plan_and_claim(make_range(limit=3))

        _run(planned, extra_env={STARDAG_CLAIM_TTL_SECONDS_ENV: "1500"})

        (start,) = planned.registry.calls_to(
            "member_start", task_id=planned.task_id, claim=False
        )
        assert start["claim_ttl_seconds"] == 1500

    @pytest.mark.parametrize("raw", ["soon", "0", "-5"])
    def test_a_malformed_claim_ttl_is_ignored_not_raised(
        self, default_in_memory_fs_target, raw
    ):
        planned = plan_and_claim(make_range(limit=3))

        assert _run(planned, extra_env={STARDAG_CLAIM_TTL_SECONDS_ENV: raw}) is None

        (start,) = planned.registry.calls_to(
            "member_start", task_id=planned.task_id, claim=False
        )
        assert start["claim_ttl_seconds"] is None
        assert planned.registry.status_of(planned.task.id) == "completed"

    def test_failure_reports_fail_and_reraises(self, default_in_memory_fs_target):
        planned = plan_and_claim(make_range(limit=2))

        with pytest.raises(Exception, match="task exploded"):
            _run(planned, _boom_runner())

        assert planned.reports() == ["member_start", "member_fail"]
        (fail,) = planned.registry.calls_to("member_fail")
        assert "task exploded" in fail["error_message"]
        assert planned.registry.status_of(planned.task.id) == "failed"

    def test_a_yield_registers_the_children_and_suspends(
        self, default_in_memory_fs_target
    ):
        """The worker's yield: the incomplete children with their closure,
        the dynamic edges, and the parent SUSPENDED with its claim released
        — under this container's own deployment id."""
        planned = plan_and_claim(SyncDynamicRangeSumTask(limit=3))

        result = _run(planned)

        assert result is not None  # the incomplete dynamic deps
        assert planned.reports() == ["member_start", "member_yield"]
        (yield_call,) = planned.registry.calls_to("member_yield")
        assert yield_call["suspend"] is True
        assert yield_call["execution_id"] == planned.execution_id
        assert yield_call["deployment_id"] == planned.deployment_id
        assert len(yield_call["yielded"]) == 1
        assert planned.registry.status_of(planned.task.id) == "suspended"
        assert planned.registry.executions[planned.execution_id].outcome == "suspended"

    def test_a_yield_from_another_deployment_fails_the_task_instead(
        self, default_in_memory_fs_target
    ):
        """The registry refuses a yield whose deployment is not the plan's
        (``deployment_mismatch``); children it did not see must not be
        suspended on, so the worker reports the task failed."""
        planned = plan_and_claim(SyncDynamicRangeSumTask(limit=3))

        _run(planned, extra_env={STARDAG_DEPLOYMENT_ID_ENV: str(uuid4())})

        assert planned.reports() == ["member_start", "member_yield", "member_fail"]
        assert planned.registry.status_of(planned.task.id) == "failed"

    def test_a_yield_without_a_deployment_id_fails_the_task(
        self, default_in_memory_fs_target, monkeypatch
    ):
        monkeypatch.delenv(STARDAG_DEPLOYMENT_ID_ENV, raising=False)
        planned = plan_and_claim(SyncDynamicRangeSumTask(limit=3))

        _run(planned, extra_env={STARDAG_DEPLOYMENT_ID_ENV: ""})

        assert planned.reports() == ["member_start", "member_fail"]
        (fail,) = planned.registry.calls_to("member_fail")
        assert "STARDAG_DEPLOYMENT_ID" in fail["error_message"]

    def test_a_late_completion_changes_nothing(self, default_in_memory_fs_target):
        """The claim moved on while the worker ran (here: the build was
        cancelled, releasing it): the completion is recorded as late and
        refused, and the work itself is not undone."""
        planned = plan_and_claim(make_range(limit=3))

        class CancelMidRun(Runner):
            def run(self, task):
                planned.registry.build_cancel(planned.build_id)
                return super().run(task)

        # The start-of-attempt checkpoint passes; the cancel lands inside run.
        assert _run(planned, CancelMidRun()) is None

        assert planned.task.complete()
        assert planned.registry.status_of(planned.task.id) == "cancelled"
        assert planned.registry.called("member_complete", task_id=planned.task_id)

    def test_no_forwarded_ids_no_reporting(self, default_in_memory_fs_target):
        registry = InMemoryRegistry()
        with registry_provider.override(registry):
            assert Runner()(make_range(limit=3)) is None
        assert registry.calls == []

    def test_opt_out_no_reporting(self, default_in_memory_fs_target):
        planned = plan_and_claim(make_range(limit=3))

        assert _run(planned, Runner(report_lifecycle=False)) is None

        assert planned.reports() == []

    def test_registry_errors_never_fail_the_task(self, default_in_memory_fs_target):
        planned = plan_and_claim(make_range(limit=3))

        def down(*args, **kwargs):
            raise ConnectionError("registry down")

        planned.registry.member_start = down  # type: ignore[method-assign]
        planned.registry.member_complete = down  # type: ignore[method-assign]
        planned.registry.build_list_executions = down  # type: ignore[method-assign]

        assert _run(planned) is None
        assert planned.task.complete()  # the actual work still succeeded


class TestReporterCreate:
    def _env(self) -> dict[str, str]:
        return {
            STARDAG_BUILD_ID_ENV: str(uuid4()),
            STARDAG_PLAN_ID_ENV: str(uuid4()),
            STARDAG_EXECUTION_ID_ENV: str(uuid4()),
        }

    @pytest.mark.parametrize(
        "missing", [STARDAG_BUILD_ID_ENV, STARDAG_PLAN_ID_ENV, STARDAG_EXECUTION_ID_ENV]
    )
    def test_none_without_any_one_of_the_three_ids(self, missing):
        env = self._env()
        del env[missing]
        with registry_provider.override(InMemoryRegistry()):
            assert _WorkerLifecycleReporter.create(make_range(limit=1), env) is None

    def test_none_with_noop_registry(self):
        with registry_provider.override(NoOpRegistry()):
            assert (
                _WorkerLifecycleReporter.create(make_range(limit=1), self._env())
                is None
            )

    def test_none_with_an_invalid_id(self):
        env = {**self._env(), STARDAG_EXECUTION_ID_ENV: "not-a-uuid"}
        with registry_provider.override(InMemoryRegistry()):
            assert _WorkerLifecycleReporter.create(make_range(limit=1), env) is None

    def test_ids_from_the_process_env(self, monkeypatch):
        env = self._env()
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        with registry_provider.override(InMemoryRegistry()):
            reporter = _WorkerLifecycleReporter.create(make_range(limit=1), None)
        assert reporter is not None
        assert str(reporter.execution_id) == env[STARDAG_EXECUTION_ID_ENV]
        assert str(reporter.plan_id) == env[STARDAG_PLAN_ID_ENV]


class TestReportingRunsInsideEnvOverrides:
    def test_reporting_sees_env_overrides(self, default_in_memory_fs_target):
        """Lifecycle reporting runs inside the env-overrides context (the
        selector's env, the build's settings, the framework ids), so a
        yield walks the children exactly as run() sees them."""
        import os

        planned = plan_and_claim(make_range(limit=3))
        seen: dict[str, str | None] = {}
        original = planned.registry._record

        def observing(method, **kwargs):
            seen[method] = os.environ.get("MY_TEST_OVERRIDE")
            return original(method, **kwargs)

        planned.registry._record = observing  # type: ignore[method-assign]

        _run(planned, extra_env={"MY_TEST_OVERRIDE": "applied"})

        assert seen["member_start"] == "applied"
        assert seen["member_complete"] == "applied"


class TestReporterCreationGuard:
    def test_broken_reporter_creation_never_fails_the_task(
        self, monkeypatch, default_in_memory_fs_target
    ):
        from stardag.integration.modal import _runner as runner_module

        def broken_create(task, env_overrides):
            raise RuntimeError("malformed registry config")

        monkeypatch.setattr(
            runner_module._WorkerLifecycleReporter,
            "create",
            staticmethod(broken_create),
        )
        planned = plan_and_claim(make_range(limit=3))

        assert _run(planned) is None
        assert planned.task.complete()


class TestReactiveWorkerBehavior:
    """In reactive mode the worker wakes the build's scheduler after each
    report that can unblock it."""

    @pytest.fixture
    def tick_spawn_stub(self, monkeypatch):
        captured: dict = {}

        class _Stub:
            def spawn(self, **kwargs):
                captured["spawn_kwargs"] = kwargs
                return "tick-handle"

        def from_name(**kwargs):
            captured["from_name"] = kwargs
            return _Stub()

        monkeypatch.setattr(modal.Function, "from_name", staticmethod(from_name))
        return captured

    def _notify_returning(self, registry, result) -> list:
        notified: list = []

        def build_notify(build_id, *, can_spawn: bool = True):
            notified.append(build_id)
            if isinstance(result, Exception):
                raise result
            return result

        registry.build_notify = build_notify  # type: ignore[method-assign]
        return notified

    def test_complete_notifies_and_spawns_tick(
        self, tick_spawn_stub, default_in_memory_fs_target
    ):
        planned = plan_and_claim(make_range(limit=3))
        notified = self._notify_returning(
            planned.registry,
            BuildNotifyResult(build_id=planned.build_id, scheduler_live=False),
        )

        _run(planned, reactive=True)

        assert notified == [planned.build_id]
        assert tick_spawn_stub["from_name"] == {"app_name": "app", "name": "tick"}
        assert tick_spawn_stub["spawn_kwargs"] == {"build_id": str(planned.build_id)}

    def test_the_real_notify_flags_the_build(
        self, tick_spawn_stub, default_in_memory_fs_target
    ):
        planned = plan_and_claim(make_range(limit=3))

        _run(planned, reactive=True)

        assert planned.registry.called("build_notify", build_id=planned.build_id)
        assert tick_spawn_stub["spawn_kwargs"] == {"build_id": str(planned.build_id)}

    def test_a_build_that_wants_no_tick_does_not_get_one(
        self, tick_spawn_stub, default_in_memory_fs_target
    ):
        """A build no longer RUNNING cannot act on a wake-up; spawning for
        it anyway is the cancelled-build loop."""
        planned = plan_and_claim(make_range(limit=3))
        notified = self._notify_returning(
            planned.registry,
            BuildNotifyResult(
                build_id=planned.build_id, needs_tick=False, scheduler_live=False
            ),
        )

        _run(planned, reactive=True)

        assert notified == [planned.build_id], "the registry is still told"
        assert tick_spawn_stub == {}, "but no tick is spawned for a dead build"

    def test_live_scheduler_sets_the_flag_without_spawning(
        self, tick_spawn_stub, default_in_memory_fs_target
    ):
        """A scheduler holds the lease and re-reads the flag on its way out
        (the exit handshake), so a spawned tick would only find it held."""
        planned = plan_and_claim(make_range(limit=3))
        notified = self._notify_returning(
            planned.registry,
            BuildNotifyResult(build_id=planned.build_id, scheduler_live=True),
        )

        _run(planned, reactive=True)

        assert notified == [planned.build_id], "the flag must still be set"
        assert tick_spawn_stub == {}, "no tick spawned while a scheduler is live"

    @pytest.mark.parametrize(
        "notify_result",
        [BuildNotifyResult(), ConnectionError("registry down")],
        ids=["field-absent", "notify-failed"],
    )
    def test_unknown_scheduler_state_spawns(
        self, tick_spawn_stub, default_in_memory_fs_target, notify_result
    ):
        """Never skip on an answer we did not get: a redundant tick costs a
        container, a skipped one costs the build its progress."""
        planned = plan_and_claim(make_range(limit=3))
        self._notify_returning(planned.registry, notify_result)

        _run(planned, reactive=True)

        assert tick_spawn_stub["spawn_kwargs"] == {"build_id": str(planned.build_id)}

    def test_failure_also_wakes_scheduler(
        self, tick_spawn_stub, default_in_memory_fs_target
    ):
        planned = plan_and_claim(make_range(limit=2))

        with pytest.raises(Exception, match="nope"):
            _run(planned, _boom_runner("nope"), reactive=True)

        assert "spawn_kwargs" in tick_spawn_stub

    def test_a_yield_wakes_the_scheduler(
        self, tick_spawn_stub, default_in_memory_fs_target
    ):
        planned = plan_and_claim(SyncDynamicRangeSumTask(limit=3))

        assert _run(planned, reactive=True) is not None

        assert planned.registry.status_of(planned.task.id) == "suspended"
        assert tick_spawn_stub["spawn_kwargs"] == {"build_id": str(planned.build_id)}

    def test_non_reactive_does_not_wake(
        self, tick_spawn_stub, default_in_memory_fs_target
    ):
        planned = plan_and_claim(make_range(limit=3))

        _run(planned)

        assert tick_spawn_stub == {}
        assert not planned.registry.called("build_notify")
