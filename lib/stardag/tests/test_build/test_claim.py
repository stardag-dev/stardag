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
from uuid import uuid4

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
from stardag.exceptions import APIError, ExecutionCancelled
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
        # Per task, the identity of the claim it is held under -- the
        # API's ``tasks.latest_execution_id``.
        self.execution_ids: dict[str, str | None] = {}
        # Per task, the build whose claim was granted -- the API's
        # ``latest_status_build_id``, which it tests before comparing
        # identities.
        self.claim_build_ids: dict[str, object] = {}
        # Every identity this registry was sent on a claim, in order.
        self.claim_execution_ids: list[str | None] = []
        # Simulates a lost response: the first claim commits server-side
        # and the caller is told ``already_running`` -- what the client's
        # own transport retry sees when the original answer never
        # arrived. The resident engine then re-asks by construction,
        # because its claim sits in a wait-and-retry loop.
        self.lose_first_response = False
        # Simulates losing the task between the claim and the spawn: the
        # post-spawn start that records the reference is refused, which is
        # what the API answers when the row has moved on.
        self.refuse_ref_recording_start: str | None = None

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
        execution_id=None,
        *,
        claim=True,
    ) -> StartClaimResult:
        tid = str(task.id)
        self._record("task_start_claim_aio", task.id)
        self.claim_execution_ids.append(
            None if execution_id is None else str(execution_id)
        )
        if claim and self.lose_first_response:
            # Commit, then answer as though the response was lost and
            # the client's retry met its own claim.
            self.lose_first_response = False
            self.statuses[tid] = "running"
            self.execution_ids[tid] = (
                None if execution_id is None else str(execution_id)
            )
            self.claim_build_ids[tid] = build_id
            return StartClaimResult(
                started=False,
                denied_reason="already_running",
                execution_id=self.execution_ids[tid],
            )
        status = self.statuses.get(tid)
        # Both denials are the *claim's*, and the server gates them on it
        # (routes/builds.py). A double that denied regardless could not
        # emulate the limiter's unclaiming acquire, which starts a task its
        # own build has already claimed.
        held = self.execution_ids.get(tid)
        # The same attempt asking again -- the server's rule, which
        # tests the holding build before it compares identities, so a
        # neighbour sending the holder's id is refused like any other
        # second claimant.
        same_attempt = (
            execution_id is not None
            and held == str(execution_id)
            and self.claim_build_ids.get(tid) == build_id
        )
        if claim and status == "running" and not same_attempt:
            stored_executor, stored_ref = self.refs.get(tid, (None, None))
            return StartClaimResult(
                started=False,
                denied_reason="already_running",
                executor=stored_executor,
                executor_ref=stored_ref,
                latest_status_expires_at=self.expires_at.get(tid),
                execution_id=held,
            )
        if claim and status == "completed":
            return StartClaimResult(started=False, denied_reason="already_completed")
        self.statuses[tid] = "running"
        self.refs[tid] = (executor, executor_ref)
        if claim:
            # A granted claim records its identity, including recording
            # none: it is a new attempt and inherits nothing.
            self.execution_ids[tid] = (
                None if execution_id is None else str(execution_id)
            )
            self.claim_build_ids[tid] = build_id
        return StartClaimResult(
            started=True,
            execution_id=None if execution_id is None else str(execution_id),
        )

    async def task_start_aio(
        self,
        build_id,
        task,
        executor=None,
        executor_ref=None,
        executor_metadata=None,
        claim_ttl_seconds=None,
        execution_id=None,
    ):
        if self.refuse_ref_recording_start is not None and executor_ref is not None:
            raise APIError(
                "the task has moved on",
                status_code=409,
                payload={"error_code": self.refuse_ref_recording_start},
            )
        await super().task_start_aio(
            build_id,
            task,
            executor=executor,
            executor_ref=executor_ref,
            executor_metadata=executor_metadata,
            execution_id=execution_id,
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


class TestClaimIdentity:
    """The resident engine's claim carries an identity, minted once per
    attempt and re-sent on every iteration of its wait-and-retry loop.

    This is the *more* reachable of the two engines, not the lesser one.
    The reactive engine claims once per tick pass, so a lost response
    there needs the HTTP client's own retry to become the failure; here
    the claim sits inside a ``while True:`` loop that polls until
    another build's claim frees up, so another attempt follows by
    construction.
    """

    async def test_a_lost_response_is_recovered_on_the_next_iteration(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The claim commits, its answer is lost, and the re-ask meets
        the build's own claim. Without an identity that is
        ``already_running`` — indistinguishable from a real loss — so the
        build waits out a claim it holds itself and then fails the task.
        """
        task = SyncOnlyTask(name="claim-lost-response")
        registry = ClaimRegistry()
        registry.lose_first_response = True

        summary = await build_aio(
            [task],
            task_executor=FakeDetachedExecutor(),
            registry=registry,
            # Bounded so a regression fails in seconds rather than
            # hanging: minting inside the loop makes the build wait out
            # a claim it holds itself, which is the production symptom
            # and, untimed, a five-minute test.
            claim_config=FAST_CLAIM,
        )

        assert summary.status == BuildExitStatus.SUCCESS
        assert task.complete()
        sent = registry.claim_execution_ids
        assert len(sent) >= 2, f"the loop did not re-ask: {sent}"
        assert sent[0] is not None, "the claim went out with no identity"
        assert len(set(sent)) == 1, (
            "the loop minted a new identity per iteration, so the re-ask "
            f"looked like a second attempt and would be refused: {sent}"
        )

    async def test_a_second_attempt_is_still_refused(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The property the identity must not cost: a different id from
        the same build is a second attempt and is denied, exactly as a
        neighbour's claim is."""
        task = SyncOnlyTask(name="claim-second-attempt")
        registry = ClaimRegistry()

        won = await registry.task_start_claim_aio(uuid4(), task, execution_id=uuid4())
        again = await registry.task_start_claim_aio(uuid4(), task, execution_id=uuid4())

        assert won.started
        assert not again.started
        assert again.denied_reason == "already_running"


class TestTheIdentityTheClaimCarries:
    """Where the resident engine's minted identity goes after the claim.

    Three destinations and two deliberate *absences*, and the absences are
    the part that reads like a bug: a build with no claim of its own has
    no identity to assert, and a start that asserted one anyway would be
    claiming an execution it does not have.
    """

    def _starts(self, registry: ClaimRegistry) -> list[dict]:
        return [
            extra for (method, _, extra) in registry.calls if method == "task_start_aio"
        ]

    async def test_the_spawn_and_the_start_name_the_claims_execution(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """All three — claim, spawn, post-spawn start — name one
        execution, which is what lets the worker inside the container
        name it too."""
        task = SyncOnlyTask(name="claim-identity-carried")
        registry = ClaimRegistry()
        executor = FakeDetachedExecutor()

        await build_aio([task], task_executor=executor, registry=registry)

        claimed = registry.claim_execution_ids[0]
        assert claimed is not None
        assert (
            executor.spawn_execution_ids
            and str(executor.spawn_execution_ids[0]) == claimed
        ), "the spawn was not told which execution it is"
        started = self._starts(registry)
        assert started and str(started[-1]["execution_id"]) == claimed, (
            "the post-spawn start named a different execution from the claim"
        )

    async def test_a_build_that_lost_the_claim_asserts_no_identity(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """Looks like a bug and is the fix.

        Re-attaching means watching *somebody else's* execution. The id
        this build minted belongs to a claim that was refused, so the
        start it still records goes out with none — exactly as this path
        behaved before identities existed. Adopting the winner's id
        instead would assert another build's execution as ours, and the
        server refuses precisely that.
        """
        task = SyncOnlyTask(name="claim-lost-no-identity")
        registry = ClaimRegistry()
        executor = FakeDetachedExecutor(live_refs={"fc-winner"})
        registry.seed_running(task, "fake", "fc-winner")

        await build_aio([task], task_executor=executor, registry=registry)

        started = self._starts(registry)
        assert started, "the re-attach recorded no start at all"
        assert all(extra["execution_id"] is None for extra in started), (
            "a build that lost the claim asserted an execution as its own"
        )


@pytest.mark.parametrize("error_code", ["execution_superseded", "task_cancelled"])
async def test_a_refused_ref_recording_start_stops_the_container_it_orphaned(
    default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
    error_code: str,
):
    """The half of the refusal that the exception handling hides.

    The claim was granted, the spawn went out, and between them the task
    stopped being ours — taken over, or cancelled. The registry refuses
    the reference, and the existing error handling already fails the task
    correctly. What it does not do is stop the container, which is
    running right now with a reference **nothing recorded** — so nothing
    else can ever address it. The handle is in hand exactly here and
    nowhere later.

    Both refusal codes, because they arrive at the same call and mean the
    same thing to this caller: this container is not what the task is
    waiting for.

    **And the build must say nothing about the task.** Propagating the
    refusal sent it through the generic error path into ``process_result``,
    which posts ``TASK_FAILED`` — against a task another build is now
    running, or one that was just cancelled. A failure report writes
    through, so losing the race would have ended with this build marking
    somebody else's live execution failed: the very damage the refusal
    exists to prevent, arriving by the other door. Counted as a local
    failure instead, on the path the claim-loser timeout already uses.
    """
    task = SyncOnlyTask(name=f"orphan-{error_code}")
    registry = ClaimRegistry()
    registry.refuse_ref_recording_start = error_code
    executor = FakeDetachedExecutor()

    # FAIL_FAST (the default) raises the local failure rather than
    # returning a summary — the pre-existing contract for one, and not
    # what this test is about. What matters is *which* failure, and what
    # was said to the registry on the way.
    with pytest.raises(Exception, match="stopped being this build's"):
        await build_aio(
            [task], task_executor=executor, registry=registry, claim_config=FAST_CLAIM
        )

    assert executor.cancel_detached_calls, (
        "the container we spawned and then lost was left running with a "
        "reference nothing recorded"
    )
    cancelled_task_id, _, cancelled_ref = executor.cancel_detached_calls[0]
    assert cancelled_task_id == task.id
    assert cancelled_ref == f"spawned-{task.id}"

    methods = [m for (m, _tid, _extra) in registry.calls]
    assert "task_fail_aio" not in methods, (
        "the build reported a failure for a task that is not its own — "
        f"against a live holder, this releases their claim. Calls: {methods}"
    )
    assert "build_fail_aio" in methods, (
        "losing the task is still this build's failure, recorded against "
        "the build rather than against a task it does not own"
    )


async def test_a_borrowed_handle_is_never_cancelled_on_a_refusal(
    default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
):
    """Stopping the orphan must not become stopping somebody else's worker.

    A handle is not always one this submission created. The claim loser
    re-attaches to the **winner's** execution, and a resumed build adopts
    a reference an earlier run recorded. Cancelling one of those when the
    registry refuses our start would kill a live execution belonging to
    another build — the exact damage the refusal exists to prevent, done
    in its name.

    Here the claim is lost to a live winner, so the handle is borrowed,
    and the start that follows is refused. The orphan-stopping path must
    not fire.
    """
    task = SyncOnlyTask(name="borrowed-handle")
    registry = ClaimRegistry()
    registry.seed_running(task, "fake", "fc-winner")
    registry.refuse_ref_recording_start = "task_cancelled"
    executor = FakeDetachedExecutor(live_refs={"fc-winner"})

    with pytest.raises(Exception, match="stopped being this build's"):
        await build_aio(
            [task], task_executor=executor, registry=registry, claim_config=FAST_CLAIM
        )

    assert executor.cancel_detached_calls == [], (
        "a refusal cancelled an execution this build did not start — "
        f"{executor.cancel_detached_calls}"
    )


async def test_a_refusal_releases_the_global_lock_it_was_holding(
    default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
):
    """A new exit must not become a new leak.

    The global lock is taken **before** the claim and the start, so the
    local-loss result added for a refused start returns while holding it.
    ``process_result``'s ``LockAcquisitionResult`` branch was written for
    the one case where nothing was ever acquired, and released nothing —
    so the lease sat held until its TTL and blocked every other build
    wanting that key.

    The same was already true of a claim denied as already-completed,
    which is why the release went into the branch rather than into this
    one exit.
    """
    from tests.test_build.test_concurrent import MockGlobalLockManager

    task = SyncOnlyTask(name="lock-released-on-refusal")
    registry = ClaimRegistry()
    registry.refuse_ref_recording_start = "execution_superseded"
    executor = FakeDetachedExecutor()
    locks = MockGlobalLockManager()

    with pytest.raises(Exception, match="stopped being this build's"):
        await build_aio(
            [task],
            task_executor=executor,
            registry=registry,
            claim_config=FAST_CLAIM,
            global_lock_manager=typing.cast(typing.Any, locks),
            global_lock_config=GlobalLockConfig(enabled=True),
        )

    assert [tid for tid, _ in locks.releases] == [str(task.id)], (
        "the refusal returned while holding the global lock, which then "
        f"blocked every other build until its TTL. Releases: {locks.releases}"
    )


async def test_a_worker_that_stopped_itself_is_not_reported_as_a_failure(
    default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
):
    """The other door into the same damage.

    A worker that reached a cooperative-cancellation checkpoint and found
    it was no longer wanted raises out of its container, and a detached
    executor reports *any* escaping exception as a task failure. But the
    task that failure would be recorded against is either cancelled or
    running under somebody else, and a failure report writes straight
    through — so a worker politely agreeing to stop would end with this
    build marking a live execution failed.

    The build still fails locally; it just says nothing about a task that
    is not its own.
    """
    task = SyncOnlyTask(name="worker-stopped-itself")
    registry = ClaimRegistry()
    executor = FakeDetachedExecutor(
        spawn_error=None,
        run_error=ExecutionCancelled("no longer wanted"),
    )

    with pytest.raises(ExecutionCancelled):
        await build_aio(
            [task], task_executor=executor, registry=registry, claim_config=FAST_CLAIM
        )

    methods = [m for (m, _tid, _extra) in registry.calls]
    assert "task_fail_aio" not in methods, (
        "a worker that stopped itself was reported as a task failure, which "
        f"against a live holder releases their claim. Calls: {methods}"
    )


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
