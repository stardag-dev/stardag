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
        self.expires_at: dict[str, str] = {}

    def seed_running(
        self,
        task: BaseTask,
        executor: str | None,
        ref: str | None,
        latest_status_expires_at: str | None = None,
    ) -> None:
        self.statuses[str(task.id)] = "running"
        self.refs[str(task.id)] = (executor, ref)
        if latest_status_expires_at is not None:
            self.expires_at[str(task.id)] = latest_status_expires_at

    async def task_start_claim_aio(
        self,
        build_id,
        task,
        executor=None,
        executor_ref=None,
        executor_metadata=None,
        limit_keys=None,
        claim_ttl_seconds=None,
        *,
        claim=True,
    ) -> StartClaimResult:
        tid = str(task.id)
        self._record("task_start_claim_aio", task.id)
        status = self.statuses.get(tid)
        # Both denials are the *claim's*, and the server gates them on it
        # (routes/builds.py). A double that denied regardless could not
        # emulate the limiter's unclaiming acquire, which starts a task its
        # own build has already claimed.
        if claim and status == "running":
            stored_executor, stored_ref = self.refs.get(tid, (None, None))
            return StartClaimResult(
                started=False,
                denied_reason="already_running",
                executor=stored_executor,
                executor_ref=stored_ref,
                latest_status_expires_at=self.expires_at.get(tid),
            )
        if claim and status == "completed":
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
        claim_ttl_seconds=None,
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

    async def test_claim_through_registry_less_path(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The registry-less path stays claimable: NoOpRegistry grants every
        claim (nothing shared to arbitrate against), so a claim=True build
        runs exactly as it would without claims."""
        task = SyncOnlyTask(name="claim-registry-less")
        registry = RecordingRegistry()  # NoOpRegistry subclass, no arbitration

        summary = await build_aio(
            [task],
            task_executor=FakeDetachedExecutor(),
            registry=registry,
            claim=True,
        )

        assert summary.status == BuildExitStatus.SUCCESS
        assert task.complete()
        # The engine's own ref-carrying start still lands.
        assert registry.has_call("task_start_aio", task.id)


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
        # denied → corroborating re-probe (still denied) → won: a single
        # FAILED probe is never trusted (transient errors must not kill a
        # live winner).
        assert registry.claim_calls(task) == 3
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

    async def test_no_ref_winner_timeout_fails_locally_only(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """A loser's wait timeout fails THIS build only — it must not stamp
        the task's env-global status FAILED (the winner may be a
        legitimately long-running ref-less execution; a global fail would
        release its claim to a third build)."""
        task = SyncOnlyTask(name="claim-loser-timeout")
        registry = ClaimRegistry()
        registry.seed_running(task, None, None)

        with pytest.raises(Exception, match="timed out"):
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

        assert not registry.has_call("task_fail_aio", task.id)
        assert registry.statuses[str(task.id)] == "running"  # claim intact


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


class TestClaimRobustness:
    """Review-driven hardening: transient errors must never break the
    exactly-once guarantee or clobber a live winner."""

    async def test_probe_exception_treated_as_unknown_not_dead(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """A raising liveness probe (transient backend error) must NOT let
        the loser declare the winner dead — it waits instead, and picks up
        the winner's completion."""
        task = SyncOnlyTask(name="claim-probe-raises")
        registry = ClaimRegistry()
        registry.seed_running(task, "fake", "fc-blip")

        class RaisingProbeExecutor(FakeDetachedExecutor):
            async def detached_status(self, task, executor, ref):
                raise ConnectionError("backend blip")

        async def external_completion():
            await asyncio.sleep(0.1)
            task.run()
            registry.statuses[str(task.id)] = "completed"

        completer = asyncio.create_task(external_completion())
        try:
            summary = await build_aio(
                [task],
                task_executor=RaisingProbeExecutor(),
                registry=registry,
                claim_config=FAST_CLAIM,
            )
        finally:
            await completer

        assert summary.status == BuildExitStatus.SUCCESS
        assert summary.task_count.previously_completed == 1
        assert not registry.has_call("task_fail_aio", task.id)  # winner untouched

    async def test_single_failed_probe_not_trusted(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """One FAILED probe followed by a healthy re-probe (transient
        misclassification) must not record the winner as dead."""
        task = SyncOnlyTask(name="claim-probe-flap")
        registry = ClaimRegistry()
        registry.seed_running(task, "fake", "fc-flap")

        class FlappingProbeExecutor(FakeDetachedExecutor):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.probes = 0

            async def detached_status(self, task, executor, ref):
                self.probes += 1
                if self.probes == 1:
                    return DetachedExecutionStatus.FAILED  # transient blip
                return DetachedExecutionStatus.RUNNING

        executor = FlappingProbeExecutor()

        async def external_completion():
            await asyncio.sleep(0.15)
            task.run()
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
        assert executor.probes >= 2  # corroboration probe happened
        assert not registry.has_call("task_fail_aio", task.id)
        assert executor.spawn_calls == []

    async def test_unclaimed_fallback_records_normal_start(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """If the claim call itself errors in warn mode, the engine falls
        back to the normal UNCLAIMED start path — a TASK_STARTED is still
        recorded (nothing is silently assumed claimed)."""

        class BrokenClaimRegistry(ClaimRegistry):
            async def task_start_claim_aio(self, *args, **kwargs):
                raise ConnectionError("registry down for claims")

        task = SyncOnlyTask(name="claim-unclaimed-fallback")
        registry = BrokenClaimRegistry()

        summary = await build_aio(
            [task],
            task_executor=FakeDetachedExecutor(),
            registry=registry,
            on_registry_failure="warn",
        )

        assert summary.status == BuildExitStatus.SUCCESS
        # normal start path ran (with the executor ref recorded)
        starts = [
            extra
            for (m, tid, extra) in registry.calls
            if m == "task_start_aio" and tid == task.id
        ]
        assert len(starts) == 1
        assert starts[0]["executor_ref"] == f"spawned-{task.id}"

    async def test_lapsed_refless_holder_recovered(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """A ref-less holder whose claim has lapsed (the winner's
        claim→ref-record crash window) is recorded failed and the claim
        re-taken — on the server's own expiry, with no local bound."""
        from datetime import datetime, timedelta, timezone

        task = SyncOnlyTask(name="claim-lapsed-holder")
        registry = ClaimRegistry()
        lapsed = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
        registry.seed_running(task, None, None, latest_status_expires_at=lapsed)
        executor = FakeDetachedExecutor()

        summary = await build_aio(
            [task],
            task_executor=executor,
            registry=registry,
            claim_config=ClaimConfig(
                wait_timeout_seconds=2.0,
                wait_initial_interval_seconds=0.02,
                wait_max_interval_seconds=0.05,
            ),
        )

        assert summary.status == BuildExitStatus.SUCCESS
        assert registry.has_call("task_fail_aio", task.id)  # lapsed holder recorded
        assert executor.spawn_calls == [task.id]
        assert task.complete()

    async def test_live_refless_holder_is_waited_on(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """Control: a ref-less holder whose claim is still live is waited
        on, not recovered — the expiry is the whole decision, so an
        unexpired one must keep the loser off the task."""
        from datetime import datetime, timedelta, timezone

        task = SyncOnlyTask(name="claim-live-holder")
        registry = ClaimRegistry()
        live = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        registry.seed_running(task, None, None, latest_status_expires_at=live)
        executor = FakeDetachedExecutor()

        with pytest.raises(Exception, match="Claim wait timed out"):
            await build_aio(
                [task],
                task_executor=executor,
                registry=registry,
                claim_config=ClaimConfig(
                    wait_timeout_seconds=0.2,
                    wait_initial_interval_seconds=0.02,
                    wait_max_interval_seconds=0.05,
                ),
            )

        assert not registry.has_call("task_fail_aio", task.id)
        assert executor.spawn_calls == []

    async def test_no_warning_for_manager_without_enabled_lock(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """Passing a lock manager with locking left disabled must not warn
        (the lock is never acquired)."""
        import warnings as warnings_module

        task = SyncOnlyTask(name="lock-manager-no-warn")

        class UnusedLockManager:
            def lock(self, task_id):
                raise NotImplementedError

            async def acquire(self, task_id):
                raise NotImplementedError

            async def release(self, task_id, task_completed=False):
                return True

        with warnings_module.catch_warnings():
            warnings_module.simplefilter("error", DeprecationWarning)
            summary = await build_aio(
                [task],
                task_executor=FakeDetachedExecutor(),
                registry=ClaimRegistry(),
                global_lock_manager=typing.cast(typing.Any, UnusedLockManager()),
            )

        assert summary.status == BuildExitStatus.SUCCESS

    async def test_zero_wait_timeout_still_claims_once(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """wait_timeout_seconds=0 means "claim, but don't wait if held" —
        the claim must still be attempted (and won) when it is free."""
        task = SyncOnlyTask(name="claim-zero-timeout-free")
        registry = ClaimRegistry()
        executor = FakeDetachedExecutor()

        summary = await build_aio(
            [task],
            task_executor=executor,
            registry=registry,
            claim_config=ClaimConfig(wait_timeout_seconds=0.0),
        )

        assert summary.status == BuildExitStatus.SUCCESS
        assert registry.claim_calls(task) == 1
        assert executor.spawn_calls == [task.id]

    async def test_zero_wait_timeout_denied_fails_fast_locally(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """With wait_timeout_seconds=0 a held claim fails this build
        immediately — one attempt, no waiting, no global task_fail."""
        task = SyncOnlyTask(name="claim-zero-timeout-held")
        registry = ClaimRegistry()
        registry.seed_running(task, None, None)

        with pytest.raises(Exception, match="timed out"):
            await build_aio(
                [task],
                task_executor=FakeDetachedExecutor(),
                registry=registry,
                claim_config=ClaimConfig(wait_timeout_seconds=0.0),
            )

        assert registry.claim_calls(task) == 1
        assert not registry.has_call("task_fail_aio", task.id)
