"""Tests for per-task execution claims in the resident build engine.

The fake registry implements real arbitration (mirroring the API's
claim-on-start semantics), so these tests exercise the full winner/loser
protocol: claim-then-spawn ordering, re-attach to a live winner,
already-completed resolution, dead-winner fail-and-retry, the no-ref wait
loop, capability/auto gating, the global-lock deprecation warning and its
TTL-renewal fix.
"""

from __future__ import annotations

import asyncio
import typing

import pytest

from stardag import BaseTask, auto_namespace
from stardag.build import (
    BuildExitStatus,
    ClaimConfig,
    DetachedExecutionStatus,
    GlobalLockConfig,
    LockAcquisitionResult,
    LockAcquisitionStatus,
    build_aio,
)
from stardag.registry import StartClaimResult
from stardag.target import InMemoryFileTarget
from stardag.utils.testing.helper_tasks import SyncOnlyTask

from .conftest import RecordingRegistry
from .test_detached import FakeDetachedExecutor

auto_namespace(__name__)

FAST_CLAIM = ClaimConfig(
    wait_timeout_seconds=0.5,
    wait_initial_interval_seconds=0.02,
    wait_max_interval_seconds=0.05,
)


class ClaimRegistry(RecordingRegistry):
    """Recording registry with real claim arbitration (API semantics)."""

    def __init__(self) -> None:
        super().__init__()
        # task_id(str) -> status; refs: task_id -> (executor, ref)
        self.statuses: dict[str, str] = {}
        self.refs: dict[str, tuple[str | None, str | None]] = {}

    def seed_running(
        self, task: BaseTask, executor: str | None, ref: str | None
    ) -> None:
        self.statuses[str(task.id)] = "running"
        self.refs[str(task.id)] = (executor, ref)

    async def task_start_claim_aio(
        self,
        build_id,
        task,
        executor=None,
        executor_ref=None,
        executor_metadata=None,
        limit_keys=None,
    ) -> StartClaimResult:
        tid = str(task.id)
        self._record("task_start_claim_aio", task.id)
        status = self.statuses.get(tid)
        if status == "running":
            stored_executor, stored_ref = self.refs.get(tid, (None, None))
            return StartClaimResult(
                started=False,
                denied_reason="already_running",
                executor=stored_executor,
                executor_ref=stored_ref,
            )
        if status == "completed":
            return StartClaimResult(started=False, denied_reason="already_completed")
        self.statuses[tid] = "running"
        self.refs[tid] = (executor, executor_ref)
        return StartClaimResult(started=True)

    async def task_start_aio(
        self,
        build_id,
        task,
        executor=None,
        executor_ref=None,
        executor_metadata=None,
    ):
        await super().task_start_aio(
            build_id,
            task,
            executor=executor,
            executor_ref=executor_ref,
            executor_metadata=executor_metadata,
        )
        self.statuses[str(task.id)] = "running"
        if executor_ref is not None:
            self.refs[str(task.id)] = (executor, executor_ref)

    async def task_complete_aio(self, build_id, task):
        await super().task_complete_aio(build_id, task)
        self.statuses[str(task.id)] = "completed"

    async def task_fail_aio(self, build_id, task, error_message=None):
        await super().task_fail_aio(build_id, task, error_message)
        self.statuses[str(task.id)] = "failed"

    async def task_waiting_for_lock_aio(self, build_id, task, lock_owner=None):
        self._record("task_waiting_for_lock_aio", task.id)

    def claim_calls(self, task: BaseTask) -> int:
        return sum(
            1
            for (m, tid, _) in self.calls
            if m == "task_start_claim_aio" and tid == task.id
        )


class ProbingExecutor(FakeDetachedExecutor):
    """FakeDetachedExecutor with a configurable liveness probe."""

    def __init__(self, *args, probe_statuses=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.probe_statuses = probe_statuses or {}

    async def detached_status(self, task, executor, ref):
        return self.probe_statuses.get(ref, DetachedExecutionStatus.UNKNOWN)


class TestClaimWinner:
    async def test_claim_then_spawn_then_ref_record(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
    ):
        """Default (auto) claims: the acquiring start precedes the spawn and
        carries no ref; the post-spawn start records the ref."""
        task = SyncOnlyTask(name="claim-winner")
        registry = ClaimRegistry()
        executor = FakeDetachedExecutor()

        summary = await build_aio([task], task_executor=executor, registry=registry)

        assert summary.status == BuildExitStatus.SUCCESS
        assert task.complete()
        assert registry.claim_calls(task) == 1
        assert executor.spawn_calls == [task.id]
        # ref-recording start after the claim (plain, tolerated duplicate)
        starts = [
            extra
            for (m, tid, extra) in registry.calls
            if m == "task_start_aio" and tid == task.id
        ]
        assert len(starts) == 1
        assert starts[0]["executor_ref"] == f"spawned-{task.id}"
        # ordering: claim before spawn-ref start
        methods = registry.call_methods_for(task.id)
        assert methods.index("task_start_claim_aio") < methods.index("task_start_aio")

    async def test_claim_off(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        task = SyncOnlyTask(name="claim-off")
        registry = ClaimRegistry()

        summary = await build_aio(
            [task],
            task_executor=FakeDetachedExecutor(),
            registry=registry,
            claim=False,
        )

        assert summary.status == BuildExitStatus.SUCCESS
        assert registry.claim_calls(task) == 0

    async def test_auto_skips_unprobeable_executor(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """auto: no claim for executors without detached support (ref-less
        executions need TTL liveness the claim doesn't have)."""
        task = SyncOnlyTask(name="claim-unprobeable")
        registry = ClaimRegistry()

        summary = await build_aio(
            [task],
            task_executor=FakeDetachedExecutor(detached=False),
            registry=registry,
        )

        assert summary.status == BuildExitStatus.SUCCESS
        assert registry.claim_calls(task) == 0

    async def test_claim_skipped_without_registry_support(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """Registries without claim arbitration (ABC default) are never
        claimed through — even with claim=True (warned, not failed)."""
        task = SyncOnlyTask(name="claim-unsupported")
        registry = RecordingRegistry()  # no task_start_claim_aio override

        summary = await build_aio(
            [task],
            task_executor=FakeDetachedExecutor(),
            registry=registry,
            claim=True,
        )

        assert summary.status == BuildExitStatus.SUCCESS
        assert not registry.has_call("task_start_claim_aio", task.id)


class TestClaimLoser:
    async def test_attaches_to_live_winner(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        task = SyncOnlyTask(name="claim-loser-live")
        registry = ClaimRegistry()
        executor = FakeDetachedExecutor(live_refs={"fc-winner"})
        registry.seed_running(task, "fake", "fc-winner")

        summary = await build_aio([task], task_executor=executor, registry=registry)

        assert summary.status == BuildExitStatus.SUCCESS
        assert task.complete()
        assert executor.reattach_calls == [(task.id, "fake", "fc-winner")]
        assert executor.spawn_calls == []  # never spawned a duplicate

    async def test_already_completed_resolves_as_previously_completed(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        task = SyncOnlyTask(name="claim-loser-done")
        task.run()  # target exists
        registry = ClaimRegistry()
        registry.statuses[str(task.id)] = "completed"
        executor = FakeDetachedExecutor()

        summary = await build_aio([task], task_executor=executor, registry=registry)

        assert summary.status == BuildExitStatus.SUCCESS
        assert summary.task_count.previously_completed == 1
        assert summary.task_count.succeeded == 0
        assert executor.spawn_calls == []

    async def test_dead_winner_recorded_and_claim_retried(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """A denied claim whose winner is provably dead records the failure
        and immediately re-claims — this build then runs the task."""
        task = SyncOnlyTask(name="claim-loser-dead")
        registry = ClaimRegistry()
        executor = ProbingExecutor(
            probe_statuses={"fc-dead": DetachedExecutionStatus.FAILED}
        )
        registry.seed_running(task, "fake", "fc-dead")

        summary = await build_aio(
            [task],
            task_executor=executor,
            registry=registry,
            claim_config=FAST_CLAIM,
        )

        assert summary.status == BuildExitStatus.SUCCESS
        assert task.complete()
        assert registry.has_call("task_fail_aio", task.id)  # dead winner recorded
        assert registry.claim_calls(task) == 2  # denied, then won
        assert executor.spawn_calls == [task.id]

    async def test_no_ref_winner_waits_until_completion(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """No probeable ref: the loser polls and picks up the external
        completion (target + registry status)."""
        task = SyncOnlyTask(name="claim-loser-wait")
        registry = ClaimRegistry()
        registry.seed_running(task, None, None)
        executor = FakeDetachedExecutor()

        async def external_completion():
            await asyncio.sleep(0.1)
            task.run()  # winner writes the target
            registry.statuses[str(task.id)] = "completed"

        completer = asyncio.create_task(external_completion())
        try:
            summary = await build_aio(
                [task],
                task_executor=executor,
                registry=registry,
                claim_config=FAST_CLAIM,
            )
        finally:
            await completer

        assert summary.status == BuildExitStatus.SUCCESS
        assert summary.task_count.previously_completed == 1
        assert executor.spawn_calls == []
        # the wait was surfaced to the registry/UI
        assert registry.has_call("task_waiting_for_lock_aio", task.id) or any(
            m == "task_waiting_for_lock_aio" for (m, _, _) in registry.calls
        )

    async def test_no_ref_winner_timeout_fails_task(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        task = SyncOnlyTask(name="claim-loser-timeout")
        registry = ClaimRegistry()
        registry.seed_running(task, None, None)

        with pytest.raises(TimeoutError):
            await build_aio(
                [task],
                task_executor=FakeDetachedExecutor(),
                registry=registry,
                claim_config=ClaimConfig(
                    wait_timeout_seconds=0.15,
                    wait_initial_interval_seconds=0.02,
                    wait_max_interval_seconds=0.05,
                ),
            )

        assert registry.has_call("task_fail_aio", task.id)


class TestLockDeprecationAndRenewal:
    async def test_global_lock_config_warns_deprecation(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        task = SyncOnlyTask(name="lock-deprecated")
        with pytest.warns(DeprecationWarning, match="execution claims"):
            await build_aio(
                [task],
                task_executor=FakeDetachedExecutor(),
                registry=RecordingRegistry(),
                global_lock_config=GlobalLockConfig(enabled=True),
            )

    async def test_lock_renewal_runs_for_long_tasks(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
        monkeypatch,
    ):
        """The engine renews held locks in the background so they don't
        expire under tasks longer than the lease TTL."""
        from stardag.build import _concurrent as concurrent_module
        from stardag.build._base import TaskExecutorABC

        monkeypatch.setattr(concurrent_module, "_LOCK_RENEWAL_INTERVAL_SECONDS", 0.05)

        renews: list[str] = []

        class RenewingLockManager:
            def lock(self, task_id):
                raise NotImplementedError

            async def acquire(self, task_id: str) -> LockAcquisitionResult:
                return LockAcquisitionResult(
                    status=LockAcquisitionStatus.ACQUIRED, acquired=True
                )

            async def release(self, task_id: str, task_completed: bool = False):
                return True

            async def renew(self, task_id: str, ttl_seconds: int = 60) -> bool:
                renews.append(task_id)
                return True

        class SlowExecutor(TaskExecutorABC):
            async def submit(self, task):
                await asyncio.sleep(0.25)  # several renewal intervals
                task.run()
                return None

            async def setup(self):
                pass

            async def teardown(self):
                pass

        task = SyncOnlyTask(name="lock-renewal")
        with pytest.warns(DeprecationWarning):
            summary = await build_aio(
                [task],
                task_executor=SlowExecutor(),
                registry=RecordingRegistry(),
                global_lock_manager=typing.cast(typing.Any, RenewingLockManager()),
                global_lock_config=GlobalLockConfig(enabled=True),
            )

        assert summary.status == BuildExitStatus.SUCCESS
        assert len(renews) >= 2
        assert all(r == str(task.id) for r in renews)
