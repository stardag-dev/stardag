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
    async def test_cancelled_build_cancels_running_refs(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
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
        assert executor.cancelled_refs == ["fc-run"]
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

    async def test_fail_fast_cancels_running_and_fails(
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
        assert executor.cancelled_refs == ["fc-x"]
        assert registry.build_status == "failed"


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


class TestCancelDynamicDepWindow:
    async def test_cancel_reaches_non_actionable_running(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """A RUNNING task inside the dynamic-dep window (incomplete upstream
        → not actionable) is still cancelled on build cancellation."""
        blocker = SyncOnlyTask(name="cxl-blocker")
        runner = SyncOnlyTask(name="cxl-runner")
        registry, executor = _setup([blocker, runner], auto_complete=False)
        registry.add_task(
            str(runner.id),
            status="running",
            upstreams={str(blocker.id)},  # dynamic edge, blocker incomplete
            executor="fake",
            executor_ref="fc-window",
        )
        registry.build_status = "cancelled"

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            config=FAST_TICK,
        )

        assert summary.terminal_status == "cancelled"
        assert executor.cancelled_refs == ["fc-window"]


class TestCancelAuthority:
    """Whose executions a dying build may stop.

    Authority to revoke is build-scoped. The frontier cannot express that —
    ``running`` is every RUNNING task in the *plan*, which after plan
    closure includes tasks another build claimed — so the tick asks the
    registry which executions are its own.
    """

    async def test_a_neighbours_execution_is_left_alone(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The reported bug. The shared task is in this build's plan and
        RUNNING, but under another build: killing it would take out a live
        worker and release a claim this build never held."""
        (root,) = _chain("shared-root")
        registry, executor = _setup([root], auto_complete=False)
        neighbour = uuid4()
        # Started by the neighbour, not by us: the execution is theirs, and
        # the listing has to attribute it to them rather than to whoever
        # happens to ask.
        registry.add_task(
            str(root.id),
            status="running",
            executor="fake",
            executor_ref="fc-theirs",
            started_by_build=neighbour,
        )
        registry.status_build_id[str(root.id)] = neighbour
        registry.build_status = "cancelled"

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            config=FAST_TICK,
        )

        assert summary.terminal_status == "cancelled"
        assert summary.cancelled_refs == 0
        assert executor.cancelled_refs == []
        assert registry.statuses[str(root.id)] == "running"
        assert ("cancel", str(root.id)) not in registry.calls

    async def test_a_taken_over_task_is_still_this_builds_to_stop(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The state the executions endpoint exists for, reachable here at
        last.

        A cascading cancel releases this build's claims so the next build
        can take those tasks over — and it can, within seconds, long before
        this build's tick runs. From then on the task row names somebody
        else while the container this build started is still going. Stopping
        by current ownership would either miss it or kill the successor.

        Until now the fake filtered its listing on current ownership, so it
        returned nothing in exactly this state and only the live tier could
        catch it. That is why this test is here and not only there.
        """
        (root,) = _chain("taken-over-root")
        registry, executor = _setup([root], auto_complete=False)
        mine = uuid4()
        successor = uuid4()
        # I started it...
        registry.add_task(
            str(root.id),
            status="running",
            executor="fake",
            executor_ref="fc-mine",
            started_by_build=mine,
        )
        # ...and somebody else now holds the task.
        registry.status_build_id[str(root.id)] = successor
        registry.build_status = "cancelled"

        summary = await run_tick_aio(
            mine,
            registry=registry,
            task_executor=executor,
            config=FAST_TICK,
        )

        assert executor.cancelled_refs == ["fc-mine"], (
            "the container this build started was left running because the "
            "task now belongs to someone else"
        )
        assert summary.cancelled_refs == 1

    async def test_a_cascaded_cancel_still_stops_its_own_containers(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """``builds cancel --cascade`` releases the claims server-side, which
        takes the task out of both ``running`` and ``actionable`` while its
        container keeps going. Nothing reached it before this, so the claim
        was released and the execution was not stopped — which is what let a
        second build run the same task concurrently."""
        (root,) = _chain("cascaded-root")
        registry, executor = _setup([root], auto_complete=False)
        registry.add_task(
            str(root.id), status="cancelled", executor="fake", executor_ref="fc-mine"
        )
        registry.build_status = "cancelled"

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            config=FAST_TICK,
        )

        assert summary.terminal_status == "cancelled"
        assert executor.cancelled_refs == ["fc-mine"]
        # Already CANCELLED in the registry: a second event would say
        # nothing the cascade has not already recorded.
        assert ("cancel", str(root.id)) not in registry.calls

    async def test_every_page_of_executions_is_drained(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """One page is not enough, and there is no second chance.

        Stopping an execution records nothing — a cancel is a request, not
        an end — so the listing does not shrink as this pass works through
        it. A single call would leave a wide build's tail running with
        nothing to come back for: a terminal tick does not run again, and a
        build that is no longer RUNNING is not re-flagged.
        """
        tasks: list[BaseTask] = [
            SyncOnlyTask(name=f"wide-{index}") for index in range(5)
        ]
        registry, executor = _setup(tasks, auto_complete=False)
        registry.executions_page_size = 2
        for index, task in enumerate(tasks):
            registry.add_task(
                str(task.id),
                status="running",
                executor="fake",
                executor_ref=f"fc-{index}",
            )
        registry.build_status = "cancelled"

        await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            config=FAST_TICK,
        )

        assert sorted(executor.cancelled_refs) == [f"fc-{i}" for i in range(5)], (
            f"the cancel pass stopped only the first page: {executor.cancelled_refs}"
        )

    async def test_an_execution_that_appears_mid_drain_is_still_stopped(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """A terminal build gets no second tick and nothing re-flags it, so
        anything the drain misses is missed for good. The drain therefore
        re-lists until nothing new comes back, rather than stopping what one
        listing happened to contain."""
        first, second = SyncOnlyTask(name="drain-a"), SyncOnlyTask(name="drain-b")
        registry, executor = _setup([first, second], auto_complete=False)
        registry.add_task(
            str(first.id), status="running", executor="fake", executor_ref="fc-a"
        )
        registry.build_status = "cancelled"

        # The second execution is recorded while the first listing is being
        # acted on — the shape a mid-drain start produces.
        original = registry.build_get_executions_aio

        async def appear_after_first_call(build_id, *, cursor=None):
            result = await original(build_id, cursor=cursor)
            if len(registry.executions_calls) == 1:
                registry.add_task(
                    str(second.id),
                    status="running",
                    executor="fake",
                    executor_ref="fc-b",
                )
            return result

        registry.build_get_executions_aio = appear_after_first_call  # type: ignore[method-assign]

        await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            config=FAST_TICK,
        )

        assert sorted(executor.cancelled_refs) == ["fc-a", "fc-b"], (
            "an execution that appeared while the drain was running was "
            f"never stopped: {executor.cancelled_refs}"
        )

    async def test_a_transient_executions_failure_is_not_a_missing_route(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """Degrading to the frontier here would be silent and total.

        For a cascaded build the frontier sees CANCELLED tasks and therefore
        nothing at all — so a transient failure treated as "this server is
        old" would report nothing to stop, let the tick exit, and leave the
        containers running with no second chance. It errors out instead,
        where it is visible and the cancel can be re-issued.
        """
        (root,) = _chain("transient-root")
        registry, executor = _setup([root], auto_complete=False)
        registry.add_task(
            str(root.id), status="cancelled", executor="fake", executor_ref="fc-mine"
        )
        registry.build_status = "cancelled"
        registry.executions_error = RuntimeError("registry unavailable")

        with pytest.raises(RuntimeError, match="registry unavailable"):
            await run_tick_aio(
                uuid4(),
                registry=registry,
                task_executor=executor,
                config=FAST_TICK,
            )

        assert executor.cancelled_refs == []
        # Reported before it propagates, so the failure is on the build's
        # trail rather than only in a container log.
        assert registry.reported_tick_summaries[-1]["outcome"] == "error"

    async def test_an_old_server_is_filtered_on_the_frontiers_owner_field(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """No executions route, but the frontier does report who holds each
        task — so the tick filters it itself rather than giving up."""
        mine, theirs = SyncOnlyTask(name="mine"), SyncOnlyTask(name="theirs")
        registry, executor = _setup([mine, theirs], auto_complete=False)
        registry.serves_executions = False
        for task, ref in ((mine, "fc-mine"), (theirs, "fc-theirs")):
            registry.add_task(
                str(task.id), status="running", executor="fake", executor_ref=ref
            )
        registry.status_build_id[str(theirs.id)] = uuid4()
        registry.build_status = "cancelled"

        await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            config=FAST_TICK,
        )

        assert executor.cancelled_refs == ["fc-mine"]
        assert registry.statuses[str(theirs.id)] == "running"

    async def test_a_server_that_cannot_say_who_owns_keeps_the_old_behaviour(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """Neither the route nor the owner field. None means "this server
        cannot say", not "not mine" — reading it as the latter would leave a
        build against an old registry unable to stop anything at all, which
        is strictly worse than what it does today."""
        (root,) = _chain("unknowable-root")
        registry, executor = _setup([root], auto_complete=False)
        registry.serves_executions = False
        registry.serves_status_build_id = False
        registry.add_task(
            str(root.id), status="running", executor="fake", executor_ref="fc-run"
        )
        registry.build_status = "cancelled"

        await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            config=FAST_TICK,
        )

        assert executor.cancelled_refs == ["fc-run"]


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

    async def test_cancelled_branch_descendants_also_skipped(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """FAIL_FAST with a second, still-running branch: the cancel pass
        records TASK_CANCELLED (a cancelled task must not dangle RUNNING —
        workers killed by the executor's cancel can't self-report), so the
        cancelled branch's descendants land in the skip closure too."""
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
        assert executor.cancelled_refs == ["ref-live"]
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
