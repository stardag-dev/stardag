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
    BuildTaskStore,
    DetachedExecutionStatus,
    DetachedHandle,
    FailMode,
    TickConfig,
    run_tick_aio,
)
from stardag.build._reactive import _frontier_actions as frontier_module
from stardag.target import InMemoryFileTarget
from stardag.utils.testing.helper_tasks import SyncOnlyTask

from tests.test_build.reactive_fakes import (
    FAST_TICK,
    FakeReactiveRegistry,
    FakeTickExecutor,
    InMemoryTaskStore,
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
        registry, executor, store = _setup(
            [root], auto_complete=False, executor=executor
        )
        registry.add_task(
            str(root.id), status="running", executor="fake", executor_ref="fc-live"
        )

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
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
        registry, executor, store = _setup([root], auto_complete=False)
        registry.add_task(
            str(root.id), status="running", executor="fake", executor_ref="fc-gone"
        )

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
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
        registry, _, store = _setup([root], auto_complete=False, executor=executor)
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
            task_store=store,
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
        registry, executor, store = _setup([root], auto_complete=False)
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
            task_store=store,
            config=FAST_TICK,
        )

        assert summary.outcome == "lingered_out"
        assert executor.spawned == []


class TestBuildTaskStoreRoundTrip:
    def test_pickle_round_trip(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        # The store is pickle-only; the reactive marker/config live in the
        # registry, not here.
        build_id = uuid4()
        store = BuildTaskStore(build_id)

        task = SyncOnlyTask(name="store-roundtrip")
        store.save_tasks([task])

        loaded = store.load_task(task.id)
        assert loaded is not None
        assert loaded.id == task.id
        assert isinstance(loaded, SyncOnlyTask)
        assert loaded.name == "store-roundtrip"

    def test_load_missing_task_returns_none(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        store = BuildTaskStore(uuid4())
        assert store.load_task(uuid4()) is None


class TestMissingTaskStoreEntry:
    async def test_missing_pickle_fails_task_instead_of_stalling(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """A pending actionable task whose object is missing from the store
        can never be scheduled — the tick fails it (and thereby the build)
        rather than leaving it in the frontier forever, where endless
        watchdog ticks would do nothing."""
        (root,) = _chain("missing-pickle-root")
        registry, executor, store = _setup([root], auto_complete=False)
        store._tasks.clear()  # simulate a lost/never-written pickle

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
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
        registry, executor, store = _setup([a, b, root], auto_complete=False)
        registry.limits["one-slot"] = 1

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
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
        registry, executor, store = _setup([a, b, root])
        registry.limits["one-slot"] = 1

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
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
        registry, executor, store = _setup([root])

        await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
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
        registry, executor, store = _setup([root], auto_complete=False)
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
            task_store=store,
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
        registry, _, store = _setup([root])
        executor = FakeTickExecutor(timeout_seconds=3600.0)

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
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
        registry, executor, store = _setup([root])

        await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,  # no timeout
            task_store=store,
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


class TestRehydrationFallback:
    async def test_store_miss_rehydrates_from_registry(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """A task missing from the pickle store is reconstructed from the
        registry's stored task_data and scheduled — instead of being failed."""
        import stardag as sd

        @sd.task(name="RehydrateFallbackTask")
        def fallback_task(limit: int) -> list[int]:
            return list(range(limit))

        root = fallback_task(limit=3)
        registry = FakeReactiveRegistry(
            root_task_ids=[str(root.id)], auto_complete=True
        )
        registry.add_task(str(root.id))
        registry.metadata_bodies[str(root.id)] = root.model_dump(mode="json")
        store = InMemoryTaskStore(uuid4())  # empty: no pickle for the task

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=(executor := FakeTickExecutor()),
            task_store=store,
            config=FAST_TICK,
        )

        assert executor.spawned == [root.id]
        assert summary.terminal_status == "completed"
        assert summary.failed_recorded == 0
        # Healed back into the store for subsequent ticks.
        assert store.load_task(root.id) is not None

    async def test_store_miss_and_no_metadata_still_fails_task(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """Without rehydratable data either, the stall-prevention failure
        path is preserved."""
        (root,) = _chain("no-rehydrate-root")
        registry, executor, store = _setup([root], auto_complete=False)
        store._tasks.clear()
        # no metadata_bodies entry -> fallback raises -> task failed

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=FAST_TICK,
        )

        assert summary.failed_recorded == 1
        assert summary.terminal_status == "failed"


class TestRehydrationOnAPickleFreeBuild:
    """``require_pickle_free`` binds the tick too, not just the trigger.

    The trigger-time gate (``PickleElisionPlan.require_pickle_free_error``)
    only ever inspected the DAG before the build started. The tick is a
    *writer* of the same store — a rehydrated task was written straight back
    — so on a writable target root a build that declared it writes no
    pickles quietly accumulated them anyway, one per task, the first time
    each was rehydrated.
    """

    @staticmethod
    def _decorator_built_dag():
        """A DAG whose class is rehydratable but NOT picklable by reference.

        ``@sd.task`` generates a class whose name differs from the module
        attribute holding it, so ``pickle.dumps`` fails on it — which is what
        made the write-back audible (a warning every tick) rather than merely
        wrong. Registry-data rehydration is unaffected: it is a lookup in the
        polymorphic registry, not a pickle.
        """
        import stardag as sd

        @sd.task(name="PickleFreeRehydrateTask")
        def pickle_free_task(limit: int) -> list[int]:
            return list(range(limit))

        return pickle_free_task(limit=3)

    @staticmethod
    def _registry_for(root):
        registry = FakeReactiveRegistry(
            root_task_ids=[str(root.id)], auto_complete=True
        )
        registry.add_task(str(root.id))
        registry.metadata_bodies[str(root.id)] = root.model_dump(mode="json")
        return registry

    async def test_the_rehydrated_task_is_not_written_back(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        root = self._decorator_built_dag()
        registry = self._registry_for(root)
        store = InMemoryTaskStore(uuid4(), pickle_free=True)

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=(executor := FakeTickExecutor()),
            task_store=store,
            config=FAST_TICK,
        )

        # Rehydration still works and the task is still scheduled — the
        # write-back was only ever a cache over an object already in hand.
        assert executor.spawned == [root.id]
        assert summary.terminal_status == "completed"
        # ...and the build kept the property it declared.
        assert store.load_task(root.id) is None

    async def test_no_warning_is_logged(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
        caplog: pytest.LogCaptureFixture,
    ):
        """The visible half of the bug: a failed write-back warned per tick.

        On a build of unpicklable-by-reference classes the write could not
        succeed, so the cost was log noise — which matters most exactly where
        this configuration is used, in CI, where it competed with real signal.
        """
        root = self._decorator_built_dag()
        registry = self._registry_for(root)
        store = InMemoryTaskStore(uuid4(), pickle_free=True)

        with caplog.at_level(logging.DEBUG):
            await run_tick_aio(
                uuid4(),
                registry=registry,
                task_executor=FakeTickExecutor(),
                task_store=store,
                config=FAST_TICK,
            )

        loader = "stardag.build._reactive._frontier_actions"
        records = [r for r in caplog.records if r.name == loader]
        assert [r for r in records if r.levelno >= logging.WARNING] == []
        # Rehydration is the designed path here, not an exception to report:
        # it happens for every task on every tick, so it is DEBUG, not INFO.
        rehydrated = [r for r in records if "Rehydrated task" in r.getMessage()]
        assert rehydrated and all(r.levelno == logging.DEBUG for r in rehydrated)

    async def test_an_ordinary_build_still_caches_the_rehydration(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The skip is opt-in. A build that did not declare pickle-freedom
        still heals its store, which is the whole point of the write-back:
        there, a miss means a pickle was expected and was not there."""
        root = self._decorator_built_dag()
        registry = self._registry_for(root)
        store = InMemoryTaskStore(uuid4())

        await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=FakeTickExecutor(),
            task_store=store,
            config=FAST_TICK,
        )

        assert store.load_task(root.id) is not None


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
        registry, executor, store = _setup([root], auto_complete=False)
        store._tasks.clear()  # neither a pickle nor rehydratable metadata

        with caplog.at_level("WARNING"):
            await run_tick_aio(
                uuid4(),
                registry=registry,
                task_executor=executor,
                task_store=store,
                config=FAST_TICK,
            )

        messages = "\n".join(record.getMessage() for record in caplog.records)
        assert "could not be rehydrated from registry data" in messages
        assert "stardag_no_such_declared_task_module" in messages
        assert "likely cause" in messages

    async def test_no_note_when_every_task_module_imported(
        self, caplog, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        from stardag.build._task_modules import _reset_import_state_for_tests

        _reset_import_state_for_tests()
        (root,) = _chain("diagnostic-clean-root")
        registry, executor, store = _setup([root], auto_complete=False)
        store._tasks.clear()

        with caplog.at_level("WARNING"):
            await run_tick_aio(
                uuid4(),
                registry=registry,
                task_executor=executor,
                task_store=store,
                config=FAST_TICK,
            )

        messages = "\n".join(record.getMessage() for record in caplog.records)
        assert "could not be rehydrated from registry data" in messages
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
        registry, _, store = _setup([root])

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=self.MetadataTickExecutor(),
            task_store=store,
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
        store = InMemoryTaskStore(uuid4())
        store.save_tasks([root])
        config = TickConfig(
            linger_seconds=0.3,
            poll_interval_seconds=0.01,
            limit_key_selector=lambda t: ["gpu"],
        )

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=self.PreSpawnMetadataExecutor(),
            task_store=store,
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
        store = InMemoryTaskStore(uuid4())
        store.save_tasks([root])
        executor = FakeTickExecutor()

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
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
        registry.limits["one-slot"] = 1
        store = InMemoryTaskStore(uuid4())
        store.save_tasks([a, b, root])
        executor = FakeTickExecutor()

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
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
        registry, _, store = _setup([*leaves, root], auto_complete=False)
        executor = InstrumentedTickExecutor(call_log=registry.calls)

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
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
        registry, _, store = _setup([*leaves, root], auto_complete=False)
        executor = InstrumentedTickExecutor(call_log=registry.calls)

        await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
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
        registry, _, store = _setup(
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
            store._tasks.pop(str(task.id), None)  # no pickle, no registry data

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
            config=TickConfig(
                linger_seconds=0.0,
                poll_interval_seconds=0.01,
                max_concurrent_actions=4,
                fail_mode=FailMode.CONTINUE,
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
        registry, _, store = _setup([*leaves, root], auto_complete=False)
        executor = InstrumentedTickExecutor(call_log=registry.calls)
        registry.limits["one-slot"] = 1

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
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
        registry, executor, store = _setup([*leaves, root], auto_complete=False)

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
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
        registry, executor, store = _setup([*leaves, root], auto_complete=False)

        summary = await run_tick_aio(
            uuid4(),
            registry=registry,
            task_executor=executor,
            task_store=store,
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
        registry, executor, store = _setup([*leaves, root], auto_complete=False)
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
            task_store=store,
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
        registry, executor, store = _setup([*leaves, root], auto_complete=False)

        with caplog.at_level(logging.INFO, logger="stardag.build._reactive"):
            await run_tick_aio(
                uuid4(),
                registry=registry,
                task_executor=executor,
                task_store=store,
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
