"""Terminal detection, external-blocker classification and the skip/cancel
remedies (stardag.build._reactive._terminal)."""

from __future__ import annotations

import typing
from datetime import datetime, timedelta, timezone
from uuid import uuid4


import pytest

from stardag.build import (
    DetachedExecutionStatus,
    FailMode,
    TickConfig,
    run_tick_aio,
)
from stardag.build._reactive import (
    TickSummary,
    _skip_blocked,
)
from stardag import BaseTask
from stardag.exceptions import NotFoundError
from stardag.registry import (
    NoOpRegistry,
)
from stardag.target import InMemoryFileTarget
from stardag.utils.testing.helper_tasks import SyncOnlyTask

from tests.test_build.reactive_fakes import (
    FAST_TICK,
    _chain,
    _setup,
    registry_body,
)


class TestTerminalHandling:
    async def test_a_cancelled_build_reports_and_stops_nothing(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """A terminal tick reaches into no container (STA-81).

        It used to stop every execution the build had started. Nothing
        does that now: the worker asks at its own checkpoints and exits,
        and an operator with a hard stop to make runs ``stardag builds
        stop`` before the claims are released. What the tick still owes is
        an honest terminal status.
        """
        (root,) = _chain("cancelled-root")
        registry, executor = _setup([root], auto_complete=False)
        registry.add_task(
            str(root.id), status="running", executor="fake", executor_ref="fc-run"
        )
        registry.build_status = "cancelled"

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            config=FAST_TICK,
        )

        assert summary.outcome == "terminal"
        assert summary.terminal_status == "cancelled"
        assert executor.cancelled_refs == []
        assert executor.spawned == []

    async def test_blocked_build_fails_instead_of_idling(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """CONTINUE mode with a failed upstream: nothing runnable/running →
        the tick fails the build rather than idling forever."""
        dep, root = _chain("blocked-dep", "blocked-root")
        registry, executor = _setup([dep, root], auto_complete=False)
        registry.add_task(str(dep.id), status="failed")

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            config=TickConfig(
                linger_seconds=0.3,
                poll_interval_seconds=0.01,
                fail_mode=FailMode.CONTINUE,
            ),
        )

        assert summary.outcome == "terminal"
        assert summary.terminal_status == "failed"
        assert registry.build_status == "failed"

    async def test_fail_fast_fails_the_build_and_stops_no_container(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        dep, root = _chain("ff-dep", "ff-root")
        other = SyncOnlyTask(name="ff-other")
        registry, executor = _setup([dep, root], auto_complete=False)
        registry.metadata_bodies[str(other.id)] = registry_body(other)
        registry.add_task(str(dep.id), status="failed")
        registry.add_task(
            str(other.id), status="running", executor="fake", executor_ref="fc-x"
        )
        executor.probe_statuses["fc-x"] = DetachedExecutionStatus.RUNNING

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            config=FAST_TICK,  # FAIL_FAST default
        )

        assert summary.outcome == "terminal"
        assert summary.terminal_status == "failed"
        assert registry.build_status == "failed"
        # The drain that used to stop ``fc-x`` here is gone (STA-81), and
        # with it the claim release it carried: ``POST /builds/{id}/fail``
        # now releases the build's claims server-side, in the transaction
        # that marks it failed. The worker finds out at its next
        # checkpoint. Asserted because a fail-fast build reaching into a
        # container is exactly the behaviour being withdrawn.
        assert executor.cancelled_refs == []


class TestAddedRootsTerminalDetection:
    async def test_added_roots_gate_completion(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """Roots appended mid-build keep the build running until they too
        complete (previously completion of the original roots stranded
        re-triggered subtrees)."""
        (r1,) = _chain("roots-r1")
        r2 = SyncOnlyTask(name="roots-r2")
        registry, executor = _setup([r1])
        registry.metadata_bodies[str(r2.id)] = registry_body(r2)
        # Original root completed already; new root appended (as the
        # re-trigger path does server-side) but still pending.
        registry.statuses[str(r1.id)] = "completed"
        await registry.build_add_roots_aio(uuid4(), [str(r2.id)])
        registry.add_task(str(r2.id), status="pending")

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            config=FAST_TICK,
        )
        # r2 was spawned (auto-completes) and only then the build completed.
        assert executor.spawned == [r2.id]
        assert summary.terminal_status == "completed"


class TestSkipBlockedOnFailure:
    async def test_fail_fast_skips_blocked_descendants(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """On a failure terminal the tick marks transitively blocked tasks
        skipped — they no longer dangle pending while the build is failed."""
        bad = SyncOnlyTask(name="skip-bad")
        mid = SyncOnlyTask(name="skip-mid", deps=(bad,))
        root = SyncOnlyTask(name="skip-root", deps=(mid,))
        registry, executor = _setup([bad, mid, root], auto_complete=False)
        registry.add_task(str(bad.id), status="failed")

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            config=FAST_TICK,  # FAIL_FAST default
        )

        assert summary.terminal_status == "failed"
        assert summary.skipped == 2
        assert registry.statuses[str(mid.id)] == "skipped"
        assert registry.statuses[str(root.id)] == "skipped"

    async def test_the_skip_count_survives_a_server_that_skips_in_fail(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """Where the count comes from, pinned against the obvious mistake.

        The server completes the blocked closure inside ``/fail``, so the
        ``skip-blocked`` call that follows answers empty. A tick that
        counted only that answer would report zero on the tick that
        skipped everything — and every other test here would still pass,
        because they assert the task *statuses* rather than the number.

        This asserts the number, and that it came from the failure rather
        than from the follow-up: the registry records exactly one
        ``skip_blocked`` call and it contributed nothing.
        """
        bad = SyncOnlyTask(name="fc-bad")
        mid = SyncOnlyTask(name="fc-mid", deps=(bad,))
        root = SyncOnlyTask(name="fc-root", deps=(mid,))
        registry, executor = _setup([bad, mid, root], auto_complete=False)
        registry.add_task(str(bad.id), status="failed")

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            config=FAST_TICK,  # FAIL_FAST default
        )

        assert summary.terminal_status == "failed"
        assert summary.skipped == 2, (
            "the tick lost the skips the failure performed server-side"
        )
        # The follow-up ran and found nothing, which is the state a real
        # server leaves. If this fake ever skips there instead, the
        # assertion above would pass for the wrong reason.
        assert ("skip_blocked", None) in registry.calls
        assert registry.statuses[str(mid.id)] == "skipped"
        assert registry.statuses[str(root.id)] == "skipped"

    async def test_cancelled_branch_descendants_also_skipped(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """FAIL_FAST with a second, still-running branch.

        Failing the build releases the claims it holds, so the running
        branch is CANCELLED by the time skip-blocked runs and its
        descendants land in the closure. **That is why the terminal path
        fails the build before asking for the closure** (STA-81): the
        release used to be a side effect of the cancel drain stopping each
        container, which happened first; now it happens inside
        ``build_fail``, and skipping first would see a RUNNING intermediate,
        which blocks nothing because it may still complete.
        """
        bad = SyncOnlyTask(name="cb-bad")
        long_running = SyncOnlyTask(name="cb-running")
        downstream = SyncOnlyTask(name="cb-downstream", deps=(long_running,))
        root = SyncOnlyTask(name="cb-root", deps=(bad, downstream))
        registry, executor = _setup(
            [bad, long_running, downstream, root], auto_complete=False
        )
        registry.add_task(str(bad.id), status="failed")
        registry.add_task(
            str(long_running.id),
            status="running",
            executor="fake",
            executor_ref="ref-live",
        )
        registry.metadata_bodies[str(long_running.id)] = registry_body(long_running)

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            config=FAST_TICK,  # FAIL_FAST default
        )

        assert summary.terminal_status == "failed"
        # Released by the failure, not stopped by the tick.
        assert executor.cancelled_refs == []
        assert registry.statuses[str(long_running.id)] == "cancelled"
        assert registry.statuses[str(downstream.id)] == "skipped"
        assert registry.statuses[str(root.id)] == "skipped"

    async def test_blocked_terminal_in_continue_mode_also_skips(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        dep, root = _chain("skip-cont-dep", "skip-cont-root")
        registry, executor = _setup([dep, root], auto_complete=False)
        registry.add_task(str(dep.id), status="failed")

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            config=TickConfig(
                linger_seconds=0.2,
                poll_interval_seconds=0.01,
                fail_mode=FailMode.CONTINUE,
            ),
        )

        assert summary.terminal_status == "failed"
        assert registry.statuses[str(root.id)] == "skipped"


class _SkipBlocked404Registry(NoOpRegistry):
    """Registry whose skip-blocked endpoint 404s with a given detail."""

    def __init__(self, detail: str):
        super().__init__()
        self.detail = detail

    async def build_skip_blocked_aio(self, build_id) -> list[str]:
        raise NotFoundError(
            "Skip blocked tasks: resource not found", detail=self.detail
        )


class TestSkipBlockedErrorHandling:
    async def test_missing_route_tolerated(self):
        """Old server without the endpoint (FastAPI default 404) → skip
        silently omitted, no raise."""
        summary = TickSummary(outcome="noop")
        await _skip_blocked(_SkipBlocked404Registry("Not Found"), uuid4(), summary)
        assert summary.skipped == 0

    async def test_app_level_404_reraised(self):
        """A 404 raised inside the endpoint (e.g. build no longer exists)
        signals a registry inconsistency and must propagate."""
        with pytest.raises(NotFoundError):
            await _skip_blocked(
                _SkipBlocked404Registry("Build not found"),
                uuid4(),
                TickSummary(outcome="noop"),
            )


class TestRevokedTasksInPlan:
    """A cancelled or skipped task in this build's plan is this build's to run.

    Builds collaborate; the claim is the only cross-build coordination. Since
    dependency edges are scoped to the build's structure scope, the server
    lists a cancelled or skipped task as *actionable* once every upstream in
    that scope is complete, and the tick resets it within the attempt budget
    and spawns it in the same pass. Nothing here consults another build's
    liveness: a cancel is a revocation of permission to run, a skip is
    derived from an upstream that has since completed, and neither is a
    verdict on the task.
    """

    def _revoked_build(
        self, *, status: str = "cancelled", attempts: int = 0
    ) -> tuple[BaseTask, BaseTask, typing.Any, typing.Any]:
        """A real two-task chain whose upstream another build left ``status``."""
        blocker, root = _chain("revoked-blocker", "revoked-root")
        registry, executor = _setup([blocker, root], auto_complete=False)
        registry.add_blocking_task(
            str(blocker.id),
            blocks={str(root.id)},
            status=status,
            in_build=True,
            attempt_count=attempts,
            namespace="pipelines",
            name="Ingest",
        )
        return blocker, root, registry, executor

    async def test_cancelled_task_in_plan_is_reset_and_run_in_the_same_pass(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The motivating shape is fail-fast. Build A starts a shared task,
        hits an unrelated failure, and cascade-cancels the tasks it started
        — correctly, since it owns those claims. The task is left CANCELLED,
        which is not schedulable, so build B — which shares the dependency
        and has failed at nothing — used to die on it too.

        Nothing owns the task now: no claim, no live execution. It is in B's
        plan and gated open, so B resets it and runs it — in the pass that
        saw it, not a later tick: the registry's wake-up flag deliberately
        skips the build whose own event caused a change, so a tick that reset
        the task and lingered would wait for news it had already heard.
        """
        blocker, _, registry, executor = self._revoked_build()

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            config=TickConfig(
                linger_seconds=0.2,
                poll_interval_seconds=0.01,
                fail_mode=FailMode.CONTINUE,
            ),
        )

        assert summary.terminal_status is None
        assert ("retry", str(blocker.id)) in registry.calls
        assert summary.in_build_blockers_reset == 1
        assert executor.spawned == [blocker.id], (
            "the reset task must be spawned by the pass that reset it: "
            f"{executor.spawned}"
        )
        assert summary.iterations >= 2, (
            "the tick must re-read the frontier after acting, not linger on "
            "a snapshot it has already invalidated"
        )
        assert registry.build_error_message is None

    async def test_a_reset_that_fails_does_not_count_as_progress(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The counter is read as "the frontier changed, act again now".

        So a reset that raised must not increment it, nor set ``acted``. If it
        did, the tick would loop straight back, re-read the same task, fail
        the same reset and refresh its own linger deadline — spinning for as
        long as the retry keeps failing, which one transient registry error
        is enough to start.
        """
        blocker, _, registry, executor = self._revoked_build()
        registry.retry_error = RuntimeError("registry unavailable")

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            config=TickConfig(
                linger_seconds=0.2,
                poll_interval_seconds=0.01,
                fail_mode=FailMode.CONTINUE,
            ),
        )

        assert summary.in_build_blockers_reset == 0
        assert ("retry-failed", str(blocker.id)) in registry.calls
        assert executor.spawned == []
        # One pass, then the linger — not a spin.
        assert summary.iterations == 1, (
            "a failed reset sent the tick round the loop again: "
            f"{summary.iterations} iterations"
        )

    async def test_a_shared_cancelled_task_is_reset_once(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """A cancelled upstream shared by several of this build's tasks is one
        actionable entry, so one reset — however many dependents it gates."""
        blocker, root, registry, executor = self._revoked_build()
        sibling = SyncOnlyTask(name="shared-blocker-sibling")
        registry.metadata_bodies[str(sibling.id)] = registry_body(sibling)
        registry.add_task(str(sibling.id), status="pending")
        registry.upstreams.setdefault(str(sibling.id), set()).add(str(blocker.id))

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            config=TickConfig(
                linger_seconds=0.2,
                poll_interval_seconds=0.01,
                fail_mode=FailMode.CONTINUE,
            ),
        )

        assert summary.terminal_status is None
        retries = [
            call for call in registry.calls if call == ("retry", str(blocker.id))
        ]
        assert len(retries) == 1, registry.calls
        assert summary.in_build_blockers_reset == 1

    async def test_the_reset_respects_the_attempt_budget(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """Resetting must not become an infinite loop.

        A task that is cancelled on every attempt would otherwise be reset,
        rerun and re-cancelled forever. The budget that bounds ordinary
        retries bounds this one too; over it the task is inert, and the
        build fails once nothing else can run — naming the task and the
        remedy (a re-trigger starts a new round).
        """
        blocker, _, registry, executor = self._revoked_build(attempts=5)

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            config=TickConfig(
                linger_seconds=0.2,
                poll_interval_seconds=0.01,
                fail_mode=FailMode.CONTINUE,
                max_attempts=2,
            ),
        )

        assert summary.terminal_status == "failed"
        assert ("retry", str(blocker.id)) not in registry.calls
        assert summary.revoked_budget_spent >= 1
        assert executor.spawned == []
        message = registry.build_error_message or ""
        assert "attempt budget in this build is spent" in message
        assert str(blocker.id) in message
        assert "Re-trigger" in message

    async def test_a_stale_skip_is_reset_and_run(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """A skip is derived: it marks a task downstream of something that
        failed or was cancelled. If every upstream is now complete — another
        build reset and completed it — the reason for the skip is gone, and
        leaving the task skipped would wedge the build until a re-trigger.
        """
        blocker, _, registry, executor = self._revoked_build(status="skipped")

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            config=TickConfig(
                linger_seconds=0.2,
                poll_interval_seconds=0.01,
                fail_mode=FailMode.CONTINUE,
            ),
        )

        assert summary.terminal_status is None
        assert ("retry", str(blocker.id)) in registry.calls
        assert executor.spawned == [blocker.id]

    async def test_a_skip_whose_cause_still_stands_is_not_touched(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """ "Skipped because an upstream will never complete" and "gated open"
        are disjoint, so the skip pass and the reset cannot oscillate: a
        skipped task with a FAILED upstream is not actionable, and the build
        fails on the result its fail_mode owns."""
        dep, mid, root = _chain("skip-dep", "skip-mid", "skip-root")
        registry, executor = _setup([dep, mid, root], auto_complete=False)
        registry.add_task(str(dep.id), status="failed")
        registry.add_task(str(mid.id), status="skipped")

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            config=TickConfig(
                linger_seconds=0.2,
                poll_interval_seconds=0.01,
                fail_mode=FailMode.CONTINUE,
            ),
        )

        assert summary.terminal_status == "failed"
        assert ("retry", str(mid.id)) not in registry.calls
        assert registry.statuses[str(mid.id)] == "skipped"
        assert executor.spawned == []

    async def test_a_failed_task_in_plan_is_left_to_fail_mode(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The line the collaboration rule stops at: FAILED is a *result*.

        Build A ran the shared task and it failed. Nothing about that failure
        belongs to B: resetting it would rerun a task that just told the
        environment it does not work, on nobody's request, and would override
        the ``fail_mode`` B was triggered with. B fails instead, and the
        message says a re-trigger — where the user *does* ask — resets it.
        """
        blocker, _, registry, executor = self._revoked_build(status="failed")

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            config=TickConfig(
                linger_seconds=0.2,
                poll_interval_seconds=0.01,
                fail_mode=FailMode.CONTINUE,
                max_attempts=2,
            ),
        )

        assert summary.terminal_status == "failed"
        assert ("retry", str(blocker.id)) not in registry.calls
        assert registry.statuses[str(blocker.id)] == "failed"
        assert summary.in_build_blockers_reset == 0
        message = registry.build_error_message or ""
        assert "No runnable or running tasks left" in message
        assert "fail_mode" in message
        assert "re-trigger" in message.lower()

    async def test_a_running_shared_task_is_probed_not_diagnosed(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """A shared upstream another build is executing is in this build's
        plan and RUNNING, so it shows up in ``running`` and the tick waits on
        it like any execution of its own. No blocker classification, no owner
        lookup: the claim's expiry is the whole answer, read by the probe."""
        blocker, root, registry, executor = self._revoked_build(status="running")
        registry.refs[str(blocker.id)] = ("fake", "fc-other-build")
        registry.expires_at[str(blocker.id)] = datetime.now(timezone.utc) + timedelta(
            hours=1
        )
        executor.probe_statuses["fc-other-build"] = DetachedExecutionStatus.RUNNING

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            config=FAST_TICK,
        )

        assert summary.outcome == "lingered_out"
        assert registry.build_status == "running"
        assert ("retry", str(blocker.id)) not in registry.calls
        assert executor.spawned == []
        assert registry.build_get_calls == [], (
            "no owner-liveness lookup: the claim's expiry is the only evidence "
            "a tick needs about a shared RUNNING task"
        )


class TestConstants:
    def test_discovery_is_bounded_lower_than_frontier_actions(self):
        """Discovery is limited by the target backend, not the registry.

        Measured live: a 64-task layer against a Modal volume completed at 16
        in flight, stalled at 32 and failed at 50. Sharing one constant with
        the registry-bound actions conflated two different ceilings.
        """
        from stardag.build._reactive import (
            _DEFAULT_MAX_CONCURRENCY,
            _DEFAULT_MAX_CONCURRENT_DISCOVER,
        )

        assert _DEFAULT_MAX_CONCURRENT_DISCOVER < _DEFAULT_MAX_CONCURRENCY
