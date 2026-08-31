"""The tick loop: lease, linger poll, exit handshake, summary reporting
(stardag.build._reactive._tick)."""

from __future__ import annotations

import dataclasses
import json
import logging
import typing
from uuid import UUID, uuid4


import pytest

from stardag.build import (
    DetachedExecutionStatus,
    TickConfig,
    run_tick_aio,
)
from stardag.build._reactive import _tick as tick_module
from stardag.build._reactive import (
    TickSummary,
)
from stardag.exceptions import NotFoundError
from stardag.target import InMemoryFileTarget

from tests.test_build.reactive_fakes import (
    FAST_TICK,
    FakeReactiveRegistry,
    FakeTickExecutor,
    _chain,
    _setup,
)


class TestTickHappyPath:
    async def test_completes_chain_within_one_lingering_tick(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """Instant workers: one tick drives dep → root → BUILD_COMPLETED via
        linger wake-ups, spawning in dependency order."""
        dep, root = _chain("tick-dep", "tick-root")
        registry, executor, store = _setup([dep, root])

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=FAST_TICK,
        )

        assert summary.outcome == "terminal"
        assert summary.terminal_status == "completed"
        assert executor.spawned == [dep.id, root.id]
        assert registry.build_status == "completed"
        assert summary.spawned == 2

    async def test_lease_held_is_noop(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        dep, root = _chain("lease-dep", "lease-root")
        registry, executor, store = _setup([dep, root], lease_acquired=False)

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=FAST_TICK,
        )

        assert summary.outcome == "lease_held"
        assert executor.spawned == []
        assert registry.calls == []

    async def test_not_reactive_build_is_noop(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        (root,) = _chain("not-reactive-root")
        registry, executor, store = _setup([root])
        # No reactive_app_name on the frontier → not a reactively-scheduled
        # build; the tick must not act on it.
        registry.reactive_app_name = None

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=FAST_TICK,
        )

        assert summary.outcome == "not_reactive"
        assert executor.spawned == []
        # No scheduling happened — the tick observed reactive_meta is None on
        # its first frontier fetch and bailed before acting.
        assert registry.build_status == "running"


class TestTickSummaryReporting:
    """B6: a tick's own account of what it did must outlive its container.

    Reporting is strictly best-effort — it sits at the end of every tick,
    and observability failing must never fail a tick or change its
    outcome. These tests pin both halves: that the summaries do get
    reported, and that nothing about reporting can leak into the result.
    """

    @pytest.fixture(autouse=True)
    def _reset_route_flag(self):
        """The missing-route latch is process-global; isolate the tests."""
        tick_module._tick_summary_route_missing = False
        yield
        tick_module._tick_summary_route_missing = False

    async def _run(
        self,
        registry: FakeReactiveRegistry,
        executor,
        store,
        config: TickConfig | None = None,
    ) -> TickSummary:
        return await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=config or FAST_TICK,
        )

    async def test_terminal_tick_is_reported(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        dep, root = _chain("report-dep", "report-root")
        registry, executor, store = _setup([dep, root])

        summary = await self._run(registry, executor, store)

        assert summary.outcome == "terminal"
        assert len(registry.reported_tick_summaries) == 1
        reported = registry.reported_tick_summaries[0]
        # The whole dataclass rides, so a field added later needs no server
        # change (the summary is stored as an open blob).
        assert reported["outcome"] == "terminal"
        assert reported["terminal_status"] == "completed"
        assert reported["spawned"] == 2
        assert set(reported) == set(vars(summary))

    async def test_lingered_out_tick_is_reported(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """A tick that found nothing to do still says so."""
        (task,) = _chain("linger-only")
        registry, executor, store = _setup([task], auto_complete=False)
        # Nothing actionable and something running -> not terminal, so the
        # tick lingers and exits on its deadline.
        registry.statuses[str(task.id)] = "running"
        registry.refs[str(task.id)] = ("fake", "ref-live")
        executor.probe_statuses["ref-live"] = DetachedExecutionStatus.RUNNING

        summary = await self._run(registry, executor, store)

        assert summary.outcome == "lingered_out"
        assert [s["outcome"] for s in registry.reported_tick_summaries] == [
            "lingered_out"
        ]

    async def test_lease_held_tick_is_reported(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """Contention is signal: many of these means ticks are piling up."""
        dep, root = _chain("held-dep", "held-root")
        registry, executor, store = _setup([dep, root], lease_acquired=False)

        summary = await self._run(registry, executor, store)

        assert summary.outcome == "lease_held"
        assert [s["outcome"] for s in registry.reported_tick_summaries] == [
            "lease_held"
        ]

    async def test_not_reactive_tick_is_not_reported(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """A stray tick on a non-reactive build learnt nothing worth keeping."""
        dep, root = _chain("stray-dep", "stray-root")
        registry, executor, store = _setup([dep, root])
        registry.reactive_app_name = None

        summary = await self._run(registry, executor, store)

        assert summary.outcome == "not_reactive"
        assert registry.reported_tick_summaries == []

    async def test_disabled_by_config(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        dep, root = _chain("off-dep", "off-root")
        registry, executor, store = _setup([dep, root])

        summary = await self._run(
            registry,
            executor,
            store,
            config=TickConfig(
                linger_seconds=0.3,
                poll_interval_seconds=0.01,
                report_tick_summaries=False,
            ),
        )

        assert summary.outcome == "terminal"
        assert registry.reported_tick_summaries == []

    async def test_reporting_failure_does_not_affect_the_tick(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The contract: a broken registry must not fail or alter a tick."""
        dep, root = _chain("raise-dep", "raise-root")
        registry, executor, store = _setup([dep, root])
        registry.tick_summary_error = RuntimeError("registry exploded")

        summary = await self._run(registry, executor, store)

        assert summary.outcome == "terminal"
        assert summary.terminal_status == "completed"
        assert summary.spawned == 2
        assert registry.build_status == "completed"
        # It was attempted — the failure is swallowed, not skipped.
        assert len(registry.reported_tick_summaries) == 1

    async def test_missing_route_is_tolerated_and_latched(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """An older server 404s the route; don't pay for it every tick."""
        dep, root = _chain("route-dep", "route-root")
        registry, executor, store = _setup([dep, root])
        # FastAPI's generic missing-route body — version skew, not an error.
        registry.tick_summary_error = NotFoundError(
            "Report tick summary: resource not found", detail="Not Found"
        )

        summary = await self._run(registry, executor, store)
        assert summary.outcome == "terminal"
        assert len(registry.reported_tick_summaries) == 1
        assert tick_module._tick_summary_route_missing is True

        # Second tick on a fresh build: the latch keeps it from re-trying.
        dep2, root2 = _chain("route-dep-2", "route-root-2")
        registry2, executor2, store2 = _setup([dep2, root2])
        summary2 = await self._run(registry2, executor2, store2)
        assert summary2.outcome == "terminal"
        assert registry2.reported_tick_summaries == []

    async def test_resource_404_does_not_latch(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """A build that vanished is not a reason to stop reporting others."""
        dep, root = _chain("gone-dep", "gone-root")
        registry, executor, store = _setup([dep, root])
        registry.tick_summary_error = NotFoundError(
            "Report tick summary: resource not found", detail="Build not found"
        )

        summary = await self._run(registry, executor, store)

        assert summary.outcome == "terminal"
        assert tick_module._tick_summary_route_missing is False

    async def test_crashed_tick_is_reported_and_the_exception_propagates(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """A crashed tick is the most informative "why did this stall?" answer.

        Recording it must not change what the caller sees: the original
        exception is re-raised, with its type and message captured.
        """
        dep, root = _chain("crash-dep", "crash-root")
        registry, executor, store = _setup([dep, root])
        registry.frontier_error = RuntimeError("frontier query exploded")

        with pytest.raises(RuntimeError, match="frontier query exploded"):
            await self._run(registry, executor, store)

        assert len(registry.reported_tick_summaries) == 1
        reported = registry.reported_tick_summaries[0]
        assert reported["outcome"] == "error"
        assert reported["error_type"] == "RuntimeError"
        assert reported["error_message"] == "frontier query exploded"

    async def test_error_message_is_bounded(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """An unbounded message would blow the server's 8 KiB summary cap —
        turning a recorded failure into no record at all."""
        dep, root = _chain("huge-dep", "huge-root")
        registry, executor, store = _setup([dep, root])
        registry.frontier_error = RuntimeError("x" * 50_000)

        with pytest.raises(RuntimeError):
            await self._run(registry, executor, store)

        reported = registry.reported_tick_summaries[0]
        message = reported["error_message"]
        assert len(message) == tick_module._MAX_ERROR_MESSAGE_CHARS
        assert message.endswith(tick_module._TRUNCATION_MARKER)
        # The whole summary stays well inside the server's cap.
        assert len(json.dumps(reported, separators=(",", ":")).encode()) < 8192

    async def test_failing_to_report_a_crash_does_not_mask_it(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """A failure to record the failure is swallowed, never substituted."""
        dep, root = _chain("mask-dep", "mask-root")
        registry, executor, store = _setup([dep, root])
        registry.frontier_error = RuntimeError("the original problem")
        registry.tick_summary_error = RuntimeError("and the reporter died too")

        with pytest.raises(RuntimeError, match="the original problem"):
            await self._run(registry, executor, store)

    async def test_crash_reporting_respects_the_config_toggle(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        dep, root = _chain("crash-off-dep", "crash-off-root")
        registry, executor, store = _setup([dep, root])
        registry.frontier_error = RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            await self._run(
                registry,
                executor,
                store,
                config=TickConfig(
                    linger_seconds=0.3,
                    poll_interval_seconds=0.01,
                    report_tick_summaries=False,
                ),
            )

        assert registry.reported_tick_summaries == []


# =============================================================================
# Concurrent DAG discovery
# =============================================================================


class TestLingerPollCost:
    """A lingering tick with nothing to do must not read the frontier.

    The poll asks one question — "has anything changed?" — every few
    seconds, per lingering build. It used to ask it by fetching the whole
    frontier: seven statements, one of them a window-function aggregate over
    the event log, of which it read a single boolean.
    """

    async def test_linger_poll_reads_the_flag_not_the_frontier(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        (root,) = _chain("poll-cost-root")
        registry, executor, store = _setup([root], auto_complete=False)
        registry.add_task(
            str(root.id),
            status="running",
            executor="other-backend",
            executor_ref="job-1",
        )

        frontier_reads: list[int] = []
        notify_reads: list[int] = []
        real_frontier = registry.build_get_frontier_aio
        real_notify = registry.build_get_notify_aio

        async def counting_frontier(bid):
            frontier_reads.append(1)
            return await real_frontier(bid)

        async def counting_notify(bid):
            notify_reads.append(1)
            return await real_notify(bid)

        registry.build_get_frontier_aio = counting_frontier  # type: ignore[method-assign]
        registry.build_get_notify_aio = counting_notify  # type: ignore[method-assign]

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=dataclasses.replace(
                FAST_TICK, linger_seconds=0.2, poll_interval_seconds=0.02
            ),
        )

        assert summary.outcome == "lingered_out"
        # Several polls happened — otherwise this proves nothing.
        assert len(notify_reads) >= 3, notify_reads
        # ...and the frontier was read once, by the pass itself. Not once
        # per poll. The exact number is the point of the test, so it is
        # asserted rather than bounded.
        assert len(frontier_reads) == 1, (
            f"the linger poll read the frontier {len(frontier_reads) - 1} "
            "extra time(s); it must ask the one-row flag endpoint"
        )


class TestSchedulerLease:
    """The single-flight lease, now a column on the build row.

    It used to ride on the deprecated global concurrency lock, which meant
    every reader assembled a lock name from a build id and queried a second
    table to answer "does this build have a scheduler?".
    """

    async def test_a_held_lease_makes_the_tick_a_no_op(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        (root,) = _chain("lease-held-root")
        registry, executor, store = _setup([root], lease_acquired=False)

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=FAST_TICK,
        )

        assert summary.outcome == "lease_held"
        assert executor.spawned == [], "a refused tick must not schedule"

    async def test_losing_the_lease_stops_the_tick(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
        monkeypatch,
    ):
        """A renewal answering "not yours" means the lease already lapsed
        and a successor took it over. Carrying on would be exactly the
        double-scheduling the lease exists to prevent, so the tick stops
        instead — the successor holds the flag and will act on it.
        """
        (root,) = _chain("lease-lost-root")
        registry, executor, store = _setup([root], auto_complete=False)
        registry.add_task(
            str(root.id),
            status="running",
            executor="other-backend",
            executor_ref="job-1",
        )
        # A successor took it over, so both the renewal and the re-acquire
        # behind it are refused. The renewal interval is 20 s in production,
        # far longer than this test lingers.
        monkeypatch.setattr(tick_module, "_LEASE_RENEW_INTERVAL_SECONDS", 0.01)
        registry.lease_stolen = True
        # Land a wake-up in the release window, so a tick that still held
        # the build *would* legitimately hand off. Without this the flag is
        # already cleared by exit time and the assertion below would hold
        # for the wrong reason — it would be about having nothing to hand
        # off rather than about having no standing to.
        registry.reactive_app_name = "my-app"
        registry.lease_on_release = lambda: setattr(registry, "needs_tick", True)
        spawned: list[UUID] = []

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=dataclasses.replace(
                FAST_TICK,
                linger_seconds=5.0,
                poll_interval_seconds=0.01,
                spawn_tick=lambda bid, app: spawned.append(bid),
            ),
        )

        assert summary.outcome == "lease_lost"
        # And it must not hand off. A successor already holds the lease, so
        # spawning one would only add a container that finds it held and
        # exits — the tick that lost the build has no standing to schedule
        # for it.
        assert summary.successor_spawned == 0
        assert spawned == []
        # And the exit release was refused rather than honoured: clearing
        # the successor's lease on the way out is exactly what the owner
        # check exists to prevent.
        assert registry.lease_owner == "__successor__", (
            "the lost tick's exit release cleared the successor's lease"
        )

    async def test_a_lapsed_lease_is_re_taken_rather_than_abandoning_the_build(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
        monkeypatch,
    ):
        """A refused renewal usually means our own lease expired between
        renewals — not that anything took it over, since nothing is normally
        competing for the build. Stopping there would abandon a build nobody
        else is driving, which is worse than the double-scheduling the check
        guards against. So the tick re-acquires and carries on, and only a
        re-acquire that *also* fails is a real loss.
        """
        (root,) = _chain("lease-lapse-root")
        registry, executor, store = _setup([root], auto_complete=False)
        registry.add_task(
            str(root.id),
            status="running",
            executor="other-backend",
            executor_ref="job-1",
        )
        monkeypatch.setattr(tick_module, "_LEASE_RENEW_INTERVAL_SECONDS", 0.01)
        registry.lease_lapsed = True

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=dataclasses.replace(
                FAST_TICK, linger_seconds=0.15, poll_interval_seconds=0.01
            ),
        )

        assert summary.outcome == "lingered_out", (
            "a lease that merely lapsed must be re-taken, not treated as lost"
        )

    async def test_the_lease_is_released_on_the_way_out(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """Not left to expire: the next tick for this build should not have
        to wait out a TTL for a scheduler that already finished."""
        (root,) = _chain("lease-release-root")
        registry, executor, store = _setup([root])
        released: list[bool] = []
        registry.lease_on_release = lambda: released.append(True)

        await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=FAST_TICK,
        )

        assert released == [True]
        assert registry.lease_owner is None


class TestExitHandshake:
    """The two re-reads that make a *conditional* wake-up safe.

    A worker may skip spawning a tick when the registry reports a
    scheduler live (``BuildNotifyResult.scheduler_live``). That is only
    sound if a live scheduler cannot exit past a flag set before it
    released the lease — which the plain "poll until the deadline, then
    unwind" shape does not guarantee. See ``_run_tick_body_aio``.
    """

    def _spawner(self) -> tuple[list[UUID], typing.Callable[[UUID, str], None]]:
        spawned: list[UUID] = []

        def spawn(build_id: UUID, app_name: str) -> None:
            spawned.append(build_id)

        return spawned, spawn

    async def test_flag_set_during_release_spawns_a_successor(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The window this whole handshake exists for: the flag lands after
        the tick's last look but before the lease is gone, and the worker
        that set it saw the lease held and did not spawn. Nobody would
        schedule."""
        (root,) = _chain("handoff-root")
        executor = FakeTickExecutor(
            statuses={"fc-live": DetachedExecutionStatus.RUNNING}
        )
        registry, executor, store = _setup(
            [root], auto_complete=False, executor=executor
        )
        registry.add_task(
            str(root.id), status="running", executor="fake", executor_ref="fc-live"
        )
        # The worker's notify lands exactly as the lease is released.
        registry.lease_on_release = lambda: setattr(registry, "needs_tick", True)

        spawned, spawn = self._spawner()
        build_id = uuid4()
        summary = await run_tick_aio(
            build_id,
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=dataclasses.replace(FAST_TICK, spawn_tick=spawn),
        )

        assert summary.outcome == "lingered_out"
        assert summary.successor_spawned == 1
        assert spawned == [build_id]
        # The flag is still set, so the successor has something to clear.
        assert registry.needs_tick is True

    async def test_flag_set_before_release_extends_the_linger(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The fast half: the flag arrives between the last poll and the
        deadline, so the tick still holds the lease and simply keeps it —
        no successor container, no cold start."""
        (root,) = _chain("extend-root")
        registry, executor, store = _setup([root], auto_complete=False)
        registry.add_task(
            str(root.id),
            status="running",
            executor="other-backend",
            executor_ref="job-1",
        )

        # Land the wake-up in the gap deterministically. With one poll per
        # linger (poll interval > linger), the *flag* reads are: 1 the
        # single linger poll, 2 the pre-release re-check. Setting the flag
        # as read 1 answers "no" puts it exactly between the last poll and
        # the deadline — the gap the re-check exists for.
        #
        # These are notify reads, not frontier reads: the linger poll asks
        # the one-row endpoint now, so hooking the frontier would no longer
        # land the flag in this window at all.
        reads: list[int] = []
        real_notify = registry.build_get_notify_aio

        async def notify_then_set(bid):
            result = await real_notify(bid)
            reads.append(1)
            if len(reads) == 1:
                # Complete the target too, so the extended pass reaches a
                # terminal state instead of lingering out a second time.
                root.run()
                registry.needs_tick = True
            return result

        registry.build_get_notify_aio = notify_then_set  # type: ignore[method-assign]

        spawned, spawn = self._spawner()
        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=dataclasses.replace(
                FAST_TICK,
                linger_seconds=0.02,
                poll_interval_seconds=0.05,
                spawn_tick=spawn,
            ),
        )

        # It re-acted under its own lease and finished the build there.
        assert summary.outcome == "terminal"
        assert summary.terminal_status == "completed"
        assert summary.linger_extended >= 1
        assert spawned == [], "kept the lease, so no successor was needed"

    async def test_quiet_exit_spawns_nothing(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The overwhelmingly common exit: nothing was notified, so neither
        half of the handshake fires."""
        (root,) = _chain("quiet-root")
        registry, executor, store = _setup([root], auto_complete=False)
        registry.add_task(
            str(root.id),
            status="running",
            executor="other-backend",
            executor_ref="job-1",
        )

        spawned, spawn = self._spawner()
        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=dataclasses.replace(FAST_TICK, spawn_tick=spawn),
        )

        assert summary.outcome == "lingered_out"
        assert summary.linger_extended == 0
        assert summary.successor_spawned == 0
        assert spawned == []

    async def test_terminal_exit_does_not_hand_off(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """A finished build has nothing a successor could act on, so the
        post-release read is skipped even with the flag set."""
        (root,) = _chain("terminal-handoff-root")
        registry, executor, store = _setup([root], auto_complete=True)
        registry.lease_on_release = lambda: setattr(registry, "needs_tick", True)

        spawned, spawn = self._spawner()
        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=dataclasses.replace(FAST_TICK, spawn_tick=spawn),
        )

        assert summary.outcome == "terminal"
        assert summary.terminal_status == "completed"
        assert registry.needs_tick is True  # a late worker report
        assert spawned == [], "a finished build needs no successor tick"

    async def test_lease_held_tick_does_not_hand_off(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """A tick that never held the lease has no window to close — the
        holder does. Handing off from here would spawn a tick per wake-up
        again, which is the cost this removes."""
        (root,) = _chain("held-handoff-root")
        registry, executor, store = _setup(
            [root], auto_complete=False, lease_acquired=False
        )
        registry.needs_tick = True

        spawned, spawn = self._spawner()
        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=dataclasses.replace(FAST_TICK, spawn_tick=spawn),
        )

        assert summary.outcome == "lease_held"
        assert spawned == []

    async def test_crashing_tick_hands_off_a_wakeup_that_lands_as_it_dies(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """A wake-up landing while a *crashing* tick unwinds is in the same
        release window as one landing while a healthy tick exits, and the
        worker that set it skipped its spawn either way. The hand-off is in
        a ``finally`` for exactly that — so an exception cannot bypass the
        release-window check.

        Note what is being asserted: the tick completes a pass (clearing
        the flag) and dies on the *second*, with a new notify arriving as it
        goes. It is that new wake-up which is handed off — not the one the
        first pass had already cleared and taken responsibility for. See
        ``test_a_crash_before_the_first_clear_hands_nothing_on``.
        """
        (root,) = _chain("crash-handoff-root")
        registry, executor, store = _setup([root], auto_complete=False)
        registry.add_task(
            str(root.id),
            status="running",
            executor="other-backend",
            executor_ref="job-1",
        )

        boom = RuntimeError("tick died mid-pass")
        real_clear = registry.build_clear_notify_aio
        clears: list[int] = []

        async def clear_then_die_on_the_second_pass(bid):
            clears.append(1)
            if len(clears) >= 2:
                # A worker reports as this tick dies.
                registry.needs_tick = True
                raise boom
            await real_clear(bid)
            # Provokes that second pass: the linger poll sees the flag.
            registry.needs_tick = True

        registry.build_clear_notify_aio = (  # type: ignore[method-assign]
            clear_then_die_on_the_second_pass
        )

        spawned, spawn = self._spawner()
        build_id = uuid4()
        with pytest.raises(RuntimeError, match="tick died mid-pass"):
            await run_tick_aio(
                build_id,
                registry=registry,
                task_executor=executor,
                task_store=store,
                config=dataclasses.replace(FAST_TICK, spawn_tick=spawn),
            )

        assert spawned == [build_id]

    async def test_a_crash_before_the_first_clear_hands_nothing_on(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The cascade guard, and the reason it exists.

        Clearing the flag is the first thing every pass does. If that call
        is what keeps failing — a rate-limited or 5xx ``DELETE /notify``
        while the frontier ``GET`` stays healthy — then a tick that handed
        off on the way out would be replaced by a successor that fails
        identically, with the flag still set because the clear never
        happened: one cold container after another, forever.

        A tick that never cleared anything has taken responsibility for
        nothing, so it hands nothing on.
        """
        (root,) = _chain("cascade-guard-root")
        registry, executor, store = _setup([root], auto_complete=False)
        registry.needs_tick = True  # and it stays set: the clear never lands

        async def clear_always_fails(bid):
            raise ConnectionError("registry refused the clear")

        registry.build_clear_notify_aio = clear_always_fails  # type: ignore[method-assign]

        spawned, spawn = self._spawner()
        with pytest.raises(ConnectionError, match="refused the clear"):
            await run_tick_aio(
                uuid4(),
                registry=registry,
                task_executor=executor,
                task_store=store,
                config=dataclasses.replace(FAST_TICK, spawn_tick=spawn),
            )

        assert spawned == [], (
            "handed off after failing to clear the flag: the successor would "
            "fail the same way, and the chain would never end"
        )

    async def test_a_tick_that_clears_then_crashes_is_not_crash_recovery(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The honest limit of the handshake, asserted so nobody documents
        it as more than it is.

        A tick that clears the flag and then dies leaves the flag false, so
        the hand-off has nothing to see and the wake-up it had taken
        responsibility for waits for the next completion or the watchdog.
        Unchanged from before the handshake existed — it closes the release
        window, it does not resurrect a crashed tick's own wake-up.
        """
        (root,) = _chain("crash-after-clear-root")
        registry, executor, store = _setup([root], auto_complete=False)
        registry.needs_tick = True
        real_clear = registry.build_clear_notify_aio

        async def clear_then_die(bid):
            await real_clear(bid)  # the flag is now false
            raise RuntimeError("died with the wake-up already claimed")

        registry.build_clear_notify_aio = clear_then_die  # type: ignore[method-assign]

        spawned, spawn = self._spawner()
        with pytest.raises(RuntimeError, match="already claimed"):
            await run_tick_aio(
                uuid4(),
                registry=registry,
                task_executor=executor,
                task_store=store,
                config=dataclasses.replace(FAST_TICK, spawn_tick=spawn),
            )

        assert registry.needs_tick is False
        assert spawned == []

    async def test_holding_the_lease_without_a_spawner_warns_once(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget], caplog
    ):
        """Nothing connects the worker's skip to the holder's ability to
        hand off: the worker decides on what the *registry* says, while the
        hand-off belongs to whoever holds the lease. Driving ``run_tick_aio``
        by hand with a default config is where the pairing comes apart, so
        it says so — once per process, since it is a property of how the
        process was configured."""
        (root,) = _chain("no-spawner-warning-root")
        registry, executor, store = _setup([root], auto_complete=True)

        tick_module._warned_missing_successor_spawner = False
        with caplog.at_level(logging.WARNING, logger=tick_module.__name__):
            await run_tick_aio(
                uuid4(),
                registry=registry,
                task_executor=executor,
                task_store=store,
                config=FAST_TICK,
            )
            first = caplog.text.count("cannot hand off on the way out")

            await run_tick_aio(
                uuid4(),
                registry=registry,
                task_executor=executor,
                task_store=store,
                config=FAST_TICK,
            )
            second = caplog.text.count("cannot hand off on the way out")

        assert first == 1
        assert second == 1, "warned more than once per process"

    async def test_a_configured_spawner_warns_about_nothing(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget], caplog
    ):
        (root,) = _chain("spawner-no-warning-root")
        registry, executor, store = _setup([root], auto_complete=True)
        _, spawn = self._spawner()

        tick_module._warned_missing_successor_spawner = False
        with caplog.at_level(logging.WARNING, logger=tick_module.__name__):
            await run_tick_aio(
                uuid4(),
                registry=registry,
                task_executor=executor,
                task_store=store,
                config=dataclasses.replace(FAST_TICK, spawn_tick=spawn),
            )

        assert "cannot hand off on the way out" not in caplog.text

    async def test_hand_off_failure_does_not_mask_the_tick(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """Best-effort means best-effort: the hand-off runs while an
        exception is unwinding, so anything it raises would replace the
        error the caller is about to see."""
        (root,) = _chain("handoff-boom-root")
        registry, executor, store = _setup([root], auto_complete=False)

        real_clear = registry.build_clear_notify_aio

        async def clear_then_die(bid):
            await real_clear(bid)
            registry.needs_tick = True
            raise RuntimeError("the real failure")

        registry.build_clear_notify_aio = clear_then_die  # type: ignore[method-assign]

        def explode(_: UUID, __: str) -> None:
            raise ConnectionError("modal is down")

        with pytest.raises(RuntimeError, match="the real failure"):
            await run_tick_aio(
                uuid4(),
                registry=registry,
                task_executor=executor,
                task_store=store,
                config=dataclasses.replace(FAST_TICK, spawn_tick=explode),
            )

    async def test_without_a_spawner_nothing_is_read_or_spawned(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """``spawn_tick=None`` (the default, and every caller
        whose wake-ups spawn unconditionally) must not pay a frontier fetch
        to discover it has nowhere to hand off to."""
        (root,) = _chain("no-spawner-root")
        registry, executor, store = _setup([root], auto_complete=False)
        registry.add_task(
            str(root.id),
            status="running",
            executor="other-backend",
            executor_ref="job-1",
        )
        trace: list[str] = []
        real_frontier = registry.build_get_frontier_aio

        async def traced_frontier(bid):
            trace.append("frontier")
            return await real_frontier(bid)

        registry.build_get_frontier_aio = traced_frontier  # type: ignore[method-assign]

        def on_release() -> None:
            trace.append("release")
            registry.needs_tick = True

        registry.lease_on_release = on_release

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=FAST_TICK,
        )

        assert summary.outcome == "lingered_out"
        assert summary.successor_spawned == 0
        assert trace[-1] == "release", (
            "read the frontier after releasing the lease with no successor "
            f"spawner configured: {trace}"
        )

    async def test_zero_linger_skips_the_pre_release_recheck(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """``linger_seconds=0`` is the watchdog sweep: one pass per build,
        many builds in one container. Extending a zero-length linger re-arms
        an already-expired deadline, so a steadily-notified build would spin
        without ever sleeping. The hand-off covers it instead."""
        (root,) = _chain("sweep-root")
        registry, executor, store = _setup([root], auto_complete=False)
        registry.add_task(
            str(root.id),
            status="running",
            executor="other-backend",
            executor_ref="job-1",
        )
        registry.needs_tick = True  # set again the instant the pass clears it
        real_clear = registry.build_clear_notify_aio

        async def clear_then_notify(bid):
            await real_clear(bid)
            registry.needs_tick = True

        registry.build_clear_notify_aio = clear_then_notify  # type: ignore[method-assign]

        spawned, spawn = self._spawner()
        build_id = uuid4()
        summary = await run_tick_aio(
            build_id,
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=TickConfig(
                linger_seconds=0,
                poll_interval_seconds=0.01,
                spawn_tick=spawn,
            ),
        )

        assert summary.outcome == "lingered_out"
        assert summary.iterations == 1, "the sweep's one-pass promise"
        assert summary.linger_extended == 0
        assert spawned == [build_id]
