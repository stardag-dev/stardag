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
  ``TickConfig.unspawned_grace_seconds``, and the end is *conditional* on there
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

**A residual risk, stated rather than assumed away.** A Modal worker with
``retries`` set is retried by Modal under the same call. Reconciliation
relies on ``get(timeout=0)`` answering only the call's *final* outcome; if
Modal ever exposed a failed attempt's output while the next attempt runs,
a ``"raised"`` read would end an execution whose retry is running -- a
duplicate if that retry had already passed checkpoint one. Modal documents
outputs per call, not per attempt, but nothing here can verify it; a
registry-live scenario with ``retries > 0`` is the check.

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
import random
import typing
from datetime import datetime, timezone

from stardag.build._base import DetachedState, TaskExecutorABC
from stardag.build._reactive._frontier_actions import is_registry_outage, rehydrate
from stardag.exceptions import APIError
from stardag.registry import BuildFrontier, ExecutionInfo, RegistryABC

if typing.TYPE_CHECKING:
    from stardag.build._reactive._config import TickSummary

logger = logging.getLogger(__name__)

#: Probes in flight at once (each is one Modal API call), and how long one
#: may take before it counts as ``"unknown"``.
_MAX_CONCURRENT_PROBES = 8
_PROBE_TIMEOUT_SECONDS = 15.0
#: Holders examined per round: with the probe bound, a round fits well
#: inside a tick's claim deadline; the rest wait for the next round, or a
#: successor's (each probe is also skipped once the tick is out of time).
_MAX_HOLDERS_PER_ROUND = 32


async def _contained(probe: typing.Awaitable[DetachedState]) -> DetachedState:
    """``probe``, never raising past it: anything but this tick's own
    cancellation is ``"unknown"``. Inside the coroutine, not around it --
    on Python 3.11 ``asyncio.wait_for`` runs it as a task, and a
    ``SystemExit`` escaping a task is re-raised by the event loop rather
    than handed to the awaiter."""
    try:
        return await probe
    except (asyncio.CancelledError, KeyboardInterrupt):
        raise
    except BaseException as e:
        logger.debug(f"A probe raised {e!r}; treating the call as unknown.")
        return "unknown"


class Reconciler:
    """One tick's reconciliation state: when it last ran, and whether the
    registry supports the conditional end."""

    def __init__(
        self,
        *,
        interval_seconds: float = 60.0,
        unspawned_grace_seconds: float = 600.0,
        max_executions: int | None = None,
    ) -> None:
        self.interval_seconds = interval_seconds
        self.unspawned_grace_seconds = unspawned_grace_seconds
        # A member that has spent its execution budget is not re-driven
        # here: ending its execution ``lost`` makes it CANCELLED, which the
        # tick's budget check (a takeover's) does not gate. Its claim is
        # left to lapse, and the takeover then fails it under the budget.
        self.max_executions = max_executions
        self._last: float | None = None
        self.supported = True

    def due(self, now: float) -> bool:
        return self.supported and (
            self._last is None or now - self._last >= self.interval_seconds
        )

    async def run(
        self,
        frontier: BuildFrontier,
        *,
        registry: RegistryABC,
        task_executor: TaskExecutorABC,
        summary: "TickSummary",
        lease_lost: typing.Callable[[], bool],
        out_of_time: typing.Callable[[], bool] = lambda: False,
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
            and (
                self.max_executions is None
                or running[e.task_id].attempts < self.max_executions
            )
        ]
        # A random sample, not the ledger's first few: rounds (and successor
        # ticks, which start afresh) then reach every holder eventually,
        # even past a prefix of calls that stay running.
        if len(holders) > _MAX_HOLDERS_PER_ROUND:
            holders = random.sample(holders, _MAX_HOLDERS_PER_ROUND)
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
                    out_of_time=out_of_time,
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
        out_of_time: typing.Callable[[], bool],
        probes: asyncio.Semaphore,
    ) -> bool:
        assert execution.executor is not None and execution.task_id is not None
        try:
            if execution.executor_ref is None:
                return await self._unspawned(execution, registry, summary, lease_lost)
            if out_of_time():
                return False
            async with probes:
                try:
                    state = await asyncio.wait_for(
                        _contained(
                            task_executor.probe_detached(
                                execution.executor, execution.executor_ref
                            )
                        ),
                        timeout=_PROBE_TIMEOUT_SECONDS,
                    )
                except TimeoutError:
                    state = "unknown"
            if state not in ("returned", "raised") or lease_lost():
                return False
            if state == "raised":
                return await self._raised(execution, registry, summary)
            return await self._returned(
                execution, running[execution.task_id], registry, summary
            )
        except (asyncio.CancelledError, KeyboardInterrupt):
            raise
        except BaseException as e:
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
        if age < self.unspawned_grace_seconds:
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
