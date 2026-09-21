"""Acting on the frontier: spawns, claims, probes, the spawn cap and
task rehydration (stardag.build._reactive._frontier_actions)."""

from __future__ import annotations

import asyncio
import logging
import typing
from datetime import datetime, timedelta, timezone
from uuid import uuid4


import pytest

from stardag import (
    BaseTask,
    flatten_task_struct,
)
from stardag.build import (
    DetachedExecutionStatus,
    DetachedHandle,
    FailMode,
    TickConfig,
    run_tick_aio,
)
from stardag.build._reactive import _frontier_actions as frontier_module
from stardag.build._reactive._report_window import _ReportWindow
from stardag.build._reactive._tick import _EXIT_RESERVE_SECONDS
from stardag.target import InMemoryFileTarget
from stardag.utils.testing.helper_tasks import SyncOnlyTask

from tests.test_build.reactive_fakes import (
    FAST_TICK,
    FakeReactiveRegistry,
    FakeTickExecutor,
    registry_body,
    _chain,
    _setup,
)


class TestRunningTaskResolution:
    async def test_live_ref_left_alone(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        (root,) = _chain("live-root")
        executor = FakeTickExecutor(
            statuses={"fc-live": DetachedExecutionStatus.RUNNING}
        )
        registry, executor = _setup([root], auto_complete=False, executor=executor)
        registry.add_task(
            str(root.id), status="running", executor="fake", executor_ref="fc-live"
        )

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            config=FAST_TICK,
        )

        assert summary.outcome == "lingered_out"
        assert executor.spawned == []
        assert summary.self_healed == 0

    async def test_target_exists_self_heals_completion(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """Worker wrote the output then died before reporting: the tick
        emits the completion (target is ground truth) and the build
        finishes."""
        (root,) = _chain("heal-root")
        root.run()  # target now exists
        registry, executor = _setup([root], auto_complete=False)
        registry.add_task(
            str(root.id), status="running", executor="fake", executor_ref="fc-gone"
        )

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            config=FAST_TICK,
        )

        assert summary.outcome == "terminal"
        assert summary.terminal_status == "completed"
        assert summary.self_healed == 1
        assert ("complete", str(root.id)) in registry.calls
        assert executor.spawned == []

    async def test_failed_ref_records_failure_and_fails_build(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        (root,) = _chain("failed-ref-root")
        executor = FakeTickExecutor(
            statuses={"fc-dead": DetachedExecutionStatus.FAILED}
        )
        registry, _ = _setup([root], auto_complete=False, executor=executor)
        registry.add_task(
            str(root.id),
            status="running",
            executor="fake",
            executor_ref="fc-dead",
            # At the default 2-attempt budget: the failure is final, which
            # is what this test is about. Retry behaviour below budget has
            # its own tests (see TestAttemptBudget).
            attempt_count=2,
        )

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            config=FAST_TICK,
        )

        assert summary.failed_recorded == 1
        assert summary.outcome == "terminal"
        assert summary.terminal_status == "failed"
        assert registry.build_status == "failed"

    async def test_unknown_ref_left_alone(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """UNKNOWN probe status → conservatively leave (no duplicate spawn)."""
        (root,) = _chain("unknown-ref-root")
        registry, executor = _setup([root], auto_complete=False)
        registry.add_task(
            str(root.id),
            status="running",
            executor="other-backend",
            executor_ref="job-1",
        )

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            config=FAST_TICK,
        )

        assert summary.outcome == "lingered_out"
        assert executor.spawned == []


class TestWorkerReportWindow:
    """Who gets to say what ended an execution the probe found gone.

    The platform ending an input is one event with two meanings: a task
    that caught it and checkpointed is INTERRUPTED and is resumed on
    ``max_interruptions``; a task that did not is FAILED and spends an
    attempt. Only the dying worker knows which, and it reports from the
    platform's grace window — precisely the window a scheduler's probe can
    land inside.

    Before this, whoever looked first decided, and under load that was the
    tick: a checkpointing task had its resumption recorded as a failure,
    spent an attempt on it, and its own report was then refused as a
    statement about an execution it no longer held (STA-65). So the worker
    is the authority and the probe is the fallback, and these pin both
    halves — including that the fallback still fires when no report comes.
    """

    # Long enough that nothing in these tests reaches it by accident: a
    # window that closes on time is one test's subject, and everywhere
    # else its closing would be the race this whole class is about.
    LONG_GRACE = 30.0

    @staticmethod
    def _config(
        *,
        linger_seconds: float,
        grace: float,
        tick_timeout_seconds: float | None = None,
    ) -> TickConfig:
        return TickConfig(
            linger_seconds=linger_seconds,
            poll_interval_seconds=0.01,
            worker_report_grace_seconds=grace,
            tick_timeout_seconds=tick_timeout_seconds,
        )

    async def test_a_report_in_flight_is_not_pre_empted_by_the_probe(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The STA-65 race, with the worker reporting as the probe lands.

        The executor below reports the interruption from inside the probe
        call, which is the worst case rather than an unlikely one: a cancel
        ends the input the moment it is issued, so the backend can answer
        "gone" while the container is still running its ``except`` block.
        """
        (root,) = _chain("report-in-flight")

        class ReportingExecutor(FakeTickExecutor):
            probes = 0
            registry: typing.Any = None

            async def detached_status(self, task, executor, ref):
                ReportingExecutor.probes += 1
                if ReportingExecutor.probes == 1:
                    # The worker's report lands while the probe is in
                    # flight — it checkpointed and asked to be resumed.
                    await ReportingExecutor.registry.task_interrupt_aio(
                        uuid4(), task, "checkpointed", ref
                    )
                    ReportingExecutor.registry.needs_tick = True
                return DetachedExecutionStatus.FAILED

        executor = ReportingExecutor()
        registry, _ = _setup([root], auto_complete=True, executor=executor)
        ReportingExecutor.registry = registry
        registry.add_task(
            str(root.id),
            status="running",
            executor="fake",
            executor_ref="fc-cancelled",
        )

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            config=self._config(linger_seconds=1.0, grace=self.LONG_GRACE),
        )

        # The probe held its verdict...
        assert summary.executions_awaiting_report == 1
        assert summary.report_window_expired == 0
        # ...so nothing invented a failure, and no attempt was spent on an
        # interruption the task asked to survive.
        assert summary.failed_recorded == 0
        assert summary.retried == 0
        assert ("fail", str(root.id)) not in registry.calls
        # ...and the task was resumed, which is the accounting the build
        # should show for a checkpointed task the platform killed.
        assert summary.interruptions_restarted == 1
        assert executor.spawned == [root.id]
        assert summary.terminal_status == "completed"

    async def test_no_report_within_the_window_records_the_failure(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The other half: the window is a wait, not an amnesty.

        A worker that died without a word — OOM, a lost container — reports
        nothing ever, and the probe is the only thing that will notice. The
        failure it records is the same one as before, just later, and the
        reason string says so rather than leaving a reader to wonder why
        the tick waited.
        """
        (root,) = _chain("no-report")
        executor = FakeTickExecutor(statuses={"fc-oom": DetachedExecutionStatus.FAILED})
        registry, _ = _setup([root], auto_complete=True, executor=executor)
        registry.add_task(
            str(root.id),
            status="running",
            executor="fake",
            executor_ref="fc-oom",
            attempt_count=1,
        )

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            config=self._config(linger_seconds=1.0, grace=0.1),
        )

        assert summary.executions_awaiting_report == 1
        assert summary.report_window_expired == 1
        assert summary.failed_recorded == 1
        # ...and the ordinary retry path took over from there, unchanged.
        assert summary.retried == 1
        assert executor.spawned == [root.id]
        assert summary.terminal_status == "completed"
        (reason,) = registry.fail_reasons[str(root.id)]
        assert reason is not None and "no report" in reason

    async def test_the_verdict_lands_on_the_grace_not_on_the_linger(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """A closed window is acted on at once, not at the next deadline.

        The deadline a tick sleeps to is the *later* of its linger and its
        open windows, so a tick that only consulted the window when that
        deadline expired would sit on a closed one for the difference —
        with the defaults, 30s of grace followed by 90s of nothing. The
        window is in memory, so checking it every poll costs nothing and
        makes the grace mean what it says.

        The linger here is fifty times the grace; a tick that waited for
        it would not finish inside this test's timeout.
        """
        (root,) = _chain("verdict-on-grace")
        executor = FakeTickExecutor(
            statuses={"fc-dead": DetachedExecutionStatus.FAILED}
        )
        registry, _ = _setup([root], auto_complete=False, executor=executor)
        registry.add_task(
            str(root.id),
            status="running",
            executor="fake",
            executor_ref="fc-dead",
            attempt_count=2,
        )

        summary = await asyncio.wait_for(
            run_tick_aio(
                uuid4(),
                registry=registry,
                task_executor=executor,
                config=self._config(linger_seconds=5.0, grace=0.1),
            ),
            timeout=3,
        )

        assert summary.report_window_expired == 1
        assert summary.failed_recorded == 1
        assert summary.terminal_status == "failed"

    async def test_the_tick_holds_past_its_linger_rather_than_owe_a_verdict(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """A window outliving the linger deadline must not end the tick.

        Exiting there would leave the task RUNNING behind a claim nobody
        releases until it lapses — the stall the probe exists to prevent,
        reintroduced by the wait meant to make the probe polite. The linger
        deadline therefore moves out to cover the window.

        The linger below is far shorter than the grace, so a tick that did
        not hold on would record nothing at all.
        """
        (root,) = _chain("held-past-linger")
        executor = FakeTickExecutor(
            statuses={"fc-dead": DetachedExecutionStatus.FAILED}
        )
        registry, _ = _setup([root], auto_complete=False, executor=executor)
        registry.add_task(
            str(root.id),
            status="running",
            executor="fake",
            executor_ref="fc-dead",
            attempt_count=2,  # budget spent: the failure is final
        )

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            config=self._config(linger_seconds=0.05, grace=0.4),
        )

        assert summary.report_window_expired == 1
        assert summary.failed_recorded == 1
        assert summary.terminal_status == "failed"
        assert registry.build_status == "failed"

    async def test_a_single_pass_tick_waits_too(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """``linger_seconds=0`` — the watchdog sweep — is no exception.

        The window lives in the tick's memory, so a tick that exits
        rather than waiting is a tick that classifies *synchronously*.
        That is the whole bug, and a sweep is not immune to it: the
        periodic pass can perfectly well land in the seconds between an
        execution ending and its worker reporting.

        What "one pass and out" costs here is bounded and conditional —
        the sweep waits for a verdict it owes and for nothing else, so a
        build with nothing to decide still exits immediately (below).
        """
        (root,) = _chain("sweep-waits")
        executor = FakeTickExecutor(
            statuses={"fc-dead": DetachedExecutionStatus.FAILED}
        )
        registry, _ = _setup([root], auto_complete=False, executor=executor)
        registry.add_task(
            str(root.id),
            status="running",
            executor="fake",
            executor_ref="fc-dead",
            attempt_count=2,
        )

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            config=self._config(linger_seconds=0, grace=0.1),
        )

        assert summary.executions_awaiting_report == 1
        # ...and having waited, it still records the verdict rather than
        # leaving the build to the next sweep five minutes later.
        assert summary.report_window_expired == 1
        assert summary.failed_recorded == 1
        assert summary.terminal_status == "failed"

    async def test_a_single_pass_tick_with_nothing_owed_exits_at_once(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The other half: no verdict owed, no wait.

        A sweep over a build whose executions all probe live is out
        immediately, which is what ``linger_seconds=0`` is for.
        """
        (root,) = _chain("sweep-no-wait")
        executor = FakeTickExecutor(
            statuses={"fc-live": DetachedExecutionStatus.RUNNING}
        )
        registry, _ = _setup([root], auto_complete=False, executor=executor)
        registry.add_task(
            str(root.id),
            status="running",
            executor="fake",
            executor_ref="fc-live",
        )

        summary = await asyncio.wait_for(
            run_tick_aio(
                uuid4(),
                registry=registry,
                task_executor=executor,
                config=self._config(linger_seconds=0, grace=self.LONG_GRACE),
            ),
            timeout=5,
        )

        assert summary.executions_awaiting_report == 0
        assert summary.failed_recorded == 0
        assert summary.outcome == "lingered_out"

    async def test_a_lapsed_claim_still_gets_the_window(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """An expired claim is not a closed window, tempting as it reads.

        The claim expiry looks like an outer bound on the wait — past it
        nobody honours the claim, so why wait for its holder? Because the
        registry refuses a report about a *different execution*, not one
        from an expired claim: `TASK_INTERRUPTED` ends a claim, so
        applying it to a lapsed one releases something already released
        and is honoured. (Only `TASK_PREEMPTED`, which grants a fresh
        window, requires a live claim.)

        Skipping the wait here would therefore take a verdict the
        registry would have given the worker — and at the worst possible
        moment, since a claim expires around the timeout whose report
        this would be.
        """
        (root,) = _chain("lapsed-claim-dead-ref")
        executor = FakeTickExecutor(
            statuses={"fc-dead": DetachedExecutionStatus.FAILED}
        )
        registry, _ = _setup([root], auto_complete=False, executor=executor)
        registry.add_task(
            str(root.id),
            status="running",
            executor="fake",
            executor_ref="fc-dead",
            attempt_count=2,
            expires_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        )

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            config=self._config(linger_seconds=1.0, grace=0.1),
        )

        assert summary.executions_awaiting_report == 1
        # ...and the fallback still fires, so nothing is left hanging.
        assert summary.report_window_expired == 1
        assert summary.failed_recorded == 1
        assert summary.terminal_status == "failed"

    async def test_a_grace_too_large_for_the_container_is_trimmed_to_fit(
        self, caplog, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """A wait no tick can outlive is trimmed, not merely truncated.

        The distinction is whether the fallback ever runs. Truncating each
        wait would have every tick open a window, exit before it closed,
        and the next start from zero — a silently-dead execution deferred
        forever by ticks that each assume a later one will decide. Trimming
        keeps the promise that a window closes inside the tick that opened
        it.

        Here the configured grace is 30s and the container offers 0.3s
        beyond its exit reserve, so the verdict lands in 0.3s.
        """
        (root,) = _chain("grace-trimmed")
        executor = FakeTickExecutor(
            statuses={"fc-dead": DetachedExecutionStatus.FAILED}
        )
        registry, _ = _setup([root], auto_complete=False, executor=executor)
        registry.add_task(
            str(root.id),
            status="running",
            executor="fake",
            executor_ref="fc-dead",
            attempt_count=2,
        )

        with caplog.at_level("WARNING"):
            summary = await asyncio.wait_for(
                run_tick_aio(
                    uuid4(),
                    registry=registry,
                    task_executor=executor,
                    config=self._config(
                        linger_seconds=0.2,
                        grace=self.LONG_GRACE,
                        tick_timeout_seconds=_EXIT_RESERVE_SECONDS + 0.3,
                    ),
                ),
                # A tick that honoured the configured 30s would still be
                # waiting here — and, being killed at 10.3s, forever.
                timeout=10,
            )

        # It still waited...
        assert summary.executions_awaiting_report == 1
        # ...and still reached the verdict, which is the whole point.
        assert summary.report_window_expired == 1
        assert summary.failed_recorded == 1
        assert summary.terminal_status == "failed"
        assert any(
            "does not fit in this tick's own container timeout" in r.message
            for r in caplog.records
        ), "a trimmed grace should say so"

    async def test_a_container_too_small_to_wait_at_all_does_not(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """Trimmed to nothing is the same as switched off.

        A tick whose whole life is shorter than the reserve its exit needs
        has no wait to give, so it classifies immediately — the behaviour
        before any of this existed, which is the right floor to degrade
        to.
        """
        (root,) = _chain("container-too-small")
        executor = FakeTickExecutor(
            statuses={"fc-dead": DetachedExecutionStatus.FAILED}
        )
        registry, _ = _setup([root], auto_complete=False, executor=executor)
        registry.add_task(
            str(root.id),
            status="running",
            executor="fake",
            executor_ref="fc-dead",
            attempt_count=2,
        )

        summary = await asyncio.wait_for(
            run_tick_aio(
                uuid4(),
                registry=registry,
                task_executor=executor,
                config=self._config(
                    linger_seconds=0.2,
                    grace=self.LONG_GRACE,
                    tick_timeout_seconds=_EXIT_RESERVE_SECONDS / 2,
                ),
            ),
            timeout=10,
        )

        assert summary.executions_awaiting_report == 0
        assert summary.failed_recorded == 1
        assert summary.terminal_status == "failed"


class TestReportWindowBookkeeping:
    """``_ReportWindow`` on its own clock — the arithmetic the tick trusts."""

    @staticmethod
    def _window(grace: float = 10.0, **kwargs: typing.Any) -> tuple:
        now = [0.0]
        window = _ReportWindow(grace, clock=lambda: now[0], **kwargs)
        return window, now

    def test_the_first_sighting_opens_and_the_expiry_closes(self):
        window, now = self._window(grace=10.0)
        assert window.observe("t", "ref-1") == "opened"
        now[0] = 9.0
        assert window.observe("t", "ref-1") == "holding"
        assert window.seconds_until_due() == pytest.approx(1.0)
        assert not window.due()
        now[0] = 10.0
        assert window.due()
        assert window.observe("t", "ref-1") == "expired"
        # Closed, not re-armed: the verdict has been taken.
        assert window.seconds_until_due() is None
        assert not window.due()

    def test_a_new_execution_opens_a_new_window(self):
        """The window belongs to one execution, not to the task.

        A task whose retry also dies is a second event, and it gets its
        own worker and its own grace — inheriting the first window would
        give the second worker no time at all.
        """
        window, now = self._window(grace=10.0)
        assert window.observe("t", "ref-1") == "opened"
        now[0] = 50.0
        assert window.observe("t", "ref-2") == "opened"
        assert window.seconds_until_due() == pytest.approx(10.0)

    def test_a_task_that_resolved_itself_stops_holding_the_tick(self):
        """What ``retain`` is for: the window's own end condition.

        The worker reporting takes the task out of RUNNING, so the next
        pass does not probe it and never names it here. Left behind, its
        entry would hold the tick past its linger for a decision nobody is
        waiting on.
        """
        window, _ = self._window(grace=10.0)
        window.observe("kept", "ref-1")
        window.observe("gone", "ref-2")
        window.retain({"kept"})
        assert window.seconds_until_due() == pytest.approx(10.0)
        window.retain(set())
        assert window.seconds_until_due() is None

    def test_a_zero_grace_never_holds_anything(self):
        """The off switch, for a deployment whose workers never report."""
        zero, _ = self._window(grace=0.0)
        assert zero.observe("t", "ref-1") == "no_window"
        assert zero.seconds_until_due() is None
        assert not zero.due()


class TestUnreconstructableTask:
    async def test_a_task_with_no_registry_data_is_failed_not_stalled(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """A pending actionable task the tick cannot rebuild can never be
        scheduled — the tick fails it (and thereby the build) rather than
        leaving it in the frontier forever, where endless watchdog ticks
        would do nothing."""
        (root,) = _chain("unreconstructable-root")
        registry, executor = _setup([root], auto_complete=False)
        # No ``task_data``: the fake's stand-in for a class this process
        # cannot resolve, which is what the bootstrap pre-flight exists to
        # catch before a build ever reaches this state.
        registry.metadata_bodies.clear()

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            config=FAST_TICK,
        )

        assert summary.failed_recorded == 1
        assert summary.outcome == "terminal"
        assert summary.terminal_status == "failed"
        assert registry.build_status == "failed"
        assert executor.spawned == []


class TestConcurrencyLimits:
    async def test_denied_task_stays_in_frontier_no_false_deadlock(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """Two tasks under a 1-slot key: only one spawns per round; a denied
        task never triggers the stuck-build failure (the slot holder may
        even be in another build)."""
        a = SyncOnlyTask(name="lim-a")
        b = SyncOnlyTask(name="lim-b")
        root = SyncOnlyTask(name="lim-root", deps=(a, b))
        registry, executor = _setup([a, b, root], auto_complete=False)
        registry.limits["one-slot"] = 1

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            config=TickConfig(
                linger_seconds=0.2,
                poll_interval_seconds=0.01,
                limit_key_selector=lambda t: ["one-slot"]
                if t.id in (a.id, b.id)
                else [],
            ),
        )

        # One acquired + spawned, one denied; build keeps waiting (no
        # terminal failure) and the tick lingers out.
        assert summary.spawned == 1
        assert summary.limit_denied >= 1
        assert summary.outcome == "lingered_out"
        assert registry.build_status == "running"

    async def test_slot_release_lets_denied_task_proceed(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """With instant workers, the whole chain completes within one tick:
        each completion frees the slot and wakes the scheduler, which then
        acquires it for the next task."""
        a = SyncOnlyTask(name="rel-a")
        b = SyncOnlyTask(name="rel-b")
        root = SyncOnlyTask(name="rel-root", deps=(a, b))
        registry, executor = _setup([a, b, root])
        registry.limits["one-slot"] = 1

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            config=TickConfig(
                linger_seconds=0.5,
                poll_interval_seconds=0.01,
                limit_key_selector=lambda t: ["one-slot"],
            ),
        )

        assert summary.outcome == "terminal"
        assert summary.terminal_status == "completed"
        assert summary.spawned == 3
        assert registry.build_status == "completed"

    async def test_no_selector_claims_without_limit_keys(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """Without a selector the claiming start still happens (it is the
        exactly-once arbitration), but carries no limit keys — so nothing
        is enforced and no slot is held."""
        (root,) = _chain("nolim-root")
        registry, executor = _setup([root])

        await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            config=FAST_TICK,
        )

        assert registry.claim_limit_keys[str(root.id)] == []


class TestRunningWithoutRef:
    """The claiming start is recorded BEFORE the spawn, so a tick that dies
    in between leaves a task RUNNING with no ref: nothing to probe, no
    worker to report it, and its concurrency-limit slots held indefinitely.
    Whether that shape is dead or merely mid-spawn is decided by the
    claim's own expiry, not by how long it has sat there."""

    async def _tick_on_running_root(
        self, expires_at: "datetime | None", attempt_count: int = 2
    ):
        # Default: at the default 2-attempt budget, so a lapsed claim ends
        # as a plain failure. Pass a lower count to exercise the retry.
        (root,) = _chain(f"noref-root-{expires_at}-{attempt_count}")
        registry, executor = _setup([root], auto_complete=False)
        registry.add_task(
            str(root.id),
            status="running",
            status_at=datetime.now(timezone.utc) - timedelta(hours=1),
            expires_at=expires_at,
            attempt_count=attempt_count,
        )
        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            config=FAST_TICK,
        )
        return summary, executor, registry, root

    async def test_lapsed_claim_without_ref_is_failed(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """Lapsed claim: the server will hand the task to the next claimant
        anyway, so leaving it RUNNING only leaks the slots it holds."""
        summary, _, _, _ = await self._tick_on_running_root(
            datetime.now(timezone.utc) - timedelta(minutes=1)
        )

        assert summary.failed_recorded == 1
        assert summary.terminal_status == "failed"

    async def test_lapsed_claim_failure_is_retryable_and_counts_once(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """A claim lapses precisely when a worker vanished — the OOM /
        preemption case ``TickConfig.max_attempts`` exists for. The failure
        it records must be retryable like any other, and expiry and retry
        must not each charge an attempt."""
        summary, executor, registry, root = await self._tick_on_running_root(
            datetime.now(timezone.utc) - timedelta(minutes=1), attempt_count=1
        )

        assert summary.failed_recorded == 1
        assert summary.retried == 1
        assert summary.spawned == 1
        assert executor.spawned == [root.id]
        # One attempt closed by the expiry, one opened by the respawn — not
        # three. (The respawn's claim + ref starts collapse into one.)
        assert registry.attempt_count(str(root.id)) == 2

    async def test_live_claim_without_ref_is_left(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """A live claim is left alone however old the status is — the age
        was only ever a proxy for the question the expiry answers."""
        summary, executor, _, _ = await self._tick_on_running_root(
            datetime.now(timezone.utc) + timedelta(hours=1)
        )

        assert summary.failed_recorded == 0
        assert summary.outcome == "lingered_out"
        assert executor.spawned == []

    async def test_claim_without_an_expiry_is_left(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """No expiry (older server, or a start predating the column): the
        spawn-in-progress window of a healthy tick looks identical from
        here, so leave it rather than kill a task about to start."""
        summary, executor, _, _ = await self._tick_on_running_root(None)

        assert summary.failed_recorded == 0
        assert summary.outcome == "lingered_out"
        assert executor.spawned == []


class TestDerivedClaimTtl:
    """Every start the tick records carries a TTL derived from the
    executor's own timeout, so the expiry other schedulers read is tied to
    when the execution is actually killed."""

    async def test_derived_ttl_is_sent_on_both_starts(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        from stardag.build._reactive import _CLAIM_TTL_GRACE_SECONDS

        (root,) = _chain("ttl-root")
        registry, _ = _setup([root])
        executor = FakeTickExecutor(timeout_seconds=3600.0)

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            config=FAST_TICK,
        )

        expected = int(3600.0 + _CLAIM_TTL_GRACE_SECONDS)
        assert summary.spawned == 1
        # The claiming start and the post-spawn ref-recording start: the
        # second must carry it too, or it would hand the claim straight
        # back to the registry's generic default.
        assert registry.sent_claim_ttls[str(root.id)] == [expected, expected]

    async def test_no_executor_timeout_leaves_the_ttl_to_the_registry(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        (root,) = _chain("ttl-none-root")
        registry, executor = _setup([root])

        await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,  # no timeout
            config=FAST_TICK,
        )

        assert registry.sent_claim_ttls[str(root.id)] == [None, None]

    def test_ttl_is_clamped_to_the_servers_accepted_range(self):
        """A 10-second task and a 100-day task are both legitimate; each
        gets the closest expiry the server can express, not a 422."""
        from stardag.build._reactive import (
            _MAX_CLAIM_TTL_SECONDS,
            _MIN_CLAIM_TTL_SECONDS,
            claim_ttl_seconds,
        )

        (task,) = _chain("ttl-clamp")

        class _Timeout(FakeTickExecutor):
            def __init__(self, seconds):
                super().__init__(timeout_seconds=seconds)

        assert (
            claim_ttl_seconds(task, _Timeout(_MAX_CLAIM_TTL_SECONDS * 10))
            == _MAX_CLAIM_TTL_SECONDS
        )
        assert claim_ttl_seconds(task, _Timeout(None)) is None
        short = claim_ttl_seconds(task, _Timeout(1.0))
        assert short is not None and short >= _MIN_CLAIM_TTL_SECONDS

    def test_a_raising_executor_falls_back_to_the_registry_default(self):
        """Resolving a timeout is a diagnostic; it must never fail a start."""
        from stardag.build._reactive import claim_ttl_seconds

        (task,) = _chain("ttl-raises")

        class _Raising(FakeTickExecutor):
            def execution_timeout_seconds(self, task):
                raise RuntimeError("backend unreachable")

        assert claim_ttl_seconds(task, _Raising()) is None


async def _load_task_raises(registry, task_id: str) -> bool:
    """True if ``_load_task`` propagated rather than returning None."""
    from stardag.exceptions import NotFoundError

    try:
        await frontier_module._load_task(task_id, registry)
    except NotFoundError:
        return True
    return False


class TestRehydration:
    """A tick rebuilds every task it schedules from the registry's stored
    ``task_data``, and from nothing else."""

    @staticmethod
    def _decorator_built_dag():
        """A DAG whose class is rehydratable but NOT picklable by reference.

        ``@sd.task`` generates a class whose name differs from the module
        attribute holding it, so ``pickle.dumps`` fails on it. That used to
        be the shape that made the retired store's write-back audible; it
        is kept because it is also the sharpest proof that rehydration owes
        pickle nothing — it is a lookup in the polymorphic registry.
        """
        import stardag as sd

        @sd.task(name="RehydrateTask")
        def rehydrate_task(limit: int) -> list[int]:
            return list(range(limit))

        return rehydrate_task(limit=3)

    @staticmethod
    def _registry_for(root):
        registry = FakeReactiveRegistry(
            root_task_ids=[str(root.id)], auto_complete=True
        )
        registry.add_task(str(root.id))
        registry.metadata_bodies[str(root.id)] = registry_body(root)
        return registry

    async def test_a_task_is_rebuilt_from_registry_data_and_scheduled(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        root = self._decorator_built_dag()
        registry = self._registry_for(root)

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=(executor := FakeTickExecutor()),
            config=FAST_TICK,
        )

        assert executor.spawned == [root.id]
        assert summary.terminal_status == "completed"
        assert summary.failed_recorded == 0

    async def test_level_2_values_come_from_the_config_installed_here(self):
        """The property that replaced the store's rebind-on-load rule.

        ``task_data`` is the registry-mode dump — identity parameters only
        — so a rebuilt task reads its ``dependencies_only`` /
        ``execution_only`` fields from the build config installed in *this*
        process, under *this* code. That is what makes a build safe to
        re-plan under a new deployment, and it is exactly what a pickle
        could not do: a pickle restored the values the writing code
        resolved.
        """
        import stardag as sd
        from stardag.base_model import StardagField
        from stardag.build_config import build_config_scope

        class Configured(sd.Task[int]):
            __namespace__ = "frontier_tests"
            key: str
            width: typing.Annotated[
                int, StardagField(significance="dependencies_only")
            ] = 4

            def run(self) -> None:
                pass

        # Registered by a process that had width=9 configured.
        with build_config_scope({"frontier_tests.Configured": {"width": 9}}):
            registered = Configured(key="k")
        assert registered.width == 9
        registry = FakeReactiveRegistry(root_task_ids=[str(registered.id)])
        registry.metadata_bodies[str(registered.id)] = registry_body(registered)

        # Rebuilt with no config installed: this code's default applies,
        # and the id is unchanged, because width is not identity.
        loaded = await frontier_module._load_task(str(registered.id), registry)
        assert isinstance(loaded, Configured)
        assert loaded.id == registered.id
        assert loaded.width == 4

        # Rebuilt under a different config: that config's value applies.
        with build_config_scope({"frontier_tests.Configured": {"width": 7}}):
            loaded = await frontier_module._load_task(str(registered.id), registry)
        assert isinstance(loaded, Configured) and loaded.width == 7

    async def test_no_registry_data_fails_the_task(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """Without rehydratable data the stall-prevention failure path is
        preserved."""
        (root,) = _chain("no-rehydrate-root")
        registry, executor = _setup([root], auto_complete=False)
        registry.metadata_bodies.clear()

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            config=FAST_TICK,
        )

        assert summary.failed_recorded == 1
        assert summary.terminal_status == "failed"

    async def test_a_transient_registry_error_is_not_a_verdict_on_the_task(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """A 500 must not permanently fail a task — or, via fail_mode, a build.

        ``_load_task`` is the only way to get a task object now, so it sits
        on the critical path for every actionable task, and the caller marks
        what it reports as NON-retryable. Swallowing an outage there would
        turn a registry blip into a dead build. It must propagate instead:
        the tick ends as ``outcome="error"``, which is reported, diagnosable
        and retried by the next tick.
        """
        from stardag.exceptions import APIError

        (root,) = _chain("transient-error-root")
        registry, executor = _setup([root], auto_complete=False)

        async def boom(task_id):
            raise APIError("upstream timeout", status_code=500)

        registry.task_get_metadata_aio = boom  # type: ignore[method-assign]

        with pytest.raises(APIError):
            await run_tick_aio(
                uuid4(),
                registry=registry,
                task_executor=executor,
                config=FAST_TICK,
            )

        # The task was NOT failed, and no attempt was spent on it.
        assert registry.statuses[str(root.id)] == "pending"
        assert registry.build_status != "failed"

    async def test_a_missing_route_404_propagates_but_a_missing_task_does_not(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The two 404s mean opposite things.

        A resource 404 is a fact about this task and is deterministic; a
        route 404 means the server has no such endpoint, which is a fact
        about the deployment and says nothing about the task.
        """
        from stardag.exceptions import NotFoundError

        (root,) = _chain("route-404-root")
        registry, executor = _setup([root], auto_complete=False)

        async def missing_route(task_id):
            raise NotFoundError("Not Found", detail="Not Found")

        registry.task_get_metadata_aio = missing_route  # type: ignore[method-assign]
        assert await _load_task_raises(registry, str(root.id))

        # ...whereas a resource 404 resolves to None and fails the task.
        registry2, _ = _setup([root], auto_complete=False)
        registry2.metadata_bodies.clear()
        assert await frontier_module._load_task(str(root.id), registry2) is None

    async def test_a_successful_rebuild_logs_nothing_above_debug(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
        caplog: pytest.LogCaptureFixture,
    ):
        """Rehydration is the designed path, not an exception to report.

        It happens for every task on every tick, so reporting it at INFO
        would train readers to skim the scheduler's log lines — which
        matters most exactly where this runs most, in CI, where it would
        compete with real signal.
        """
        root = self._decorator_built_dag()
        registry = self._registry_for(root)

        with caplog.at_level(logging.DEBUG):
            await run_tick_aio(
                uuid4(),
                registry=registry,
                task_executor=FakeTickExecutor(),
                config=FAST_TICK,
            )

        loader = "stardag.build._reactive._frontier_actions"
        records = [r for r in caplog.records if r.name == loader]
        assert [r for r in records if r.levelno >= logging.WARNING] == []
        rebuilt = [r for r in records if "Rebuilt task" in r.getMessage()]
        assert rebuilt and all(r.levelno == logging.DEBUG for r in rebuilt)


class TestRehydrationDiagnostics:
    """A rehydration failure names the declared task modules that failed to
    import — "class X unresolved" and "the module defining X blew up on
    import" are the same incident seen from two ends, and only the
    annotation connects them."""

    @pytest.fixture
    def failed_task_module_import(self):
        from stardag.build._task_modules import (
            _reset_import_state_for_tests,
            import_task_modules,
        )

        _reset_import_state_for_tests()
        import_task_modules(["stardag_no_such_declared_task_module"])
        yield
        _reset_import_state_for_tests()

    async def test_failure_note_is_appended_to_the_rehydration_error(
        self,
        caplog,
        failed_task_module_import,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
    ):
        (root,) = _chain("diagnostic-root")
        registry, executor = _setup([root], auto_complete=False)
        registry.metadata_bodies.clear()  # nothing to rebuild the task from

        with caplog.at_level("WARNING"):
            await run_tick_aio(
                uuid4(),
                registry=registry,
                task_executor=executor,
                config=FAST_TICK,
            )

        messages = "\n".join(record.getMessage() for record in caplog.records)
        assert "could not be rebuilt from its registry data" in messages
        assert "stardag_no_such_declared_task_module" in messages
        assert "likely cause" in messages

    async def test_no_note_when_every_task_module_imported(
        self, caplog, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        from stardag.build._task_modules import _reset_import_state_for_tests

        _reset_import_state_for_tests()
        (root,) = _chain("diagnostic-clean-root")
        registry, executor = _setup([root], auto_complete=False)
        registry.metadata_bodies.clear()

        with caplog.at_level("WARNING"):
            await run_tick_aio(
                uuid4(),
                registry=registry,
                task_executor=executor,
                config=FAST_TICK,
            )

        messages = "\n".join(record.getMessage() for record in caplog.records)
        assert "could not be rebuilt from its registry data" in messages
        assert "failed to import" not in messages


class TestExecutorMetadataRecording:
    """The post-spawn ref-recording start carries the handle's
    executor_metadata (and drops it for pre-metadata registries)."""

    class MetadataTickExecutor(FakeTickExecutor):
        METADATA = {"kind": "modal", "app_name": "tick-app"}

        async def submit_detached(self, task: BaseTask) -> DetachedHandle:
            handle = await super().submit_detached(task)
            return DetachedHandle(
                executor=handle.executor,
                ref=handle.ref,
                wait=handle.wait,
                executor_metadata=self.METADATA,
            )

    async def test_post_spawn_start_carries_handle_metadata(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        (root,) = _chain("meta-root")
        registry, _ = _setup([root])

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=self.MetadataTickExecutor(),
            config=FAST_TICK,
        )

        assert summary.spawned == 1
        assert registry.start_metadata[str(root.id)] == (
            self.MetadataTickExecutor.METADATA
        )


class TestAcquiringStartExecutorMetadata:
    """The limits-acquiring TASK_STARTED (recorded BEFORE the spawn) carries
    the executor metadata resolvable pre-spawn, closing the acquire→spawn
    window where a RUNNING task would otherwise show blank executor info."""

    PRE_SPAWN_METADATA = {"kind": "modal", "app_name": "tick-app"}

    class PreSpawnMetadataExecutor(FakeTickExecutor):
        async def get_executor_metadata(self, task: BaseTask):
            return TestAcquiringStartExecutorMetadata.PRE_SPAWN_METADATA

    class AcquireRecordingRegistry(FakeReactiveRegistry):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.acquire_metadata: dict[str, dict | None] = {}

        async def _acquire_limits(
            self,
            build_id,
            task,
            executor=None,
            executor_ref=None,
            executor_metadata=None,
            limit_keys=None,
            claim_ttl_seconds=None,
        ):
            self.acquire_metadata[str(task.id)] = executor_metadata
            return await super()._acquire_limits(
                build_id,
                task,
                executor=executor,
                executor_ref=executor_ref,
                executor_metadata=executor_metadata,
                limit_keys=limit_keys,
                claim_ttl_seconds=claim_ttl_seconds,
            )

    async def test_acquiring_start_carries_metadata(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        (root,) = _chain("acquire-meta-root")
        registry = self.AcquireRecordingRegistry(
            root_task_ids=[str(root.id)], auto_complete=True
        )
        registry.add_task(str(root.id))
        registry.limits["gpu"] = 1
        registry.metadata_bodies[str(root.id)] = registry_body(root)
        config = TickConfig(
            linger_seconds=0.3,
            poll_interval_seconds=0.01,
            limit_key_selector=lambda t: ["gpu"],
        )

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=self.PreSpawnMetadataExecutor(),
            config=config,
        )

        assert summary.spawned == 1
        assert registry.acquire_metadata[str(root.id)] == self.PRE_SPAWN_METADATA


class ClaimingReactiveRegistry(FakeReactiveRegistry):
    """FakeReactiveRegistry with a scriptable cross-build claim race.

    ``claim_race_once`` simulates the race the claim closes: the frontier
    snapshot says PENDING, but by claim time another build's scheduler has
    already started (and instantly completed) the task.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.claim_race_once: set[str] = set()

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
    ):
        from stardag.registry import StartClaimResult

        tid = str(task.id)
        # Gated on ``claim`` like the server and the other doubles: an
        # unclaiming acquire cannot be denied ``already_running``, so a
        # double that raced it regardless could not emulate the limiter.
        if claim and tid in self.claim_race_once:
            # "Another build" won this task just before us and its instant
            # worker completed it (completion wakes our scheduler).
            self.claim_race_once.discard(tid)
            self.calls.append(("start_claim", tid))
            self.statuses[tid] = "completed"
            self.needs_tick = True
            return StartClaimResult(
                started=False,
                denied_reason="already_running",
                executor="fake",
                executor_ref="fc-other-build",
            )
        return await super().task_start_claim_aio(
            build_id,
            task,
            executor=executor,
            executor_ref=executor_ref,
            executor_metadata=executor_metadata,
            limit_keys=limit_keys,
            claim_ttl_seconds=claim_ttl_seconds,
            claim=claim,
        )


class TestTickClaims:
    async def test_claim_race_lost_then_build_completes(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The tick loses the claim to 'another build' — the task stays in
        the frontier, no duplicate spawn happens, no false stuck-failure,
        and the build completes once the winner's completion is observed."""
        (root,) = _chain("tick-claim-race")
        registry = ClaimingReactiveRegistry(
            root_task_ids=[str(root.id)], auto_complete=True
        )
        registry.add_task(str(root.id))
        registry.claim_race_once.add(str(root.id))
        registry.metadata_bodies[str(root.id)] = registry_body(root)
        executor = FakeTickExecutor()

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            config=FAST_TICK,
        )

        assert summary.outcome == "terminal"
        assert summary.terminal_status == "completed"
        assert summary.claim_denied == 1
        assert executor.spawned == []  # the duplicate spawn never happened
        assert registry.build_status == "completed"

    async def test_claims_and_limits_compose(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """With claims on, limit denials still resolve via the claim start
        (single acquiring call) and the chain completes under a 1-slot key."""
        a = SyncOnlyTask(name="tick-claim-a")
        b = SyncOnlyTask(name="tick-claim-b")
        root = SyncOnlyTask(name="tick-claim-root", deps=(a, b))
        registry = ClaimingReactiveRegistry(
            root_task_ids=[str(root.id)], auto_complete=True
        )
        for task in (a, b, root):
            registry.add_task(
                str(task.id),
                upstreams={str(d.id) for d in flatten_task_struct(task.requires())},
            )
            registry.metadata_bodies[str(task.id)] = registry_body(task)
        registry.limits["one-slot"] = 1
        executor = FakeTickExecutor()

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            config=TickConfig(
                linger_seconds=0.5,
                poll_interval_seconds=0.01,
                limit_key_selector=lambda t: ["one-slot"],
            ),
        )

        assert summary.outcome == "terminal"
        assert summary.terminal_status == "completed"
        assert summary.spawned == 3
        # all acquisitions went through the claiming start
        assert any(m == "start_claim" for (m, _) in registry.calls)


class InstrumentedTickExecutor(FakeTickExecutor):
    """FakeTickExecutor that records spawn concurrency and interleaving.

    ``submit_detached`` suspends (``asyncio.sleep(0)``) so several spawn
    coroutines can genuinely be in flight at once — without a suspension
    point the fakes complete synchronously and every "concurrent" pass
    would look serial no matter what the scheduler does.
    """

    def __init__(self, *, call_log: list[tuple[str, str | None]], **kwargs) -> None:
        super().__init__(**kwargs)
        self.call_log = call_log
        self.in_flight = 0
        self.max_in_flight = 0

    async def submit_detached(self, task: BaseTask) -> DetachedHandle:
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            # Two suspensions: one to let siblings pile up against the
            # semaphore, one to make sure the peak is observed while they
            # are all still inside this block.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            self.call_log.append(("spawn", str(task.id)))
            return await super().submit_detached(task)
        finally:
            self.in_flight -= 1


def _wide_layer(prefix: str, width: int) -> tuple[list[BaseTask], BaseTask]:
    """``width`` independent leaves plus a root depending on all of them."""
    leaves = [SyncOnlyTask(name=f"{prefix}-{index}") for index in range(width)]
    root = SyncOnlyTask(name=f"{prefix}-root", deps=tuple(leaves))
    return list(leaves), root


class TestFanOutConcurrency:
    async def test_wide_layer_spawns_concurrently_within_the_bound(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """A wide layer fans out concurrently — and never wider than
        ``max_concurrent_actions``.

        This is the test that pins the bound. Without the semaphore the
        peak would be the whole layer (200), which is exactly the
        unbounded fan-out that would just move the failure from the tick's
        clock to the registry's connection pool; without the TaskGroup it
        would be 1, which is the serial wall this change removes.
        """
        width, bound = 200, 5
        leaves, root = _wide_layer("fanout", width)
        registry, _ = _setup([*leaves, root], auto_complete=False)
        executor = InstrumentedTickExecutor(call_log=registry.calls)

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            config=TickConfig(
                linger_seconds=0.0,
                poll_interval_seconds=0.01,
                max_concurrent_actions=bound,
            ),
        )

        assert summary.spawned == width
        assert len(executor.spawned) == width
        assert set(executor.spawned) == {leaf.id for leaf in leaves}
        assert executor.max_in_flight <= bound
        assert executor.max_in_flight == bound  # the bound is saturated
        assert summary.outcome == "lingered_out"

    async def test_ordering_holds_per_task_under_concurrency(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """Concurrency reorders tasks against each other, never the three
        steps *within* one task: the acquiring start precedes the spawn (a
        denied task must never occupy a worker), and the ref-recording
        start follows it (no executor ref for an execution that does not
        exist yet)."""
        width = 40
        leaves, root = _wide_layer("order", width)
        registry, _ = _setup([*leaves, root], auto_complete=False)
        executor = InstrumentedTickExecutor(call_log=registry.calls)

        await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            config=TickConfig(
                linger_seconds=0.0,
                poll_interval_seconds=0.01,
                max_concurrent_actions=8,
            ),
        )

        calls = registry.calls
        # Interleaving across tasks is real (otherwise this asserts nothing).
        assert executor.max_in_flight > 1
        for leaf in leaves:
            tid = str(leaf.id)
            claim_at = calls.index(("start_claim", tid))
            spawn_at = calls.index(("spawn", tid))
            # The last start for this task is the post-spawn one carrying
            # the executor ref (the claim records one too, ref-less).
            ref_start_at = len(calls) - 1 - calls[::-1].index(("start", tid))
            assert claim_at < spawn_at < ref_start_at
        # And the ref actually landed, for every task.
        assert all(registry.refs[str(leaf.id)][1] is not None for leaf in leaves)

    async def test_counters_stay_accurate_under_concurrency(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """Three outcomes in one concurrent pass — spawned, self-healed and
        two flavours of recorded failure — all counted exactly once."""
        spawnable = [SyncOnlyTask(name=f"count-spawn-{i}") for i in range(12)]
        healed = [SyncOnlyTask(name=f"count-heal-{i}") for i in range(5)]
        dead = [SyncOnlyTask(name=f"count-dead-{i}") for i in range(4)]
        lost = [SyncOnlyTask(name=f"count-lost-{i}") for i in range(3)]
        root = SyncOnlyTask(
            name="count-root", deps=tuple([*spawnable, *healed, *dead, *lost])
        )
        registry, _ = _setup(
            [*spawnable, *healed, *dead, *lost, root], auto_complete=False
        )
        executor = InstrumentedTickExecutor(call_log=registry.calls)
        for index, task in enumerate(healed):
            task.run()  # target exists → self-heal on probe
            registry.add_task(
                str(task.id),
                status="running",
                executor="fake",
                executor_ref=f"heal-{index}",
            )
        for index, task in enumerate(dead):
            registry.add_task(
                str(task.id),
                status="running",
                executor="fake",
                executor_ref=f"dead-{index}",
                attempt_count=2,  # at budget: probed-dead stays failed
            )
            executor.probe_statuses[f"dead-{index}"] = DetachedExecutionStatus.FAILED
        for task in lost:
            # Nothing to rebuild the object from: the tick cannot probe it.
            registry.metadata_bodies.pop(str(task.id), None)

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            config=TickConfig(
                linger_seconds=0.0,
                poll_interval_seconds=0.01,
                max_concurrent_actions=4,
                fail_mode=FailMode.CONTINUE,
                # The four dead refs are here to be *counted*, not waited
                # for; the worker report window has its own tests, and
                # leaving it on would spend its whole grace before this
                # pass records anything.
                worker_report_grace_seconds=0,
            ),
        )

        assert summary.spawned == len(spawnable)
        assert summary.self_healed == len(healed)
        assert summary.failed_recorded == len(dead) + len(lost)
        assert sorted(executor.spawned) == sorted(task.id for task in spawnable)

    async def test_denied_task_never_reaches_a_worker(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """A one-slot limit against a concurrent fan-out: exactly one task
        acquires and spawns, and no denied task is ever submitted."""
        width = 10
        leaves, root = _wide_layer("denied", width)
        registry, _ = _setup([*leaves, root], auto_complete=False)
        executor = InstrumentedTickExecutor(call_log=registry.calls)
        registry.limits["one-slot"] = 1

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            config=TickConfig(
                linger_seconds=0.05,
                poll_interval_seconds=0.01,
                max_concurrent_actions=width,
                limit_key_selector=lambda t: ["one-slot"],
            ),
        )

        assert summary.spawned == 1
        # Cumulative across the tick's passes (the denied nine are re-tried
        # on every fresh frontier), so at least one full round of denials.
        assert summary.limit_denied >= width - 1
        assert summary.limit_denied % (width - 1) == 0
        assert len(executor.spawned) == 1
        # The denied ones were claimed-and-refused, never spawned.
        spawned_ids = set(executor.spawned)
        denied = [leaf for leaf in leaves if leaf.id not in spawned_ids]
        assert len(denied) == width - 1
        for leaf in denied:
            assert ("spawn", str(leaf.id)) not in registry.calls
        assert registry.build_status == "running"


class TestSpawnCap:
    async def test_cap_truncates_and_the_tick_re_acts_immediately(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """``linger_seconds=0`` is the probe: the linger loop returns on its
        first check, so the only way the remaining tasks get spawned in this
        same tick is the ``acted`` path re-evaluating on a fresh frontier.
        A cap that "just truncated" would leave 20 of the 30 unspawned."""
        width, cap = 30, 10
        leaves, root = _wide_layer("cap", width)
        registry, executor = _setup([*leaves, root], auto_complete=False)

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            config=TickConfig(
                linger_seconds=0.0,
                poll_interval_seconds=30.0,  # never reached: no lingering
                max_spawns_per_tick=cap,
            ),
        )

        assert summary.spawned == width
        assert len(executor.spawned) == width
        # Three acting passes of `cap` each, plus the pass that found
        # nothing left to do and let the tick linger out.
        assert summary.iterations == width // cap + 1
        assert summary.outcome == "lingered_out"

    async def test_uncapped_layer_is_one_pass(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """Control for the test above: the same layer under the default cap
        goes out in a single acting pass."""
        width = 30
        leaves, root = _wide_layer("uncapped", width)
        registry, executor = _setup([*leaves, root], auto_complete=False)

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            config=TickConfig(linger_seconds=0.0, poll_interval_seconds=30.0),
        )

        assert summary.spawned == width
        assert summary.iterations == 2

    async def test_ticks_timeout_bounds_a_real_pass(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """End to end: the tick's own timeout reaches the fan-out and
        truncates it, rather than only being readable in _spawn_cap."""
        width = 200
        leaves, root = _wide_layer("tick-timeout", width)
        registry, executor = _setup([*leaves, root], auto_complete=False)
        # A tiny container: min cap (50) per pass, so 200 leaves take four.
        config = TickConfig(
            linger_seconds=0.0,
            poll_interval_seconds=30.0,
            max_concurrent_actions=10,
            tick_timeout_seconds=1.0,
            # A backend that would have justified a far larger batch.
            report_tick_summaries=False,
        )
        assert (
            frontier_module._spawn_cap([], FakeTickExecutor(), config).limit
            == frontier_module._MIN_SPAWN_CAP
        )

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            config=config,
        )

        assert summary.spawned == width
        assert summary.iterations == width // frontier_module._MIN_SPAWN_CAP + 1

    async def test_the_cap_and_its_source_are_logged_once_per_tick(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
        caplog: pytest.LogCaptureFixture,
    ):
        """Three of the four rungs produce plausible-looking numbers from
        very different inputs, so a truncating tick is only diagnosable if
        the log says which one was read."""
        leaves, root = _wide_layer("cap-log", 3)
        registry, executor = _setup([*leaves, root], auto_complete=False)

        with caplog.at_level(logging.INFO, logger="stardag.build._reactive"):
            await run_tick_aio(
                uuid4(),
                registry=registry,
                task_executor=executor,
                config=TickConfig(
                    linger_seconds=0.0,
                    poll_interval_seconds=30.0,
                    tick_timeout_seconds=900.0,
                ),
            )

        announcements = [
            record.message
            for record in caplog.records
            if "will spawn at most" in record.message
        ]
        assert len(announcements) == 1  # once per tick, not once per pass
        assert "tick container's own timeout (900s)" in announcements[0]

    def test_cap_prefers_the_ticks_own_timeout_over_the_workers(self):
        """The rung that matters: a five-minute tick spawning hour-long
        workers must size its fan-out to the five minutes.

        The two inputs differ by two orders of magnitude here, and the
        worker-derived cap is the dangerous one — a tick that commits to a
        container's worth of work it cannot live long enough to finish is
        exactly the failure the cap exists to prevent. Asserting the cap
        *tracks the tick's* number (and not merely "is smaller") is what
        makes a regression to the proxy fail loudly."""
        tasks = [SyncOnlyTask(name="tick-vs-worker")]
        # A 24-hour worker under a 5-minute tick.
        executor = FakeTickExecutor(timeout_seconds=86_400.0)
        config = TickConfig(max_concurrent_actions=10, tick_timeout_seconds=300.0)

        cap = frontier_module._spawn_cap(tasks, executor, config)

        assert cap.limit == frontier_module._derived_spawn_cap(300.0, config)
        assert "tick container's own timeout" in cap.source
        # And it is emphatically not the worker-derived answer, which the
        # ceiling alone would not have saved us from.
        worker_derived = frontier_module._spawn_cap(
            tasks, executor, TickConfig(max_concurrent_actions=10)
        )
        assert worker_derived.limit == frontier_module._MAX_SPAWN_CAP
        assert cap.limit < worker_derived.limit

    def test_cap_is_derived_from_the_ticks_timeout(self):
        """No explicit cap → the cap is a duration budget: a fraction of the
        container's own wall clock, spread over the in-flight bound."""
        tasks = [SyncOnlyTask(name="derive")]
        config = TickConfig(max_concurrent_actions=10, tick_timeout_seconds=600.0)

        cap = frontier_module._spawn_cap(tasks, FakeTickExecutor(), config)

        assert cap.limit == int(
            frontier_module._SPAWN_BUDGET_FRACTION
            * 600.0
            * 10
            / frontier_module._SECONDS_PER_SPAWN
        )

    def test_executor_timeout_is_the_proxy_when_the_tick_has_none(self):
        """Rung 3: no tick timeout is known, so the executor's is read —
        and the source says so, because it is a proxy for a different
        quantity."""
        tasks = [SyncOnlyTask(name="proxy")]
        config = TickConfig(max_concurrent_actions=10)

        cap = frontier_module._spawn_cap(
            tasks, FakeTickExecutor(timeout_seconds=600.0), config
        )

        assert cap.limit == frontier_module._derived_spawn_cap(600.0, config)
        assert "as a proxy" in cap.source

    def test_cap_uses_the_tightest_timeout_across_candidates(self):
        """Heterogeneous routing: the smallest backend limit bounds the
        pass, so the proxy rung is derived from it."""

        class PerTaskTimeoutExecutor(FakeTickExecutor):
            def execution_timeout_seconds(self, task: BaseTask) -> float | None:
                return {"tight": 400.0}.get(typing.cast(typing.Any, task).name, 4000.0)

        tasks = [SyncOnlyTask(name="tight"), SyncOnlyTask(name="loose")]
        config = TickConfig(max_concurrent_actions=10)

        cap = frontier_module._spawn_cap(tasks, PerTaskTimeoutExecutor(), config)

        assert (
            cap.limit
            == frontier_module._spawn_cap(
                [SyncOnlyTask(name="tight")], PerTaskTimeoutExecutor(), config
            ).limit
        )
        assert (
            cap.limit
            < frontier_module._spawn_cap(
                [SyncOnlyTask(name="loose")], PerTaskTimeoutExecutor(), config
            ).limit
        )

    def test_cap_falls_back_when_no_timeout_is_known_anywhere(self):
        """Bottom rung: neither the tick nor the executor enforces a
        wall-clock limit — but the cap is still a cap, never "everything"."""
        tasks = [SyncOnlyTask(name="no-timeout")]

        cap = frontier_module._spawn_cap(
            tasks, FakeTickExecutor(timeout_seconds=None), TickConfig()
        )

        assert cap.limit == frontier_module._DEFAULT_MAX_SPAWNS_PER_TICK
        assert "no wall-clock limit is known" in cap.source

    def test_derived_cap_is_clamped(self):
        """Floor and ceiling, so neither a 30-second container nor a 30-day
        one produces a nonsense batch size."""
        tasks = [SyncOnlyTask(name="clamp")]

        assert (
            frontier_module._spawn_cap(
                tasks,
                FakeTickExecutor(),
                TickConfig(max_concurrent_actions=1, tick_timeout_seconds=1.0),
            ).limit
            == frontier_module._MIN_SPAWN_CAP
        )
        assert (
            frontier_module._spawn_cap(
                tasks,
                FakeTickExecutor(),
                TickConfig(max_concurrent_actions=50, tick_timeout_seconds=2_592_000.0),
            ).limit
            == frontier_module._MAX_SPAWN_CAP
        )

    def test_explicit_cap_wins(self):
        """Top rung: the override beats every derivation below it."""
        cap = frontier_module._spawn_cap(
            [SyncOnlyTask(name="explicit")],
            FakeTickExecutor(timeout_seconds=600.0),
            TickConfig(max_spawns_per_tick=7, tick_timeout_seconds=600.0),
        )

        assert cap.limit == 7
        assert "set explicitly" in cap.source
