"""Reconciliation (STA-94, STA-135): a tick ends its build's dead claim
holders -- a call that finished without reporting (R1), a spawn that never
landed (R2) -- and leaves everything it cannot positively call dead to the
claim's TTL. See ``stardag.build._reactive._reconcile`` for the invariant."""

from __future__ import annotations

import typing
from datetime import timedelta
from uuid import UUID

import pytest

from stardag import auto_namespace
from stardag.build import TickConfig
from stardag.build._reactive import _reconcile
from stardag.build._registration import new_id
from stardag.target import InMemoryFileTarget
from stardag.testing import InMemoryRegistry
from stardag.testing._registry_state import refuse
from stardag.utils.testing.helper_tasks import SyncOnlyTask

from tests.test_build.fakes import FAKE_EXECUTOR, FakeDetachedExecutor
from tests.test_build.test_reactive_tick import _plan, _tick

auto_namespace(__name__)

Target = typing.Type[InMemoryFileTarget]

ONE_PASS = TickConfig(linger_seconds=0, poll_interval_seconds=0.01)


async def _dead_claim(
    registry: InMemoryRegistry,
    build_id: UUID,
    task: SyncOnlyTask,
    *,
    ref: str | None,
    executor: str = FAKE_EXECUTOR,
    age_seconds: float = 0,
) -> UUID:
    """A claim a dead tick or worker left behind: granted, with or without
    a recorded ref, and nothing running it."""
    frontier = registry.build_get_frontier(build_id)
    assert frontier.plan_id is not None
    execution_id = new_id()
    registry.member_start(
        frontier.plan_id,
        str(task.id),
        execution_id=execution_id,
        claim=True,
        claim_ttl_seconds=3600,
        executor=executor,
        executor_ref=ref,
    )
    row = registry.executions[execution_id]
    row.started_at = row.started_at - timedelta(seconds=age_seconds)
    return execution_id


def _stops(registry: InMemoryRegistry) -> list[dict]:
    return registry.calls_to("execution_report_stopped")


class TestFinishedWithoutReporting:
    """R1: the executor positively says the call finished."""

    async def test_a_returned_call_whose_target_is_complete_is_completed(
        self, default_in_memory_fs_target: Target
    ):
        registry = InMemoryRegistry()
        task = SyncOnlyTask(name=f"done-{new_id()}")
        build_id, _ = await _plan(registry, [task])
        dead = await _dead_claim(registry, build_id, task, ref="fc-done")
        task.run()  # its worker wrote the output, then lost the report
        executor = FakeDetachedExecutor(registry=registry, workers=True)
        executor.probes["fc-done"] = "returned"
        summary = await _tick(registry, build_id, executor, ONE_PASS)
        (complete,) = registry.calls_to("member_complete", task_id=str(task.id))
        assert complete["execution_id"] == dead
        assert summary.reconciled == 1
        assert executor.spawns == []  # nothing ran it again
        assert registry.builds[build_id].status == "completed"

    async def test_a_returned_call_whose_target_is_missing_is_re_driven(
        self, default_in_memory_fs_target: Target
    ):
        registry = InMemoryRegistry()
        task = SyncOnlyTask(name=f"empty-{new_id()}")
        build_id, _ = await _plan(registry, [task])
        dead = await _dead_claim(registry, build_id, task, ref="fc-empty")
        executor = FakeDetachedExecutor(registry=registry, workers=True)
        executor.probes["fc-empty"] = "returned"
        summary = await _tick(registry, build_id, executor, ONE_PASS)
        (stop,) = _stops(registry)
        assert (stop["execution_id"], stop["outcome"]) == (dead, "lost")
        assert not stop["only_if_unspawned"]
        assert summary.reconciled == 1
        # Re-driven: a fresh claim, a fresh spawn.
        assert [t for t, _, _ in executor.spawns] == [task.id]
        assert executor.spawns[0][1] != dead

    async def test_a_raised_call_is_failed_against_the_retry_budget(
        self, default_in_memory_fs_target: Target
    ):
        registry = InMemoryRegistry()
        task = SyncOnlyTask(name=f"raised-{new_id()}")
        build_id, _ = await _plan(registry, [task])
        dead = await _dead_claim(registry, build_id, task, ref="fc-raised")
        executor = FakeDetachedExecutor(registry=registry, workers=True)
        executor.probes["fc-raised"] = "raised"
        await _tick(registry, build_id, executor, ONE_PASS)
        (fail,) = registry.calls_to("member_fail", task_id=str(task.id))
        assert fail["execution_id"] == dead
        assert "ended without reporting" in fail["error_message"]

    @pytest.mark.parametrize("state", ["running", "unknown"])
    async def test_a_call_not_positively_finished_is_left_alone(
        self, state: str, default_in_memory_fs_target: Target
    ):
        registry = InMemoryRegistry()
        task = SyncOnlyTask(name=f"alive-{new_id()}")
        build_id, _ = await _plan(registry, [task])
        await _dead_claim(registry, build_id, task, ref="fc-alive", age_seconds=7200)
        executor = FakeDetachedExecutor(registry=registry, workers=True)
        executor.probes["fc-alive"] = state
        summary = await _tick(registry, build_id, executor, ONE_PASS)
        assert summary.reconciled == 0
        assert _stops(registry) == []
        assert not registry.called("member_fail", task_id=str(task.id))
        assert executor.spawns == []

    async def test_an_executor_that_does_not_reconcile_is_left_alone(
        self, default_in_memory_fs_target: Target
    ):
        registry = InMemoryRegistry()
        task = SyncOnlyTask(name=f"other-{new_id()}")
        build_id, _ = await _plan(registry, [task])
        await _dead_claim(
            registry, build_id, task, ref=None, executor="elsewhere", age_seconds=7200
        )
        executor = FakeDetachedExecutor(registry=registry, workers=True)
        summary = await _tick(registry, build_id, executor, ONE_PASS)
        assert summary.reconciled == 0 and _stops(registry) == []


class TestSpawnNeverLanded:
    """R2: no ref recorded, past the grace, and ended only on that
    condition."""

    async def test_an_old_claim_with_no_ref_is_ended_conditionally_and_re_driven(
        self, default_in_memory_fs_target: Target
    ):
        registry = InMemoryRegistry()
        task = SyncOnlyTask(name=f"lost-{new_id()}")
        build_id, _ = await _plan(registry, [task])
        dead = await _dead_claim(registry, build_id, task, ref=None, age_seconds=3600)
        executor = FakeDetachedExecutor(registry=registry, workers=True)
        summary = await _tick(registry, build_id, executor, ONE_PASS)
        (stop,) = _stops(registry)
        assert stop["execution_id"] == dead
        assert (stop["outcome"], stop["only_if_unspawned"]) == ("lost", True)
        assert summary.reconciled == 1
        # Re-driven: a fresh claim, a fresh spawn.
        assert [t for t, _, _ in executor.spawns] == [task.id]
        assert executor.spawns[0][1] != dead

    async def test_a_young_claim_with_no_ref_is_left_alone(
        self, default_in_memory_fs_target: Target
    ):
        """A live tick may be between its claim and its spawn."""
        registry = InMemoryRegistry()
        task = SyncOnlyTask(name=f"young-{new_id()}")
        build_id, _ = await _plan(registry, [task])
        await _dead_claim(registry, build_id, task, ref=None, age_seconds=30)
        executor = FakeDetachedExecutor(registry=registry, workers=True)
        summary = await _tick(registry, build_id, executor, ONE_PASS)
        assert summary.reconciled == 0 and _stops(registry) == []

    async def test_a_ref_recorded_before_the_end_lands_wins(
        self, default_in_memory_fs_target: Target
    ):
        """The container landed after all, between the ledger read and the
        end: the registry refuses the end and nothing is re-driven."""

        class RefLandsFirst(InMemoryRegistry):
            def execution_report_stopped(self, execution_id, **kwargs):
                self.executions[execution_id].executor_ref = "fc-late"
                return super().execution_report_stopped(execution_id, **kwargs)

        registry = RefLandsFirst()
        task = SyncOnlyTask(name=f"late-{new_id()}")
        build_id, _ = await _plan(registry, [task])
        dead = await _dead_claim(registry, build_id, task, ref=None, age_seconds=3600)
        executor = FakeDetachedExecutor(registry=registry, workers=True)
        summary = await _tick(registry, build_id, executor, ONE_PASS)
        assert summary.reconciled == 0
        assert registry.executions[dead].ended_at is None
        assert executor.spawns == []

    async def test_a_registry_without_the_conditional_end_turns_it_off(
        self, default_in_memory_fs_target: Target
    ):
        """An older registry refuses the field (422): never the
        unconditional end instead."""

        class OldRegistry(InMemoryRegistry):
            refused = 0

            def execution_report_stopped(self, execution_id, **kwargs):
                if kwargs.get("only_if_unspawned"):
                    type(self).refused += 1
                    raise refuse("invalid_request", status=422)
                return super().execution_report_stopped(execution_id, **kwargs)

        registry = OldRegistry()
        task = SyncOnlyTask(name=f"old-{new_id()}")
        build_id, _ = await _plan(registry, [task])
        dead = await _dead_claim(registry, build_id, task, ref=None, age_seconds=3600)
        executor = FakeDetachedExecutor(registry=registry, workers=True)
        summary = await _tick(registry, build_id, executor, ONE_PASS)
        assert summary.reconciled == 0
        assert registry.executions[dead].ended_at is None
        assert OldRegistry.refused == 1
        assert _stops(registry) == []  # never the unconditional end


def test_a_round_is_due_once_per_interval():
    reconciler = _reconcile.Reconciler(interval_seconds=60)
    assert reconciler.due(0.0)
    reconciler._last = 0.0
    assert not reconciler.due(59)
    assert reconciler.due(60)
    reconciler.supported = False
    assert not reconciler.due(600)
