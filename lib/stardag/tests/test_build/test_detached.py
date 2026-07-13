"""Tests for detached task execution in the concurrent build engine.

Covers the executor-agnostic mechanics (`TaskExecutorABC.submit_detached` /
`reattach` + registry executor-ref bookkeeping in ``build_aio``) using a fake
detached executor. The Modal-specific implementation is tested in
``tests/test_integration/test_modal/test_detached_executor.py``.
"""

from __future__ import annotations

import typing
from uuid import UUID

import pytest

from stardag import BaseTask, TaskStruct, auto_namespace
from stardag.build import (
    BuildExitStatus,
    DetachedHandle,
    TaskExecutionError,
    TaskExecutorABC,
    build_aio,
)
from stardag.registry import RegisteredTaskInfo
from stardag.target import InMemoryFileTarget
from stardag.utils.testing.helper_tasks import SyncOnlyTask

from .conftest import RecordingRegistry

auto_namespace(__name__)

FAKE_EXECUTOR_NAME = "fake"


class FakeDetachedExecutor(TaskExecutorABC):
    """Executor with detached support that records every interaction.

    - ``submit_detached`` "spawns" by just deferring inline execution to the
      handle's ``wait()`` and returns ref ``spawned-<task.id>``.
    - ``reattach`` succeeds only for refs listed in ``live_refs`` (the fake's
      stand-in for "the remote execution is still running"); its ``wait()``
      also executes the task inline so the target actually gets written.
    """

    def __init__(
        self,
        *,
        detached: bool = True,
        live_refs: set[str] | None = None,
        spawn_error: Exception | None = None,
    ) -> None:
        self.detached = detached
        self.live_refs = live_refs or set()
        self.spawn_error = spawn_error
        self.submit_calls: list[UUID] = []
        self.spawn_calls: list[UUID] = []
        self.reattach_calls: list[tuple[UUID, str, str]] = []
        self.cancel_calls: list[UUID] = []

    async def _run_inline(self, task: BaseTask) -> None | TaskStruct:
        result = task.run()
        assert result is None, "FakeDetachedExecutor only supports simple tasks"
        return None

    async def submit(self, task: BaseTask) -> None | TaskStruct | TaskExecutionError:
        self.submit_calls.append(task.id)
        return await self._run_inline(task)

    def supports_detached(self, task: BaseTask) -> bool:
        return self.detached

    async def submit_detached(self, task: BaseTask) -> DetachedHandle:
        if self.spawn_error is not None:
            raise self.spawn_error
        self.spawn_calls.append(task.id)

        async def wait() -> None | TaskStruct | TaskExecutionError:
            return await self._run_inline(task)

        return DetachedHandle(
            executor=FAKE_EXECUTOR_NAME, ref=f"spawned-{task.id}", wait=wait
        )

    async def reattach(
        self, task: BaseTask, executor: str, ref: str
    ) -> DetachedHandle | None:
        self.reattach_calls.append((task.id, executor, ref))
        if executor != FAKE_EXECUTOR_NAME or ref not in self.live_refs:
            return None

        async def wait() -> None | TaskStruct | TaskExecutionError:
            return await self._run_inline(task)

        return DetachedHandle(executor=FAKE_EXECUTOR_NAME, ref=ref, wait=wait)

    async def cancel(self, task: BaseTask) -> None:
        self.cancel_calls.append(task.id)

    async def setup(self) -> None:
        pass

    async def teardown(self) -> None:
        pass


def _start_call_extras(registry: RecordingRegistry, task_id: UUID) -> dict:
    starts = [
        extra
        for (method, tid, extra) in registry.calls
        if method == "task_start_aio" and tid == task_id
    ]
    assert len(starts) == 1
    return starts[0]


class TestDetachedSpawn:
    async def test_spawn_records_executor_ref_on_start(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
        recording_registry: RecordingRegistry,
    ):
        """Detached-capable executor: spawn is used and the TASK_STARTED
        event carries the (executor, ref) so the execution is re-attachable."""
        task = SyncOnlyTask(name="detached-spawn")
        executor = FakeDetachedExecutor()

        summary = await build_aio(
            [task], task_executor=executor, registry=recording_registry
        )

        assert summary.status == BuildExitStatus.SUCCESS
        assert task.complete()
        assert executor.spawn_calls == [task.id]
        assert executor.submit_calls == []
        extra = _start_call_extras(recording_registry, task.id)
        assert extra["executor"] == FAKE_EXECUTOR_NAME
        assert extra["executor_ref"] == f"spawned-{task.id}"

    async def test_non_detached_executor_uses_submit(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
        recording_registry: RecordingRegistry,
    ):
        task = SyncOnlyTask(name="not-detached")
        executor = FakeDetachedExecutor(detached=False)

        summary = await build_aio(
            [task], task_executor=executor, registry=recording_registry
        )

        assert summary.status == BuildExitStatus.SUCCESS
        assert executor.submit_calls == [task.id]
        assert executor.spawn_calls == []
        extra = _start_call_extras(recording_registry, task.id)
        assert extra["executor"] is None
        assert extra["executor_ref"] is None

    async def test_spawn_failure_fails_the_task(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
        recording_registry: RecordingRegistry,
    ):
        task = SyncOnlyTask(name="spawn-fails")
        executor = FakeDetachedExecutor(spawn_error=RuntimeError("spawn exploded"))

        # FAIL_FAST (default) re-raises the task exception to the caller —
        # a spawn failure is a task failure, same as an execution failure.
        with pytest.raises(RuntimeError, match="spawn exploded"):
            await build_aio([task], task_executor=executor, registry=recording_registry)

        assert recording_registry.has_call("task_fail_aio", task.id)
        assert recording_registry.has_call("build_fail_aio")


class TestReattach:
    def _running_info(
        self, task: BaseTask, ref: str, executor: str = FAKE_EXECUTOR_NAME
    ) -> RegisteredTaskInfo:
        return RegisteredTaskInfo(
            task_id=str(task.id),
            latest_status="running",
            latest_executor=executor,
            latest_executor_ref=ref,
        )

    async def test_reattach_to_live_execution(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
        recording_registry: RecordingRegistry,
    ):
        """Registry reports the task RUNNING with a live ref → the engine
        re-attaches instead of spawning a new execution."""
        task = SyncOnlyTask(name="reattach-live")
        ref = "live-ref-1"
        executor = FakeDetachedExecutor(live_refs={ref})
        recording_registry.bulk_register_response = [self._running_info(task, ref)]

        summary = await build_aio(
            [task], task_executor=executor, registry=recording_registry
        )

        assert summary.status == BuildExitStatus.SUCCESS
        assert task.complete()
        assert executor.reattach_calls == [(task.id, FAKE_EXECUTOR_NAME, ref)]
        assert executor.spawn_calls == []  # no duplicate execution spawned
        # The re-attached ref is recorded on this build's TASK_STARTED too.
        extra = _start_call_extras(recording_registry, task.id)
        assert extra["executor_ref"] == ref

    async def test_reattach_not_live_falls_back_to_spawn(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
        recording_registry: RecordingRegistry,
    ):
        """A dead/expired ref (reattach → None) falls back to fresh execution."""
        task = SyncOnlyTask(name="reattach-dead")
        executor = FakeDetachedExecutor(live_refs=set())  # nothing live
        recording_registry.bulk_register_response = [
            self._running_info(task, "dead-ref")
        ]

        summary = await build_aio(
            [task], task_executor=executor, registry=recording_registry
        )

        assert summary.status == BuildExitStatus.SUCCESS
        assert executor.reattach_calls == [(task.id, FAKE_EXECUTOR_NAME, "dead-ref")]
        assert executor.spawn_calls == [task.id]

    async def test_ref_ignored_when_status_not_running(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
        recording_registry: RecordingRegistry,
    ):
        """Executor refs are only trusted while the task is RUNNING."""
        task = SyncOnlyTask(name="reattach-failed-status")
        executor = FakeDetachedExecutor(live_refs={"stale-ref"})
        recording_registry.bulk_register_response = [
            RegisteredTaskInfo(
                task_id=str(task.id),
                latest_status="failed",
                latest_executor=FAKE_EXECUTOR_NAME,
                latest_executor_ref="stale-ref",
            )
        ]

        summary = await build_aio(
            [task], task_executor=executor, registry=recording_registry
        )

        assert summary.status == BuildExitStatus.SUCCESS
        assert executor.reattach_calls == []
        assert executor.spawn_calls == [task.id]

    async def test_reattach_exception_falls_back_to_spawn(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
        recording_registry: RecordingRegistry,
    ):
        """An exception during reattach degrades to normal execution."""
        task = SyncOnlyTask(name="reattach-raises")

        class RaisingReattachExecutor(FakeDetachedExecutor):
            async def reattach(self, task, executor, ref):
                raise ConnectionError("modal unreachable")

        executor = RaisingReattachExecutor()
        recording_registry.bulk_register_response = [
            self._running_info(task, "some-ref")
        ]

        summary = await build_aio(
            [task], task_executor=executor, registry=recording_registry
        )

        assert summary.status == BuildExitStatus.SUCCESS
        assert executor.spawn_calls == [task.id]


class TestOldRegistrySignatureCompat:
    async def test_task_start_without_executor_kwargs_still_works(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
    ):
        """A custom registry overriding the pre-detached task_start_aio
        signature must not break detached execution (refs are dropped)."""
        from stardag.registry import NoOpRegistry

        started: list[UUID] = []

        class OldSignatureRegistry(NoOpRegistry):
            async def task_start_aio(self, build_id, task):  # pyright: ignore[reportIncompatibleMethodOverride]  # old signature (deliberate)
                started.append(task.id)

        task = SyncOnlyTask(name="old-registry-sig")
        executor = FakeDetachedExecutor()

        summary = await build_aio(
            [task], task_executor=executor, registry=OldSignatureRegistry()
        )

        assert summary.status == BuildExitStatus.SUCCESS
        assert started == [task.id]
        assert executor.spawn_calls == [task.id]

    async def test_old_sync_signature_via_default_aio_delegation(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
    ):
        """A registry overriding only the *sync* pre-detached task_start —
        reached through the base class's aio→sync delegation — also keeps
        working (refs dropped via signature inspection)."""
        from stardag.registry import NoOpRegistry

        started: list[UUID] = []

        class OldSyncSignatureRegistry(NoOpRegistry):
            def task_start(self, build_id, task):  # pyright: ignore[reportIncompatibleMethodOverride]  # old signature (deliberate)
                started.append(task.id)

        task = SyncOnlyTask(name="old-sync-registry-sig")
        executor = FakeDetachedExecutor()

        summary = await build_aio(
            [task], task_executor=executor, registry=OldSyncSignatureRegistry()
        )

        assert summary.status == BuildExitStatus.SUCCESS
        assert started == [task.id]

    async def test_type_error_inside_new_signature_registry_propagates(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
    ):
        """A TypeError raised *inside* a new-signature task_start_aio is a
        real bug in the registry implementation and must propagate — the
        old-signature compatibility is signature-inspected, not
        caught-and-retried."""
        from stardag.registry import NoOpRegistry

        class BuggyRegistry(NoOpRegistry):
            async def task_start_aio(
                self,
                build_id,
                task,
                executor=None,
                executor_ref=None,
                executor_metadata=None,
            ):
                raise TypeError("bug inside the registry implementation")

        task = SyncOnlyTask(name="buggy-registry")
        executor = FakeDetachedExecutor()

        with pytest.raises(TypeError, match="bug inside the registry"):
            await build_aio([task], task_executor=executor, registry=BuggyRegistry())


class TestWorkerReportsLifecycle:
    """When the executor's workers self-report lifecycle events, the engine
    suppresses its own completed-reporting but keeps started (immediate
    detached-spawn re-attachability) and failed (fallback)."""

    class ReportingExecutor(FakeDetachedExecutor):
        def reports_lifecycle(self, task: BaseTask) -> bool:
            return True

    async def test_complete_suppressed_start_kept(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
        recording_registry: RecordingRegistry,
    ):
        task = SyncOnlyTask(name="worker-reports")
        executor = self.ReportingExecutor()

        summary = await build_aio(
            [task], task_executor=executor, registry=recording_registry
        )

        assert summary.status == BuildExitStatus.SUCCESS
        assert task.complete()
        methods = recording_registry.call_methods_for(task.id)
        assert "task_start_aio" in methods  # kept: immediate ref recording
        assert "task_complete_aio" not in methods  # worker reports it
        assert summary.task_count.succeeded == 1  # engine bookkeeping intact

    async def test_fail_still_reported_by_engine(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
        recording_registry: RecordingRegistry,
    ):
        """TASK_FAILED stays engine-side as a fallback — the worker may die
        before reporting (spawn failure, OOM, preemption past retries)."""
        task = SyncOnlyTask(name="worker-reports-fail")
        executor = self.ReportingExecutor(
            spawn_error=RuntimeError("worker never started")
        )

        with pytest.raises(RuntimeError, match="worker never started"):
            await build_aio([task], task_executor=executor, registry=recording_registry)

        assert recording_registry.has_call("task_fail_aio", task.id)


class MetadataDetachedExecutor(FakeDetachedExecutor):
    """FakeDetachedExecutor whose handles carry executor metadata."""

    METADATA = {"kind": "fake", "app_name": "meta-app", "workspace": "acme"}

    async def submit_detached(self, task: BaseTask) -> DetachedHandle:
        handle = await super().submit_detached(task)
        return DetachedHandle(
            executor=handle.executor,
            ref=handle.ref,
            wait=handle.wait,
            executor_metadata=self.METADATA,
        )


class TestExecutorMetadataOnStart:
    async def test_handle_metadata_recorded_on_start(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
        recording_registry: RecordingRegistry,
    ):
        """A detached handle's executor_metadata rides on the TASK_STARTED
        call together with the (executor, ref)."""
        task = SyncOnlyTask(name="detached-meta")
        executor = MetadataDetachedExecutor()

        summary = await build_aio(
            [task], task_executor=executor, registry=recording_registry
        )

        assert summary.status == BuildExitStatus.SUCCESS
        extra = _start_call_extras(recording_registry, task.id)
        assert extra["executor"] == FAKE_EXECUTOR_NAME
        assert extra["executor_metadata"] == MetadataDetachedExecutor.METADATA

    async def test_metadata_dropped_for_pre_metadata_registry(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
    ):
        """A registry whose task_start_aio predates the executor_metadata
        kwarg still receives the start (with ref) — no TypeError."""
        from stardag.registry import NoOpRegistry

        starts: list[dict] = []

        class PreMetadataRegistry(NoOpRegistry):
            async def task_start_aio(  # pyright: ignore[reportIncompatibleMethodOverride]  # old signature (deliberate)
                self, build_id, task, executor=None, executor_ref=None
            ) -> None:
                starts.append({"executor": executor, "executor_ref": executor_ref})

        task = SyncOnlyTask(name="detached-meta-legacy")
        executor = MetadataDetachedExecutor()

        summary = await build_aio(
            [task], task_executor=executor, registry=PreMetadataRegistry()
        )

        assert summary.status == BuildExitStatus.SUCCESS
        assert starts == [
            {"executor": FAKE_EXECUTOR_NAME, "executor_ref": f"spawned-{task.id}"}
        ]
