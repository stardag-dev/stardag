"""Reconciliation: a tick ending the executions of its build that are dead
but still hold a claim, so their tasks are re-driven now rather than when
the claim lapses (STA-94, STA-135).

An execution holding a live claim is listed ``running`` on the frontier
whether or not anything is running it. Two ways it can be dead:

* its call **finished without reporting** -- the worker's end report was
  lost (a rate limit outlasting its retries, an outage), or the container
  was killed before it could report;
* its call **never started** -- the tick that claimed it died, or failed,
  between the claim and the spawn, so no executor ref was ever recorded.

Left alone, either holds the task for the claim's TTL: the executor's
timeout plus 15 minutes.

**The invariant** (stated before the code; a duplicate execution costs more
than a delayed one, so every doubt resolves to "leave it"). An execution is
ended by anyone other than itself only when that cannot coexist with it
running its task:

* **R1** -- its executor positively reports the call finished
  (:meth:`~stardag.build.TaskExecutorABC.probe_detached` answers
  ``"returned"`` or ``"raised"``). A finished call reports nothing more.
* **R2** -- it never recorded an executor ref, it is older than
  :data:`UNSPAWNED_GRACE_SECONDS`, and the end is *conditional* on there
  still being no ref when the registry applies it
  (``only_if_unspawned``, decided under the task row lock the ref record
  also takes). Paired with the worker rule: a Modal worker does not enter
  ``run()`` unless its own ref is on the ledger
  (``_WorkerLifecycleReporter._require_ref_recorded``). So a container
  that landed after all either recorded its ref first -- and the end is
  refused -- or finds its execution ended at checkpoint one and stops.

Everything else is left to the claim's TTL: a call still running, pending
or restarting, a probe that fails or cannot tell, an executor that cannot
probe, and a registry that predates the conditional end (it refuses the
field, and reconciliation turns itself off for the tick). **The scheduler
lease is not load-bearing for safety**: two ticks reconciling at once
issue the same idempotent ends, and a zombie tick's late spawn is caught
by the rule above. The lease bounds wasted work, nothing more.

What an end does to the task:

* returned, target complete -- the completion is reported for the
  execution, as its worker would have;
* returned, target not complete -- (a yield whose report was lost, or a
  run that wrote nothing) the execution is ended ``lost``, the task becomes
  CANCELLED, and the next claiming start re-runs it;
* raised -- the failure is reported for the execution, so it counts
  against the build's retry budget like any other;
* never spawned -- ended ``lost``, and re-driven.
"""

from __future__ import annotations

import asyncio
import logging
import typing
from datetime import datetime, timezone

from stardag.build._base import TaskExecutorABC
from stardag.build._reactive._frontier_actions import is_registry_outage, rehydrate
from stardag.exceptions import APIError
from stardag.registry import BuildFrontier, ExecutionInfo, RegistryABC

if typing.TYPE_CHECKING:
    from stardag.build._reactive._config import TickSummary

logger = logging.getLogger(__name__)

#: How old a claim with no executor ref must be before it is ended as a
#: spawn that never landed. A live tick records the ref within seconds of
#: its claim (one spawn call, retried, then the record, under the rate
#: limit's backoff); this is long enough that only a dead one is still
#: missing it, and it is a delay, not a safety bound -- the conditional end
#: is what makes it safe.
UNSPAWNED_GRACE_SECONDS = 600.0

#: At most this often per tick: each round reads the build's ledger and
#: probes every running member's call.
RECONCILE_INTERVAL_SECONDS = 60.0

#: Probes in flight at once (each is one Modal API call).
_MAX_CONCURRENT_PROBES = 8


class Reconciler:
    """One tick's reconciliation state: when it last ran, and whether the
    registry supports the conditional end."""

    def __init__(self) -> None:
        self._last: float | None = None
        self.supported = True

    def due(self, now: float) -> bool:
        return self.supported and (
            self._last is None or now - self._last >= RECONCILE_INTERVAL_SECONDS
        )

    async def run(
        self,
        frontier: BuildFrontier,
        *,
        registry: RegistryABC,
        task_executor: TaskExecutorABC,
        summary: "TickSummary",
        lease_lost: typing.Callable[[], bool],
    ) -> bool:
        """One round over ``frontier``'s running members. Returns whether it
        ended anything (the frontier is then stale). Never raises: a round
        that fails is a round that did nothing, and the claims keep their
        TTL."""
        self._last = asyncio.get_running_loop().time()
        running = {m.task_id: m for m in frontier.running}
        if not running:
            return False
        try:
            ledger = await registry.build_list_executions_aio(frontier.build_id)
        except Exception as e:
            logger.info(f"Reconciliation skipped: could not read the ledger: {e}")
            return False
        holders = [
            e
            for e in ledger
            if e.task_id in running
            and e.claim_released_at is None
            and e.ended_at is None
            and e.executor is not None
            and task_executor.reconciles(e.executor)
        ]
        if not holders:
            return False
        probes = asyncio.Semaphore(_MAX_CONCURRENT_PROBES)
        acted = await asyncio.gather(
            *(
                self._one(
                    execution,
                    running,
                    registry=registry,
                    task_executor=task_executor,
                    summary=summary,
                    lease_lost=lease_lost,
                    probes=probes,
                )
                for execution in holders
            )
        )
        return any(acted)

    async def _one(
        self,
        execution: ExecutionInfo,
        running: dict,
        *,
        registry: RegistryABC,
        task_executor: TaskExecutorABC,
        summary: "TickSummary",
        lease_lost: typing.Callable[[], bool],
        probes: asyncio.Semaphore,
    ) -> bool:
        assert execution.executor is not None and execution.task_id is not None
        try:
            if execution.executor_ref is None:
                return await self._unspawned(execution, registry, summary, lease_lost)
            async with probes:
                state = await task_executor.probe_detached(
                    execution.executor, execution.executor_ref
                )
            if state not in ("returned", "raised") or lease_lost():
                return False
            if state == "raised":
                return await self._raised(execution, registry, summary)
            return await self._returned(
                execution, running[execution.task_id], registry, summary
            )
        except Exception as e:
            # Best-effort: the claim keeps its TTL. Refusals land here too
            # (the execution moved on between the read and the write).
            level = logging.INFO if is_registry_outage(e) else logging.WARNING
            logger.log(
                level,
                f"Reconciling execution {execution.id} of task "
                f"{execution.task_id} did nothing: {e}",
            )
            return False

    async def _unspawned(
        self,
        execution: ExecutionInfo,
        registry: RegistryABC,
        summary: "TickSummary",
        lease_lost: typing.Callable[[], bool],
    ) -> bool:
        started = execution.started_at
        if started is None or lease_lost():
            return False
        age = (datetime.now(timezone.utc) - started).total_seconds()
        if age < UNSPAWNED_GRACE_SECONDS:
            return False
        try:
            await registry.execution_report_stopped_aio(
                execution.id, outcome="lost", only_if_unspawned=True
            )
        except APIError as e:
            if e.code == "execution_spawned":
                return False
            if e.status_code == 422:
                # A registry that predates the conditional end refuses the
                # field rather than ignoring it: never fall back to the
                # unconditional end; stop reconciling unspawned claims.
                self.supported = False
                logger.warning(
                    "The registry does not support conditional execution "
                    "ends; claims whose spawn never landed are left to "
                    "their TTL."
                )
                return False
            raise
        logger.warning(
            f"Execution {execution.id} of task {execution.task_id} recorded no "
            f"executor ref in {age:.0f}s (its spawn never landed); ended as "
            "lost, the task is re-driven."
        )
        summary.reconciled += 1
        return True

    async def _raised(
        self, execution: ExecutionInfo, registry: RegistryABC, summary: "TickSummary"
    ) -> bool:
        assert execution.plan_id is not None and execution.task_id is not None
        await registry.member_fail_aio(
            execution.plan_id,
            execution.task_id,
            execution_id=execution.id,
            error_message=(
                f"Execution {execution.id} ended without reporting: its "
                f"{execution.executor} call {execution.executor_ref!r} raised."
            ),
        )
        logger.warning(
            f"Execution {execution.id} of task {execution.task_id}: its call "
            "raised and its failure was never reported; reported now."
        )
        summary.reconciled += 1
        return True

    async def _returned(
        self,
        execution: ExecutionInfo,
        member,
        registry: RegistryABC,
        summary: "TickSummary",
    ) -> bool:
        assert execution.plan_id is not None and execution.task_id is not None
        task = rehydrate(member)
        if await task.complete_aio():
            await registry.member_complete_aio(
                execution.plan_id, execution.task_id, execution_id=execution.id
            )
            what = "its target is complete; completion reported now"
        else:
            await registry.execution_report_stopped_aio(execution.id, outcome="lost")
            what = "its target is not complete; ended as lost, the task is re-driven"
        logger.warning(
            f"Execution {execution.id} of task {execution.task_id}: its call "
            f"returned without reporting its end; {what}."
        )
        summary.reconciled += 1
        return True
