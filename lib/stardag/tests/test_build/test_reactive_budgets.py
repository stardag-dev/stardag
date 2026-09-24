"""What a reactive tick does with a member that failed or keeps being
interrupted (unit tier, against the in-memory registry).

- A FAILED member is a result: the fail mode owns it and the tick never
  retries it (design.md, "The runnable rule": FAILED is not actionable).
- An INTERRUPTED member is actionable, so the tick restarts it — up to
  ``TickConfig.max_interruptions``, counted by the registry from the
  execution ledger over the build's plans (D9) and served on the frontier.
  At the cap the tick records a failure naming the count instead.
"""

from __future__ import annotations

import typing
from uuid import UUID

from stardag import BaseTask, auto_namespace
from stardag.build import TickConfig, run_tick_aio
from stardag.build._reactive import TickSummary
from stardag.build._registration import new_id, register_plan_aio, walk_aio
from stardag.target import InMemoryFileTarget
from stardag.testing import InMemoryRegistry
from stardag.utils.testing.helper_tasks import SyncOnlyTask

from tests.test_build.fakes import FakeDetachedExecutor

auto_namespace(__name__)

Target = typing.Type[InMemoryFileTarget]


def _config(**kwargs: typing.Any) -> TickConfig:
    return TickConfig(linger_seconds=0.5, poll_interval_seconds=0.01, **kwargs)


async def _plan(registry: InMemoryRegistry, roots: list[BaseTask]) -> UUID:
    deployment_id = registry.add_deployment(app_name="app")
    build_id = registry.build_create(root_task_ids=[str(r.id) for r in roots]).id
    await register_plan_aio(
        registry,
        build_id,
        await walk_aio(roots),
        deployment_id=deployment_id,
        settings={},
    )
    registry.build_set_reactive_meta(build_id, app_name="app")
    return build_id


class InterruptingExecutor(FakeDetachedExecutor):
    """Every execution records its ref, then reports INTERRUPTED — a task
    that checkpoints and asks to be resumed on every run (the Modal
    runner's ``interrupted`` report)."""

    def __init__(self, *, interrupt_runs: int | None = None, **kwargs) -> None:
        super().__init__(workers=True, **kwargs)
        # None: every run is interrupted; n: the first n runs are.
        self.interrupt_runs = interrupt_runs
        self.runs = 0

    async def _worker(self, task, execution_id, plan_id, deployment_id, ref):
        self.runs += 1
        if self.interrupt_runs is not None and self.runs > self.interrupt_runs:
            return await super()._worker(
                task, execution_id, plan_id, deployment_id, ref
            )
        registry = self.registry
        assert registry is not None and plan_id is not None
        await registry.member_start_aio(
            plan_id, str(task.id), execution_id=execution_id, claim=False
        )
        registry.member_interrupt(
            plan_id,
            str(task.id),
            execution_id=execution_id,
            error_message="asked to be resumed near its timeout",
        )
        self._wake(plan_id)
        return None


async def _drive(
    registry: InMemoryRegistry,
    build_id: UUID,
    executor: FakeDetachedExecutor,
    config: TickConfig,
) -> TickSummary:
    summary = await run_tick_aio(
        build_id, registry=registry, task_executor=executor, config=config
    )
    await executor.drain()
    return summary


class TestTheLedgerCounts:
    """The fake serves the counts the server's frontier serves."""

    async def test_interruptions_and_attempts_are_counted_over_the_build(
        self, default_in_memory_fs_target: Target
    ):
        registry = InMemoryRegistry()
        task = SyncOnlyTask(name=f"counted-{new_id()}")
        build_id = await _plan(registry, [task])
        plan_id = registry.active_plan(build_id).id  # type: ignore[union-attr]
        for _ in range(2):
            execution_id = new_id()
            registry.member_start(plan_id, str(task.id), execution_id=execution_id)
            registry.member_interrupt(plan_id, str(task.id), execution_id=execution_id)
        (member,) = registry.build_get_frontier(build_id).runnable
        assert (member.status, member.attempts, member.interruptions) == (
            "interrupted",
            2,
            2,
        )

    async def test_a_preempted_end_counts_once_and_a_failed_spawn_is_an_attempt(
        self, default_in_memory_fs_target: Target
    ):
        registry = InMemoryRegistry()
        task = SyncOnlyTask(name=f"preempted-{new_id()}")
        build_id = await _plan(registry, [task])
        plan_id = registry.active_plan(build_id).id  # type: ignore[union-attr]
        first = new_id()
        registry.member_start(plan_id, str(task.id), execution_id=first)
        registry.member_interrupt(plan_id, str(task.id), execution_id=first)
        # A preemption's restart that ends interrupted: one execution, and
        # its claim release and its end both say so — counted once.
        registry.executions[first].outcome = "preempted"
        assert registry.attempt_counts(build_id, str(task.id)) == (1, 1)
        second = new_id()
        registry.member_start(plan_id, str(task.id), execution_id=second)
        registry.member_fail(plan_id, str(task.id), execution_id=second)
        assert registry.attempt_counts(build_id, str(task.id)) == (2, 1)

    async def test_another_builds_executions_are_not_counted(
        self, default_in_memory_fs_target: Target
    ):
        registry = InMemoryRegistry()
        task = SyncOnlyTask(name=f"shared-{new_id()}")
        first_build = await _plan(registry, [task])
        plan_id = registry.active_plan(first_build).id  # type: ignore[union-attr]
        execution_id = new_id()
        registry.member_start(plan_id, str(task.id), execution_id=execution_id)
        registry.member_interrupt(plan_id, str(task.id), execution_id=execution_id)
        second_build = await _plan(registry, [task])
        (member,) = registry.build_get_frontier(second_build).runnable
        assert (member.attempts, member.interruptions) == (0, 0)


class TestAFailedMember:
    async def test_a_worker_reported_failure_is_not_retried(
        self, default_in_memory_fs_target: Target
    ):
        """Case (a): the fail mode decides, the tick does not retry. The
        build stalls on the failure and fails."""

        class Raising(SyncOnlyTask):
            def run(self) -> None:
                raise RuntimeError("deterministic")

        registry = InMemoryRegistry()
        task = Raising(name=f"fails-{new_id()}")
        build_id = await _plan(registry, [task])
        executor = FakeDetachedExecutor(registry=registry, workers=True)
        summary = await _drive(registry, build_id, executor, _config())
        assert len(executor.spawns) == 1
        assert registry.status_of(task.id) == "failed"
        assert summary.terminal_status == "failed"


class TestTheInterruptionBudget:
    async def test_an_interrupted_member_under_budget_is_restarted(
        self, default_in_memory_fs_target: Target
    ):
        registry = InMemoryRegistry()
        task = SyncOnlyTask(name=f"resumed-{new_id()}")
        build_id = await _plan(registry, [task])
        executor = InterruptingExecutor(registry=registry, interrupt_runs=2)
        summary = await _drive(registry, build_id, executor, _config())
        assert executor.runs == 3
        assert summary.interruptions_exhausted == 0
        assert summary.terminal_status == "completed"

    async def test_a_task_interrupted_on_every_run_is_bounded(
        self, default_in_memory_fs_target: Target
    ):
        """Case (b): without the cap this restarts forever. With
        ``max_interruptions=3`` it runs three times, then the tick records
        a failure naming the count and the build fails per its fail mode."""
        registry = InMemoryRegistry()
        task = SyncOnlyTask(name=f"forever-{new_id()}")
        build_id = await _plan(registry, [task])
        executor = InterruptingExecutor(registry=registry)
        summary = await _drive(
            registry, build_id, executor, _config(max_interruptions=3)
        )
        assert executor.runs == 3
        assert summary.interruptions_exhausted == 1
        (fail,) = registry.calls_to("member_fail", task_id=task.id)
        assert "Interrupted 3 times" in fail["error_message"]
        assert "max_interruptions=3" in fail["error_message"]
        assert registry.status_of(task.id) == "failed"
        assert any(e.type == "TASK_FAILED" for e in registry.events)
        assert summary.terminal_status == "failed"
        assert registry.builds[build_id].status == "failed"
        # The failure is recorded under a claim and nothing was spawned for
        # it: one more attempt on the ledger, no fourth run.
        assert registry.attempt_counts(build_id, str(task.id)) == (4, 3)

    async def test_the_default_budget_is_twenty(
        self, default_in_memory_fs_target: Target
    ):
        assert TickConfig().max_interruptions == 20
        registry = InMemoryRegistry()
        task = SyncOnlyTask(name=f"default-{new_id()}")
        build_id = await _plan(registry, [task])
        executor = InterruptingExecutor(registry=registry)
        summary = await _drive(registry, build_id, executor, _config())
        assert executor.runs == 20
        assert summary.terminal_status == "failed"

    async def test_an_operators_retry_gets_one_more_run(
        self, default_in_memory_fs_target: Target
    ):
        """At the budget the member is FAILED; ``tasks retry`` makes it
        PENDING, which is not gated: it runs once more, and an interruption
        of that run fails it again (the count is over the build)."""
        registry = InMemoryRegistry()
        task = SyncOnlyTask(name=f"retried-{new_id()}")
        build_id = await _plan(registry, [task])
        plan_id = registry.active_plan(build_id).id  # type: ignore[union-attr]
        for _ in range(2):
            execution_id = new_id()
            registry.member_start(plan_id, str(task.id), execution_id=execution_id)
            registry.member_interrupt(plan_id, str(task.id), execution_id=execution_id)
        registry.member_retry(plan_id, str(task.id))
        executor = InterruptingExecutor(registry=registry)
        summary = await _drive(
            registry, build_id, executor, _config(max_interruptions=2)
        )
        assert executor.runs == 1
        assert summary.interruptions_exhausted == 1
        assert registry.status_of(task.id) == "failed"
