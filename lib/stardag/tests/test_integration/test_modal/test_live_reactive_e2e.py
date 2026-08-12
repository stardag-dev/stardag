"""Live e2e: a reactive build completes with no resident orchestrator.

Drives the reactive scheduler loop against **real Modal workers** with the
ticks executed locally (watchdog-style, repeatedly), using an in-memory
fake registry fed exclusively by the ticks' own event calls:

1. "Trigger": discovery + registration into the fake registry + task-store
   persistence on the shared Modal volume (exactly what
   ``build_trigger(reactive=True)`` does).
2. Tick #1 rehydrates the task from the store, spawns a real detached
   worker, records the ref, lingers, exits — **no process is now watching
   the task**.
3. Later ticks probe the recorded ref via Modal (`detached_status`) and/or
   the target (ground truth): once the worker finished, the tick self-heals
   the completion and drives the build to COMPLETED.

The worker container has no registry (NoOp) — its lifecycle reporting
no-ops gracefully — so this also exercises the tick's self-heal path, the
resilience story for registry-less/dead workers. The output's saved
function call id must equal the ref recorded by tick #1: the task ran
exactly once, in the worker spawned by the first tick.
"""

import asyncio
import subprocess
import time
import typing
import uuid as uuid_module
from pathlib import Path
from uuid import UUID, uuid4

import pytest

VOLUME_NAME = "stardag-testing"
ROOT_DEFAULT = "stardag/root/default"
TEST_APP_NAME = "stardag-testing-app"

try:
    import modal  # noqa: F401

    from stardag.testing.modal import live_modal_guard

    live_modal_guard(VOLUME_NAME)

    from stardag import flatten_task_struct
    from stardag.build import (
        BuildTaskStore,
        TickConfig,
        discover_and_register_aio,
        run_tick_aio,
    )
    from stardag.build._base import (
        GlobalConcurrencyLockManager,
        LockAcquisitionResult,
        LockAcquisitionStatus,
    )
    from stardag.integration.modal._executor import ModalTaskExecutor
    from stardag.integration.modal._metadata import MODAL_EXECUTOR_NAME
    from stardag.registry import BuildFrontier, FrontierTaskRef, NoOpRegistry
    from stardag.testing.modal._tasks import SleepAndSaveCallId

except ImportError:
    pytest.skip("Skipping modal tests (import not available)", allow_module_level=True)

pytestmark = pytest.mark.modal_live

MODAL_TARGET_ROOT = f"modalvol://{VOLUME_NAME}/{ROOT_DEFAULT}"


@pytest.fixture(autouse=True)
def modal_target_factory():
    from stardag.target._factory import TargetFactory, target_factory_provider

    with target_factory_provider.override(
        TargetFactory(target_roots={"default": MODAL_TARGET_ROOT})
    ):
        yield


@pytest.fixture(scope="module", autouse=True)
def ensure_app_deployed():
    result = subprocess.run(
        ["modal", "deploy", str(Path(__file__).parent / "test__app.py")],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.fail(f"Failed to deploy Modal app:\n{result.stderr}\n{result.stdout}")
    yield


class MiniReactiveRegistry(NoOpRegistry):
    """Just enough registry for locally-driven ticks (single-process test)."""

    def __init__(self, root_task_ids: list[str]):
        super().__init__()
        self.root_task_ids = root_task_ids
        self.statuses: dict[str, str] = {}
        self.upstreams: dict[str, set[str]] = {}
        self.refs: dict[str, tuple[str | None, str | None]] = {}
        self.build_status = "running"
        # The reactive marker/owner/config now lives in the registry (not the
        # target root); set by the trigger via build_set_reactive_meta.
        self.reactive_app_name: str | None = None
        self.reactive_tick_kwargs: dict | None = None

    async def build_set_reactive_meta_aio(
        self, build_id, *, app_name, tick_kwargs=None
    ):
        self.reactive_app_name = app_name
        if tick_kwargs is not None:
            self.reactive_tick_kwargs = tick_kwargs

    async def task_register_bulk_aio(self, build_id, tasks):
        for task in tasks:
            tid = str(task.id)
            self.statuses.setdefault(tid, "pending")
            self.upstreams.setdefault(tid, set()).update(
                str(d.id) for d in flatten_task_struct(task.requires())
            )
        return None

    async def task_start_aio(
        self,
        build_id,
        task,
        executor=None,
        executor_ref=None,
        executor_metadata=None,
        claim_ttl_seconds=None,
    ):
        self.statuses[str(task.id)] = "running"
        self.refs[str(task.id)] = (executor, executor_ref)

    async def task_complete_aio(self, build_id, task):
        self.statuses[str(task.id)] = "completed"

    async def task_fail_aio(self, build_id, task, error_message=None):
        self.statuses[str(task.id)] = "failed"

    async def build_complete_aio(self, build_id):
        self.build_status = "completed"

    async def build_fail_aio(self, build_id, error_message=None):
        self.build_status = "failed"

    async def build_clear_notify_aio(self, build_id):
        pass

    async def build_get_frontier_aio(self, build_id) -> BuildFrontier:
        def ref(tid: str) -> FrontierTaskRef:
            executor, executor_ref = self.refs.get(tid, (None, None))
            return FrontierTaskRef(
                task_id=tid,
                latest_status=self.statuses[tid],
                latest_executor=executor,
                latest_executor_ref=executor_ref,
            )

        actionable = [
            ref(tid)
            for tid, status in self.statuses.items()
            if status in ("pending", "suspended", "running")
            and all(
                self.statuses.get(up) == "completed"
                for up in self.upstreams.get(tid, set())
            )
        ]
        counts: dict[str, int] = {}
        for status in self.statuses.values():
            counts[status] = counts.get(status, 0) + 1
        return BuildFrontier(
            build_id=build_id,
            build_status=self.build_status,
            needs_tick=False,
            root_task_ids=self.root_task_ids,
            roots=[ref(t) for t in self.root_task_ids if t in self.statuses],
            status_counts=counts,
            actionable=actionable,
            reactive_app_name=self.reactive_app_name,
            reactive_tick_kwargs=self.reactive_tick_kwargs,
        )


class LocalLease:
    def __init__(self):
        self.result = LockAcquisitionResult(
            status=LockAcquisitionStatus.ACQUIRED, acquired=True
        )

    def mark_completed(self) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class LocalLockManager:
    """Single-process lease (the test is the only scheduler)."""

    def lock(self, task_id: str) -> LocalLease:
        return LocalLease()

    async def acquire(self, task_id: str) -> LockAcquisitionResult:
        raise NotImplementedError

    async def release(self, task_id: str, task_completed: bool = False) -> bool:
        return True


def test_reactive_build_completes_without_resident_orchestrator():
    salt = uuid_module.uuid4().hex
    task = SleepAndSaveCallId(sleep_seconds=15.0, salt=salt)
    assert not task.complete()

    build_id: UUID = uuid4()
    registry = MiniReactiveRegistry(root_task_ids=[str(task.id)])
    executor = ModalTaskExecutor(
        modal_app_name=TEST_APP_NAME,
        worker_selector=lambda t: "default",
    )
    store = BuildTaskStore(build_id)

    async def trigger():
        # What the reactive bootstrap does (in-container behind
        # build_trigger(reactive=True); see run_reactive_bootstrap):
        # discover + register + persist task objects, and only THEN set
        # the reactive marker/config in the registry.
        discovery = await discover_and_register_aio(registry, build_id, task)
        store.save_tasks(discovery.incomplete.values())
        await registry.build_set_reactive_meta_aio(
            build_id, app_name=TEST_APP_NAME, tick_kwargs={}
        )

    asyncio.run(trigger())

    # Watchdog-style loop: short-lingering local ticks until terminal.
    tick_config = TickConfig(linger_seconds=5.0, poll_interval_seconds=0.5)
    summaries = []
    deadline = time.time() + 240
    while registry.build_status == "running":
        assert time.time() < deadline, (
            f"Timed out; statuses={registry.statuses}, summaries={summaries}"
        )
        summary = asyncio.run(
            run_tick_aio(
                build_id,
                registry=registry,
                task_executor=executor,
                lock_manager=typing.cast(
                    GlobalConcurrencyLockManager, LocalLockManager()
                ),
                task_store=store,
                config=tick_config,
            )
        )
        summaries.append(summary)

    assert registry.build_status == "completed", f"summaries={summaries}"
    total_spawned = sum(s.spawned for s in summaries)
    assert total_spawned == 1, "task must have been spawned exactly once"
    # A later tick observed/healed the completion (the registry-less worker
    # couldn't report it).
    assert sum(s.self_healed for s in summaries) == 1

    # Ground truth: the output was produced by the worker the FIRST tick
    # spawned (ref recorded at spawn == call id saved by the worker).
    assert task.complete()
    recorded_executor, recorded_ref = registry.refs[str(task.id)]
    assert recorded_executor == MODAL_EXECUTOR_NAME
    result = task.load()
    assert result["salt"] == salt
    assert result["call_id"] == recorded_ref
