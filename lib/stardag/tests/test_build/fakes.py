"""Executor doubles for the build tests.

:class:`FakeDetachedExecutor` "spawns" by running the task in this process
— immediately inside ``wait()`` for a resident build, or, with
``workers=True``, as a background worker that reports its own lifecycle to
the registry the way the Modal ``Runner`` does (its non-claiming start,
completion or failure, and a ``/yield`` with ``suspend: true`` when the
generator yields incomplete dependencies), then wakes the build. That is
the shape a reactive tick drives.
"""

from __future__ import annotations

import asyncio
from uuid import UUID

from stardag import BaseTask, TaskStruct, flatten_task_struct
from stardag.build import (
    DetachedHandle,
    TaskExecutionError,
    TaskExecutorABC,
    get_current_build_context,
)
from stardag.build._registration import send_yield_aio, walk_aio, yield_batches
from stardag.exceptions import APIError, ExecutionCancelled, execution_not_wanted
from stardag.registry import RegistryABC

FAKE_EXECUTOR = "fake"


def _drive(task: BaseTask) -> None | TaskStruct:
    """Run ``task``; for a generator, advance past complete yields and return
    the first yield with an incomplete dependency (re-execution pattern)."""
    result = task.run()
    if result is None or not hasattr(result, "__next__"):
        return None
    try:
        while True:
            deps = flatten_task_struct(next(result))
            if any(not dep.complete() for dep in deps):
                return tuple(deps)
    except StopIteration:
        return None


class FakeDetachedExecutor(TaskExecutorABC):
    """See the module docstring."""

    def __init__(
        self,
        *,
        registry: RegistryABC | None = None,
        workers: bool = False,
        reports: bool | None = None,
        spawn_error: Exception | None = None,
        app_name: str | None = None,
        timeout_seconds: float | None = None,
        worker_delay: float = 0.01,
    ) -> None:
        self.registry = registry
        self.workers = workers
        self.reports = workers if reports is None else reports
        self.spawn_error = spawn_error
        self.app_name = app_name
        self.timeout_seconds = timeout_seconds
        self.worker_delay = worker_delay
        # (task id, execution id, plan id) per spawn, in order.
        self.spawns: list[tuple[UUID, UUID, UUID | None]] = []
        self.cancel_detached_calls: list[tuple[UUID, str, str]] = []
        self.cancelled: list[UUID] = []
        self.background: list[asyncio.Task] = []
        self.ticks_spawned: list[tuple[UUID, str]] = []
        # What ``probe_detached`` answers, by ref (default ``"unknown"``).
        self.probes: dict[str, str] = {}

    # -- executor surface ------------------------------------------------------------

    async def submit(self, task: BaseTask) -> None | TaskStruct | TaskExecutionError:
        return _drive(task)

    def supports_detached(self, task: BaseTask) -> bool:
        return True

    def detached_executor_name(self, task: BaseTask) -> str | None:
        return FAKE_EXECUTOR

    def reports_lifecycle(self, task: BaseTask) -> bool:
        return self.reports

    def deployment_app_name(self) -> str | None:
        return self.app_name

    def execution_timeout_seconds(self, task: BaseTask) -> float | None:
        return self.timeout_seconds

    async def get_executor_metadata(self, task: BaseTask) -> dict | None:
        return {"kind": FAKE_EXECUTOR, "task": task.get_name()}

    async def submit_detached(
        self, task: BaseTask, *, execution_id: UUID
    ) -> DetachedHandle:
        if self.spawn_error is not None:
            raise self.spawn_error
        context = get_current_build_context()
        plan_id = context.plan_id if context else None
        deployment_id = context.deployment_id if context else None
        self.spawns.append((task.id, execution_id, plan_id))
        ref = f"ref-{execution_id}"
        if self.workers:
            worker = asyncio.create_task(
                self._worker(task, execution_id, plan_id, deployment_id, ref)
            )
            self.background.append(worker)

            async def wait_for_worker() -> None | TaskStruct | TaskExecutionError:
                return await worker

            return DetachedHandle(
                FAKE_EXECUTOR, ref, wait_for_worker, {"kind": FAKE_EXECUTOR}
            )

        async def wait() -> None | TaskStruct | TaskExecutionError:
            try:
                return _drive(task)
            except Exception as e:
                return TaskExecutionError(exception=e, traceback=str(e))

        return DetachedHandle(FAKE_EXECUTOR, ref, wait, {"kind": FAKE_EXECUTOR})

    async def _worker(
        self,
        task: BaseTask,
        execution_id: UUID,
        plan_id: UUID | None,
        deployment_id: UUID | None,
        ref: str,
    ) -> None | TaskStruct | TaskExecutionError:
        """A self-reporting worker: what the Modal runner does, in-process."""
        await asyncio.sleep(self.worker_delay)
        registry = self.registry
        assert (
            registry is not None and plan_id is not None and deployment_id is not None
        )
        task_id = str(task.id)
        try:
            await registry.member_start_aio(
                plan_id,
                task_id,
                execution_id=execution_id,
                claim=False,
                executor_ref=ref,
            )
        except Exception as e:
            # The Modal runner's start rules: a refused start (the execution
            # is over) runs nothing, and an unanswered one runs only once
            # the ledger names this worker's ref (the worker rule that makes
            # reconciliation safe, ``_reporter._require_ref_recorded``).
            if not self._may_run_after_unrecorded_start(e, execution_id, plan_id, ref):
                return TaskExecutionError(
                    exception=ExecutionCancelled(f"start not recorded: {e}"),
                    traceback=str(e),
                )
        try:
            result = _drive(task)
        except Exception as e:
            await registry.member_fail_aio(
                plan_id, task_id, execution_id=execution_id, error_message=str(e)
            )
            self._wake(plan_id)
            return TaskExecutionError(exception=e, traceback=str(e))
        if result is None:
            await registry.member_complete_aio(
                plan_id, task_id, execution_id=execution_id
            )
        else:
            children = flatten_task_struct(result)
            walk = await walk_aio(children)
            await send_yield_aio(
                registry,
                yield_batches(walk, children, suspend=True),
                plan_id=plan_id,
                task_id=task_id,
                execution_id=execution_id,
                deployment_id=deployment_id,
            )
        self._wake(plan_id)
        return result

    def _may_run_after_unrecorded_start(
        self, error: Exception, execution_id: UUID, plan_id: UUID, ref: str
    ) -> bool:
        if isinstance(error, APIError) and execution_not_wanted(error):
            return False
        registry = self.registry
        plans = getattr(registry, "plans", None)
        if registry is None or plans is None or plan_id not in plans:
            return False
        try:
            ledger = registry.build_list_executions(plans[plan_id].build_id)
        except Exception:
            return False
        own = next((x for x in ledger if x.id == execution_id), None)
        return own is not None and own.executor_ref == ref

    def _wake(self, plan_id: UUID) -> None:
        registry = self.registry
        plans = getattr(registry, "plans", None)
        if plans is not None and plan_id in plans:
            registry.build_notify(plans[plan_id].build_id)  # type: ignore[union-attr]

    async def cancel(self, task: BaseTask) -> None:
        self.cancelled.append(task.id)

    def reconciles(self, executor: str) -> bool:
        return executor == FAKE_EXECUTOR

    def reconciles_unspawned(self, executor: str) -> bool:
        # Its self-reporting worker keeps the start rules (``_worker``).
        return executor == FAKE_EXECUTOR

    async def probe_detached(self, executor: str, ref: str):  # type: ignore[override]
        return self.probes.get(ref, "unknown")

    async def cancel_detached(self, task: BaseTask, executor: str, ref: str) -> None:
        self.cancel_detached_calls.append((task.id, executor, ref))

    def can_spawn_scheduler_ticks(self) -> bool:
        return self.app_name is not None

    def spawn_scheduler_tick(self, build_id: UUID, app_name: str) -> None:
        self.ticks_spawned.append((build_id, app_name))

    async def drain(self) -> None:
        """Wait for every background worker to finish."""
        while self.background:
            await asyncio.gather(*self.background, return_exceptions=True)
            self.background = [t for t in self.background if not t.done()]

    async def setup(self) -> None:
        pass

    async def teardown(self) -> None:
        pass
