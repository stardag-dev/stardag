"""The attempt and interruption budgets
(stardag.build._reactive._budgets)."""

from __future__ import annotations

import typing
from uuid import uuid4


from stardag import (
    BaseTask,
)
from stardag.build import (
    DetachedExecutionStatus,
    DetachedHandle,
    FailMode,
    TickConfig,
    discover_and_register_aio,
    run_tick_aio,
)
from stardag.build._reactive import (
    _RETRYABLE_STATUSES,
)
from stardag.target import InMemoryFileTarget
from stardag.utils.testing.helper_tasks import SyncOnlyTask

from tests.test_build.reactive_fakes import (
    FAST_TICK,
    FakeTickExecutor,
    _chain,
    _setup,
)


class TestRetryPath:
    async def test_retry_failed_discovery_resets_and_build_completes(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The reactive retry path: a task failed in a previous run is reset
        to pending by discovery (retry_failed=True) and the re-triggered
        build runs to completion instead of FAIL_FASTing on tick 1."""
        (root,) = _chain("retry-root")
        registry, executor, store = _setup([root])
        registry.add_task(str(root.id), status="failed")

        # What the reactive trigger does on (re-)trigger:
        result = await discover_and_register_aio(
            registry, uuid4(), root, retry_failed=True
        )
        assert [t.id for t in result.retried] == [root.id]
        assert registry.statuses[str(root.id)] == "pending"

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=FAST_TICK,
        )
        assert summary.terminal_status == "completed"
        assert registry.build_status == "completed"

    async def test_retry_failed_resets_an_abandoned_suspended_task(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """#208 A2: a task left SUSPENDED — its execution yielded dynamic
        dependencies and returned, then the build was abandoned — used to be
        permanently unschedulable, since the re-trigger's retry skipped it.
        It is now reset like any other non-completed status."""
        (root,) = _chain("suspended-retry-root")
        registry, executor, store = _setup([root])
        registry.add_task(str(root.id), status="suspended")

        result = await discover_and_register_aio(
            registry, uuid4(), root, retry_failed=True
        )

        assert [t.id for t in result.retried] == [root.id]
        assert registry.statuses[str(root.id)] == "pending"

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=FAST_TICK,
        )
        assert summary.terminal_status == "completed"

    async def test_worker_dynamic_dep_registration_never_resets_suspended(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The other caller of discover_and_register_aio is a worker
        registering its dynamically yielded deps — it takes the default
        retry_failed=False, so widening the retryable set cannot make a
        suspending worker reset its own task."""
        (root,) = _chain("suspended-worker-root")
        registry, _, _ = _setup([root], auto_complete=False)
        registry.add_task(str(root.id), status="suspended")

        result = await discover_and_register_aio(registry, uuid4(), root)

        assert result.retried == []
        assert registry.statuses[str(root.id)] == "suspended"
        assert not any(method == "retry" for (method, _) in registry.calls)

    async def test_without_retry_failed_build_fail_fasts(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """Control: without the retry, the failed status poisons the build
        (the pre-fix behavior the stack review flagged)."""
        (root,) = _chain("poison-root")
        registry, executor, store = _setup([root])
        registry.add_task(str(root.id), status="failed")

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=FAST_TICK,
        )
        assert summary.terminal_status == "failed"


class SpawnFailingExecutor(FakeTickExecutor):
    """Executor whose detached submit always raises.

    The failure this PR exists for: the backend refused the spawn, so no
    container ever ran and no function-level retry policy can apply.
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.spawn_attempts = 0

    async def submit_detached(self, task: BaseTask) -> DetachedHandle:
        self.spawn_attempts += 1
        raise RuntimeError("backend refused the spawn")


class TestAttemptBudget:
    """``TickConfig.max_attempts``: a per-build, per-task budget on starts.

    It exists because a backend's function-level retries only cover
    exceptions *inside* the container. Everything a tick observes — a spawn
    that never produced a container, an execution the backend killed, a
    claim that lapsed under a vanished worker — is outside that, and used
    to end a FAIL_FAST build on the first occurrence.
    """

    async def test_spawn_failure_under_budget_is_retried_then_exhausts(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The headline case, both halves: the first spawn failure is
        retried, the second exhausts the 2-attempt budget and fails the
        build — bounded, not a loop."""
        (root,) = _chain("budget-spawn-fail")
        executor = SpawnFailingExecutor()
        registry, _, store = _setup([root], auto_complete=False, executor=executor)

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=FAST_TICK,  # max_attempts=2, FAIL_FAST
        )

        assert executor.spawn_attempts == 2
        assert summary.retried == 1
        assert summary.retry_exhausted == 1
        assert summary.failed_recorded == 2
        assert summary.terminal_status == "failed"
        assert registry.build_status == "failed"
        # Each spawn's two starts (claim + ref-recording) collapse into one
        # attempt, so two attempts is what the budget counted.
        assert registry.attempt_count(str(root.id)) == 2

    async def test_probed_dead_execution_under_budget_is_respawned(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """An execution the backend reports FAILED is retried while the
        budget allows — and FAIL_FAST does not kill the build over the
        failure recorded on the way there."""
        (root,) = _chain("budget-probe-retry")
        executor = FakeTickExecutor(statuses={"fc-oom": DetachedExecutionStatus.FAILED})
        registry, _, store = _setup([root], auto_complete=False, executor=executor)
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
            task_store=store,
            config=FAST_TICK,
        )

        assert summary.retried == 1
        assert summary.failed_recorded == 1
        assert summary.spawned == 1
        assert executor.spawned == [root.id]
        # FAIL_FAST reads the pre-action frontier snapshot, so the failure
        # recorded and retried inside one pass never counts as a
        # build-killing failure.
        assert summary.terminal_status is None
        assert registry.build_status == "running"
        assert registry.statuses[str(root.id)] == "running"

    async def test_at_budget_the_tick_declines_and_says_why(
        self, caplog, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """Budget spent: no respawn, and a message naming the task, the
        count, the budget and the escape that actually works."""
        (root,) = _chain("budget-probe-exhausted")
        executor = FakeTickExecutor(
            statuses={"fc-dead": DetachedExecutionStatus.FAILED}
        )
        registry, _, store = _setup([root], auto_complete=False, executor=executor)
        registry.add_task(
            str(root.id),
            status="running",
            executor="fake",
            executor_ref="fc-dead",
            attempt_count=2,
        )

        with caplog.at_level("ERROR"):
            summary = await run_tick_aio(
                uuid4(),
                registry=registry,
                task_executor=executor,
                task_store=store,
                config=FAST_TICK,
            )

        assert summary.retried == 0
        assert summary.retry_exhausted == 1
        assert executor.spawned == []
        assert summary.terminal_status == "failed"
        assert ("retry", str(root.id)) not in registry.calls
        messages = "\n".join(record.getMessage() for record in caplog.records)
        assert str(root.id) in messages
        assert "will NOT be retried" in messages
        assert "2 of 2 allowed attempt(s) spent" in messages
        # The escape is the re-trigger, and the message must not leave the
        # reader thinking a bare retry would have done.
        assert "RE-TRIGGER THIS BUILD" in messages
        assert "starts a new round and resets every task's attempt count" in messages
        assert "Retrying the task on its own does NOT reset the count" in messages
        assert 'tick_kwargs={"max_attempts": 4}' in messages

    async def test_bare_retry_at_budget_is_refused_and_names_the_re_trigger(
        self, caplog, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The trap: a *bare* retry of a task already at budget.

        The retry succeeds server-side and the task returns to PENDING, but
        it records no BUILD_RESUMED, so the round the count is measured
        against is unchanged and the scheduler would never start it. The
        distinction from a re-trigger (which does reset) is the whole
        reason this message exists.
        """
        (root,) = _chain("budget-operator-retry")
        registry, executor, store = _setup([root], auto_complete=False)
        # Exactly what `stardag tasks retry` / the UI's Retry leaves behind:
        # pending again, with the attempts already spent.
        registry.add_task(str(root.id), status="pending", attempt_count=2)

        with caplog.at_level("ERROR"):
            summary = await run_tick_aio(
                uuid4(),
                registry=registry,
                task_executor=executor,
                task_store=store,
                config=FAST_TICK,
            )

        assert summary.budget_denied == 1
        assert summary.retried == 0
        assert executor.spawned == []
        assert ("start_claim", str(root.id)) not in registry.calls
        assert summary.terminal_status == "failed"
        messages = "\n".join(record.getMessage() for record in caplog.records)
        assert str(root.id) in messages
        assert "a BARE RETRY put it back" in messages
        assert "That retry SUCCEEDED" in messages
        assert "a bare retry does not start a new build round" in messages
        assert "What you wanted is a RE-TRIGGER of this build" in messages
        # The operator reads the task, not only the tick's logs.
        reason = registry.fail_reasons[str(root.id)][0]
        assert reason is not None
        assert "Attempt budget spent (2 of 2 allowed attempt(s)" in reason
        assert "A bare retry does not reset it" in reason
        assert "re-trigger this build" in reason

    async def test_a_re_trigger_starts_a_new_round_and_resets_the_budget(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The escape both exhaustion messages point at, end to end.

        A re-trigger of an existing build id records BUILD_RESUMED *before*
        its discovery retries the failed tasks, so the round boundary lands
        ahead of them and they arrive at zero — the same task the tick
        refused a moment ago is now an ordinary spawn candidate.
        """
        build_id = uuid4()
        (root,) = _chain("budget-round-reset")
        registry, executor, store = _setup([root], auto_complete=False)
        registry.add_task(str(root.id), status="pending", attempt_count=2)

        refused = await run_tick_aio(
            build_id,
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=FAST_TICK,
        )

        assert refused.budget_denied == 1
        assert executor.spawned == []
        assert registry.build_status == "failed"

        # Exactly what ``_trigger_reactive`` does, in exactly that order:
        # resume the build (the round boundary), then let discovery reset
        # the failed task to pending.
        await registry.build_resume_aio(build_id)
        await registry.task_retry_aio(build_id, root)
        assert registry.attempt_count(str(root.id)) == 0

        resumed = await run_tick_aio(
            build_id,
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=FAST_TICK,
        )

        assert resumed.budget_denied == 0
        assert resumed.spawned == 1
        assert executor.spawned == [root.id]
        # One attempt into the new round (the spawn's claim + ref starts
        # collapse), with the previous round's two no longer counted.
        assert registry.attempt_count(str(root.id)) == 1

    async def test_a_zero_attempt_count_never_denies_a_start(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """0 is "not attempted in this build", never "out of budget" — even
        with retries switched off entirely."""
        (root,) = _chain("budget-zero-count")
        registry, executor, store = _setup([root], auto_complete=False)
        registry.add_task(str(root.id), status="pending", attempt_count=0)

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=TickConfig(
                linger_seconds=0.0, poll_interval_seconds=0.01, max_attempts=1
            ),
        )

        assert summary.budget_denied == 0
        assert summary.spawned == 1
        assert executor.spawned == [root.id]

    async def test_suspended_resumption_is_never_budget_gated(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """Resuming a dynamic-dependency yield records a fresh start, so a
        suspend-heavy task is "over budget" while perfectly healthy. Gating
        it would cap dynamic dependencies, not retries."""
        (root,) = _chain("budget-suspended")
        registry, executor, store = _setup([root], auto_complete=False)
        registry.add_task(str(root.id), status="suspended", attempt_count=5)

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=TickConfig(
                linger_seconds=0.0, poll_interval_seconds=0.01, max_attempts=2
            ),
        )

        assert summary.budget_denied == 0
        assert summary.spawned == 1
        assert executor.spawned == [root.id]

    async def test_unrehydratable_task_never_spends_the_budget(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """A task whose object cannot be resolved fails deterministically:
        the second reading finds the same absence. Retrying it would burn
        the budget to arrive at the same failure, later."""
        (root,) = _chain("budget-no-object")
        registry, executor, store = _setup([root], auto_complete=False)
        store._tasks.pop(str(root.id), None)  # no pickle, no registry data

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=FAST_TICK,
        )

        assert summary.failed_recorded == 1
        assert summary.retried == 0
        assert summary.retry_exhausted == 0
        assert ("retry", str(root.id)) not in registry.calls
        assert registry.attempt_count(str(root.id)) == 0
        assert summary.terminal_status == "failed"

    async def test_max_attempts_one_records_the_failure_and_never_respawns(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The pre-``max_attempts`` behaviour, still available verbatim."""
        (root,) = _chain("budget-disabled")
        executor = SpawnFailingExecutor()
        registry, _, store = _setup([root], auto_complete=False, executor=executor)

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=TickConfig(
                linger_seconds=0.0, poll_interval_seconds=0.01, max_attempts=1
            ),
        )

        assert executor.spawn_attempts == 1
        assert summary.retried == 0
        # Not "exhausted": nothing was budgeted away, retries are off.
        assert summary.retry_exhausted == 0
        assert summary.terminal_status == "failed"

    async def test_a_registry_that_cannot_count_attempts_never_retries(
        self, caplog, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """A server predating ``attempt_count`` reports nothing, so no
        budget can bound a retry loop. Degrade to the old behaviour rather
        than to an unbounded one — and say so, because a configured retry
        policy silently doing nothing is its own trap."""
        (root,) = _chain("budget-old-server")
        executor = SpawnFailingExecutor()
        registry, _, store = _setup([root], auto_complete=False, executor=executor)
        registry.serves_attempt_counts = False

        with caplog.at_level("WARNING"):
            summary = await run_tick_aio(
                uuid4(),
                registry=registry,
                task_executor=executor,
                task_store=store,
                config=FAST_TICK,
            )

        assert executor.spawn_attempts == 1
        assert summary.retried == 0
        assert summary.retry_exhausted == 0
        assert summary.budget_denied == 0
        assert summary.terminal_status == "failed"
        messages = "\n".join(record.getMessage() for record in caplog.records)
        assert "does not report per-round attempt counts" in messages
        assert "Upgrade stardag-api" in messages

    async def test_summary_counters_are_reported_to_the_registry(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The budget counters ride the persisted TickSummary, so "why did
        this build fail on a transient error?" is answerable without logs."""
        (root,) = _chain("budget-summary")
        executor = SpawnFailingExecutor()
        registry, _, store = _setup([root], auto_complete=False, executor=executor)

        await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=FAST_TICK,
        )

        reported = registry.reported_tick_summaries[-1]
        assert reported["retried"] == 1
        assert reported["retry_exhausted"] == 1
        assert reported["budget_denied"] == 0


class TestInterruptedTasks:
    """What a tick does with a task the platform interrupted.

    An interruption is the execution backend taking a container away — a
    function timeout, a reclaimed instance — reported by the dying worker
    in its grace window. It is not a failure, so it must not fail a
    FAIL_FAST build; it is not running, so it holds no claim; and it is
    still the scheduler's to act on, which is what these tests pin.

    There is no policy to configure: the status is written only for a
    task that raised ``ResumableInterruption``, so reaching this code means
    the task asked. An interruption a task did not catch never gets here —
    the worker reports nothing, the execution dies, and a later pass
    records an ordinary retryable failure.
    """

    async def test_an_interrupted_task_is_resumed(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """No configuration involved: the status exists only because a
        worker asked to be resumed, so the tick resumes it."""
        (root,) = _chain("interrupted-default")
        registry, executor, store = _setup([root], auto_complete=True)
        registry.statuses[str(root.id)] = "interrupted"

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=FAST_TICK,
        )

        assert summary.interruptions_restarted == 1
        assert summary.interruptions_failed == 0
        assert summary.failed_recorded == 0
        assert ("fail", str(root.id)) not in registry.calls
        assert executor.spawned == [root.id]
        assert summary.terminal_status == "completed"

    async def test_a_resumption_request_respawns_without_a_failure(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """An INTERRUPTED task is one that asked to be resumed, so it goes
        straight back to the frontier with no failure in its history."""
        (root,) = _chain("interrupted-restart")
        registry, executor, store = _setup([root], auto_complete=True)
        registry.statuses[str(root.id)] = "interrupted"

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=TickConfig(
                linger_seconds=0.3,
                poll_interval_seconds=0.01,
            ),
        )

        assert summary.interruptions_restarted == 1
        assert summary.interruptions_failed == 0
        assert summary.failed_recorded == 0
        assert ("fail", str(root.id)) not in registry.calls
        assert executor.spawned == [root.id]
        assert summary.terminal_status == "completed"

    async def test_resumption_is_bounded_by_its_own_budget(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """Exempt from the attempt budget does not mean unbounded: a task
        that times out forever must stop, with a message naming the knob."""
        (root,) = _chain("interrupted-exhausted")
        registry, executor, store = _setup([root], auto_complete=True)
        registry.add_task(str(root.id), status="interrupted", interrupt_count=3)

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=TickConfig(
                linger_seconds=0.3,
                poll_interval_seconds=0.01,
                max_interruptions=3,
            ),
        )

        assert summary.interruptions_exhausted == 1
        assert summary.interruptions_restarted == 0
        assert executor.spawned == []
        reason = registry.fail_reasons[str(root.id)][-1] or ""
        assert "Interruption budget spent (3 of 3" in reason
        assert "max_interruptions" in reason

    async def test_interruptions_do_not_spend_the_attempt_budget(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The property the whole separate budget exists for. Two
        interruptions with ``max_attempts=2`` — a task charged for them
        would already be refused a start."""
        (root,) = _chain("interrupted-not-an-attempt")
        registry, executor, store = _setup([root], auto_complete=True)
        registry.add_task(str(root.id), status="interrupted", interrupt_count=2)
        assert registry.attempt_count(str(root.id)) == 1

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=TickConfig(
                linger_seconds=0.3,
                poll_interval_seconds=0.01,
                max_attempts=2,
                max_interruptions=10,
            ),
        )

        assert summary.interruptions_restarted == 1
        assert summary.budget_denied == 0
        assert executor.spawned == [root.id]

    async def test_a_live_ref_is_re_probed_until_it_is_not(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """An interrupted task whose execution still probes as live is left
        alone — the backend may be retrying the input under the same ref,
        and spawning would run it twice.

        **But "left alone" must not mean "abandoned".** Nothing will ever
        emit an event when that ref stops being live: the worker that would
        have reported is dead, and an interrupted task produces nothing
        further. A tick that lingered on the wake-up flag here would stall
        the build until the watchdog — which is off by default. So the pass
        re-probes instead, and picks the task up as soon as the ref
        resolves.

        The executor below answers RUNNING once and FAILED after, which is
        exactly the shape of the race this guards: the interruption is
        reported inside the grace window and wakes a tick immediately, so
        the probe can easily land before the call has finished unwinding.
        """
        (root,) = _chain("interrupted-backend-retry")

        class SettlingExecutor(FakeTickExecutor):
            probes = 0

            async def detached_status(self, task, executor, ref):
                SettlingExecutor.probes += 1
                if SettlingExecutor.probes == 1:
                    return DetachedExecutionStatus.RUNNING
                return DetachedExecutionStatus.FAILED

        registry, executor, store = _setup(
            [root], auto_complete=True, executor=SettlingExecutor()
        )
        registry.add_task(
            str(root.id),
            status="interrupted",
            executor="fake",
            executor_ref="fc-live",
        )

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=FAST_TICK,
        )

        # It waited on the first probe...
        assert summary.interruptions_backend_retrying >= 1
        assert SettlingExecutor.probes >= 2, "the tick stopped re-probing"
        # ...and resumed the task once the ref resolved, rather than
        # lingering out with the build stalled.
        assert summary.interruptions_restarted == 1
        assert executor.spawned == [root.id]
        assert summary.terminal_status == "completed"

    async def test_a_live_ref_is_not_spawned_in_the_same_pass(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The other half: while the ref stays live, nothing is spawned.
        A permanently-live ref lingers out rather than duplicating the
        execution."""
        (root,) = _chain("interrupted-still-live")
        registry, executor, store = _setup(
            [root],
            auto_complete=False,
            executor=FakeTickExecutor(
                statuses={"fc-live": DetachedExecutionStatus.RUNNING}
            ),
        )
        registry.add_task(
            str(root.id),
            status="interrupted",
            executor="fake",
            executor_ref="fc-live",
        )

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=TickConfig(linger_seconds=0.1, poll_interval_seconds=0.02),
        )

        assert summary.interruptions_backend_retrying >= 1
        assert summary.interruptions_restarted == 0
        assert executor.spawned == []
        assert registry.statuses[str(root.id)] == "interrupted"

    async def test_a_dead_ref_does_not_block_the_restart(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The control for the guard above: a ref that probes FAILED is a
        finished execution, so the task is the scheduler's to start."""
        (root,) = _chain("interrupted-dead-ref")
        registry, executor, store = _setup(
            [root],
            auto_complete=True,
            executor=FakeTickExecutor(
                statuses={"fc-dead": DetachedExecutionStatus.FAILED}
            ),
        )
        registry.add_task(
            str(root.id),
            status="interrupted",
            executor="fake",
            executor_ref="fc-dead",
        )

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=TickConfig(
                linger_seconds=0.3,
                poll_interval_seconds=0.01,
            ),
        )

        assert summary.interruptions_backend_retrying == 0
        assert summary.interruptions_restarted == 1
        assert executor.spawned == [root.id]

    async def test_unknown_probe_does_not_stall_the_task(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """UNKNOWN is deliberately NOT treated as "hands off", unlike the
        RUNNING-task path. An interrupted task holds no claim and is
        nobody else's to run, and an executor that does not recognise the
        recorded ref's backend answers UNKNOWN forever — so waiting on it
        would wedge the build rather than protect it."""
        (root,) = _chain("interrupted-unknown-ref")
        # FakeTickExecutor answers UNKNOWN for any ref it was not told about.
        registry, executor, store = _setup([root], auto_complete=True)
        registry.add_task(
            str(root.id),
            status="interrupted",
            executor="some-other-backend",
            executor_ref="ref-we-cannot-probe",
        )

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=TickConfig(
                linger_seconds=0.3,
                poll_interval_seconds=0.01,
            ),
        )

        assert summary.interruptions_backend_retrying == 0
        assert executor.spawned == [root.id]

    async def test_no_interrupt_counter_falls_back_to_the_attempt_budget(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """A server predating ``interrupt_count`` cannot bound a resume
        loop, so resumption degrades to the bounded thing rather than to an
        unbounded one."""
        (root,) = _chain("interrupted-no-counter")
        registry, executor, store = _setup([root], auto_complete=True)
        registry.statuses[str(root.id)] = "interrupted"
        registry.serves_interrupt_counts = False

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=TickConfig(
                linger_seconds=0.3,
                poll_interval_seconds=0.01,
            ),
        )

        assert summary.interruptions_restarted == 0
        assert summary.interruptions_failed == 1

    async def test_an_interruption_does_not_fail_a_fail_fast_build(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The headline property, stated directly. Under FAIL_FAST a single
        FAILED task kills the build on the next pass — which is exactly
        what must not happen when the platform, not the task, ended the
        run. The dep is interrupted and the build still completes."""
        dep, root = _chain("ff-dep", "ff-root")
        registry, executor, store = _setup([dep, root], auto_complete=True)
        registry.statuses[str(dep.id)] = "interrupted"

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=TickConfig(
                linger_seconds=0.3,
                poll_interval_seconds=0.01,
                fail_mode=FailMode.FAIL_FAST,
            ),
        )

        assert summary.terminal_status == "completed"
        assert registry.build_status == "completed"

    async def test_fail_fast_cancels_an_interrupted_task(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """An interrupted task may still have a live execution — that is
        the premise of the backend-retry guard — so a dying build must
        cancel it, and must not leave it INTERRUPTED.

        Left behind it is a permanent wedge for every other build gated on
        it: ``_OWNER_DRIVEN_STATUSES`` reads interrupted as "the owner will
        move it", so a neighbour waits and then fails, where a CANCELLED
        task is reset and run.
        """
        # Siblings, not a chain: a failed *upstream* would gate the
        # interrupted task out of `actionable` and the test would pass for
        # the wrong reason.
        broken = SyncOnlyTask(name="ff-cancel-broken", deps=())
        resuming = SyncOnlyTask(name="ff-cancel-resuming", deps=())
        root = SyncOnlyTask(name="ff-cancel-root", deps=(broken, resuming))
        registry, executor, store = _setup(
            [broken, resuming, root],
            auto_complete=False,
            executor=FakeTickExecutor(
                statuses={"fc-live": DetachedExecutionStatus.RUNNING}
            ),
        )
        registry.statuses[str(broken.id)] = "failed"
        registry.add_task(
            str(resuming.id),
            status="interrupted",
            executor="fake",
            executor_ref="fc-live",
        )

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=TickConfig(
                linger_seconds=0.05,
                poll_interval_seconds=0.02,
                fail_mode=FailMode.FAIL_FAST,
            ),
        )

        assert summary.terminal_status == "failed"
        assert "fc-live" in executor.cancelled_refs
        assert registry.statuses[str(resuming.id)] == "cancelled"

    async def test_fail_fast_does_not_cancel_what_this_pass_just_resumed(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """Terminal handling runs on the snapshot taken BEFORE the pass
        acted, so an interrupted task the pass resumed still reads
        ``interrupted`` there, under its old ref.

        Acting on that stale copy is worse than doing nothing: cancelling
        the dead ref is a no-op, while the TASK_CANCELLED it records
        releases the claim on the execution that just started — and a
        cancelled task with no claim is exactly what a neighbouring build
        treats as recoverable, so it resets it and spawns a second
        execution while the first is still writing the target. Hence the
        re-read in ``_cancel_running``.
        """
        broken = SyncOnlyTask(name="ff-fresh-broken", deps=())
        resuming = SyncOnlyTask(name="ff-fresh-resuming", deps=())
        root = SyncOnlyTask(name="ff-fresh-root", deps=(broken, resuming))
        registry, executor, store = _setup(
            [broken, resuming, root],
            auto_complete=False,
            # The interrupted task's OLD ref is dead, so the pass resumes it.
            executor=FakeTickExecutor(
                statuses={"fc-dead": DetachedExecutionStatus.FAILED}
            ),
        )
        registry.statuses[str(broken.id)] = "failed"
        registry.add_task(
            str(resuming.id),
            status="interrupted",
            executor="fake",
            executor_ref="fc-dead",
        )

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=TickConfig(
                linger_seconds=0.05,
                poll_interval_seconds=0.02,
                fail_mode=FailMode.FAIL_FAST,
            ),
        )

        assert summary.terminal_status == "failed"
        # It was resumed, so it is RUNNING under a NEW ref by the time the
        # build dies — and it is that ref which must be cancelled, never
        # the stale one the pre-action snapshot carried.
        assert executor.spawned == [resuming.id]
        assert "fc-dead" not in executor.cancelled_refs, (
            "cancelled the stale ref: the live execution is orphaned and "
            "its claim released"
        )
        assert executor.cancelled_refs == ["ref-1"]
        assert registry.statuses[str(resuming.id)] == "cancelled"

    async def test_interrupted_is_reset_by_a_re_trigger(self):
        """``_RETRYABLE_STATUSES`` covers it, so discovery's
        ``retry_failed`` picks up a task abandoned mid-interruption. Leave
        it out and that task is unschedulable forever."""
        assert "interrupted" in _RETRYABLE_STATUSES
