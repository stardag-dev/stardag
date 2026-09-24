"""The reactive scheduler tick: ``run_tick_aio``.

One tick holds the build's scheduler lease and loops: clear the wake-up
flag, read the frontier, roll the build over if its plan belongs to another
deployment, act on the frontier under the plan's settings, check for the
end, linger polling the flag. See the package docstring.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from uuid import UUID

from stardag.build._base import (
    BuildContext,
    TaskExecutorABC,
    current_build_context_var,
)
from stardag.build._reactive._config import TickConfig, TickSummary
from stardag.build._reactive._frontier_actions import act_on_frontier
from stardag.build._reactive._lease import SchedulerLease
from stardag.build._reactive._rollover import RollOver, RollOverFailed
from stardag.build._reactive._terminal import TERMINAL_BUILD_STATUSES, handle_terminal
from stardag.build._settings import settings_applied
from stardag.build._wakeups import drain_wake_candidates
from stardag.registry import BuildFrontier, RegistryABC

logger = logging.getLogger(__name__)

# Outcomes not worth persisting: the build is not tick-driven at all.
_UNREPORTED_TICK_OUTCOMES = frozenset({"not_reactive"})
# Outcomes that end a tick with nothing left for it to hand off.
_NO_HANDOFF_OUTCOMES = frozenset(
    {"terminal", "not_reactive", "lease_lost", "superseded", "rollover_failed"}
)

# The server caps a summary at 8 KiB; an unbounded message would turn "this
# tick crashed" into no record at all.
_MAX_ERROR_TYPE_CHARS = 128
_MAX_ERROR_MESSAGE_CHARS = 1024
_TRUNCATION_MARKER = "… [truncated]"


def _bounded(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - len(_TRUNCATION_MARKER)] + _TRUNCATION_MARKER


class _Settings:
    """Settings bodies by hash, fetched once per tick."""

    def __init__(self, registry: RegistryABC) -> None:
        self._registry = registry
        self._bodies: dict[str, dict[str, str]] = {}

    async def of(self, settings_hash: str | None) -> dict[str, str]:
        if settings_hash is None:
            return {}
        if settings_hash not in self._bodies:
            info = await self._registry.settings_get_aio(settings_hash)
            self._bodies[settings_hash] = dict(info.body)
        return self._bodies[settings_hash]


async def _hand_off_if_needed(
    build_id: UUID, *, registry: RegistryABC, config: TickConfig, summary: TickSummary
) -> None:
    """Post-release half of the exit handshake: re-read the wake-up flag
    once the lease is released, and spawn a successor if it is set.
    Best-effort — this runs in a ``finally``."""
    if config.spawn_tick is None:
        return
    try:
        flag = await registry.build_get_notify_aio(build_id)
        if not flag.needs_tick:
            return
        build = await registry.build_get_aio(build_id)
        if build.reactive_app_name is None:
            return
        await asyncio.to_thread(config.spawn_tick, build_id, build.reactive_app_name)
        summary.successor_spawned += 1
    except Exception as e:
        logger.warning(
            "Failed to hand off the scheduler for build %s (the flag stays set "
            "for the next completion or the watchdog): %s",
            build_id,
            e,
        )


async def _drain(
    build_id: UUID, *, registry: RegistryABC, config: TickConfig, summary: TickSummary
) -> list[UUID]:
    """Spawn ticks for the flagged builds nobody is serving."""
    if config.spawn_tick is None:
        return []
    spawned = await drain_wake_candidates(
        registry, config.spawn_tick, build_id=build_id
    )
    own = build_id in spawned
    summary.neighbour_ticks_spawned += len(spawned) - (1 if own else 0)
    if own:
        summary.successor_spawned += 1
    return spawned


async def _report(
    build_id: UUID, registry: RegistryABC, config: TickConfig, summary: TickSummary
) -> None:
    """Report the tick's summary. Best-effort: never fails a tick."""
    if not config.report_tick_summaries or summary.outcome in _UNREPORTED_TICK_OUTCOMES:
        return
    try:
        await registry.build_report_tick_summary_aio(build_id, asdict(summary))
    except Exception as e:
        logger.warning(f"Failed to report tick summary for build {build_id}: {e}")


async def run_tick_aio(
    build_id: UUID,
    *,
    registry: RegistryABC,
    task_executor: TaskExecutorABC,
    config: TickConfig | None = None,
    deployment_id: UUID | None = None,
    roll_over: RollOver | None = None,
) -> TickSummary:
    """Run one reactive scheduler tick for ``build_id``.

    Args:
        build_id: The build.
        registry: The registry (the scheduler state).
        task_executor: Spawns executions detached (``supports_detached``).
        config: The tick's configuration.
        deployment_id: This tick's own deployment (``STARDAG_DEPLOYMENT_ID``).
            When the build's active plan names another one, the tick calls
            ``roll_over`` — or, without it, exits ``superseded``. None skips
            the comparison (a caller that is not a deployment).
        roll_over: The rollover hook (see
            :data:`~stardag.build._reactive._rollover.RollOver`).

    Idempotent and safe to invoke at any time (single-flighted by the
    scheduler lease; a no-op on a build that is not reactively scheduled).
    The summary is reported to the registry on the way out, a crashed tick
    included (``outcome="error"``), and the original exception re-raised.
    """
    config = config or TickConfig()
    summary = TickSummary(outcome="lingered_out")
    try:
        await _tick_body(
            build_id,
            registry=registry,
            task_executor=task_executor,
            config=config,
            summary=summary,
            deployment_id=deployment_id,
            roll_over=roll_over,
        )
    except Exception as e:
        summary.outcome = "error"
        summary.error_type = _bounded(type(e).__name__, _MAX_ERROR_TYPE_CHARS)
        summary.error_message = _bounded(str(e), _MAX_ERROR_MESSAGE_CHARS)
        await _report(build_id, registry, config, summary)
        raise
    await _report(build_id, registry, config, summary)
    return summary


class _Driver:
    """One tick's state while it holds the lease."""

    def __init__(
        self,
        build_id: UUID,
        *,
        registry: RegistryABC,
        task_executor: TaskExecutorABC,
        config: TickConfig,
        summary: TickSummary,
        deployment_id: UUID | None,
        roll_over: RollOver | None,
    ) -> None:
        self.build_id = build_id
        self.registry = registry
        self.executor = task_executor
        self.config = config
        self.summary = summary
        self.deployment_id = deployment_id
        self.roll_over = roll_over
        self.rollover_attempted = False
        self.settings = _Settings(registry)
        self.cleared_a_wakeup = False

    async def _read(self) -> BuildFrontier:
        await self.registry.build_clear_notify_aio(self.build_id)
        self.cleared_a_wakeup = True
        return await self.registry.build_get_frontier_aio(self.build_id)

    async def _follow_deployment(self, frontier: BuildFrontier) -> BuildFrontier | None:
        """The frontier to act on, rolled over to this tick's deployment if
        the plan names another; None when the tick must stop (the outcome
        is set)."""
        if (
            self.deployment_id is None
            or frontier.plan_id is None
            or frontier.deployment_id == self.deployment_id
        ):
            return frontier
        if self.roll_over is None or self.rollover_attempted:
            self.summary.outcome = "superseded"
            return None
        self.rollover_attempted = True
        try:
            outcome = await self.roll_over(frontier)
        except RollOverFailed as e:
            self.summary.outcome = "rollover_failed"
            self.summary.error_type = type(e).__name__
            self.summary.error_message = _bounded(str(e), _MAX_ERROR_MESSAGE_CHARS)
            return None
        if outcome != "rolled":
            self.summary.outcome = "superseded"
            return None
        self.summary.rolled_over += 1
        frontier = await self.registry.build_get_frontier_aio(self.build_id)
        if frontier.deployment_id != self.deployment_id:
            self.summary.outcome = "superseded"
            return None
        return frontier

    async def one_pass(self) -> tuple[bool, bool]:
        """Read and act once. Returns ``(stop, acted)``."""
        self.summary.iterations += 1
        frontier = await self._read()
        if frontier.reactive_app_name is None:
            self.summary.outcome = "not_reactive"
            return True, False
        if frontier.build_status in TERMINAL_BUILD_STATUSES:
            self.summary.outcome = "terminal"
            self.summary.terminal_status = frontier.build_status
            return True, False
        if frontier.plan_id is None:
            # The static phase has not registered a plan yet (a bootstrap
            # in flight): nothing to act on.
            return False, False
        followed = await self._follow_deployment(frontier)
        if followed is None:
            return True, False
        frontier = followed
        settings = await self.settings.of(frontier.settings_hash)
        token = current_build_context_var.set(
            BuildContext(
                build_id=self.build_id,
                plan_id=frontier.plan_id,
                deployment_id=frontier.deployment_id,
                settings=settings,
            )
        )
        try:
            with settings_applied(settings):
                result = await act_on_frontier(
                    frontier,
                    registry=self.registry,
                    task_executor=self.executor,
                    config=self.config,
                    summary=self.summary,
                )
        finally:
            current_build_context_var.reset(token)
        if result.superseded:
            self.summary.outcome = "superseded"
            return True, result.acted
        terminal = await handle_terminal(
            frontier,
            build_id=self.build_id,
            registry=self.registry,
            config=self.config,
            summary=self.summary,
            pass_result=result,
        )
        if terminal is not None:
            self.summary.outcome = "terminal"
            self.summary.terminal_status = terminal
            return True, result.acted
        return False, result.acted

    async def drive(self, lease: SchedulerLease) -> None:
        """The loop: act, then linger polling the flag until the deadline.

        **The exit handshake.** A worker may skip spawning a tick when the
        registry reports a scheduler live, which is sound only if the live
        scheduler is guaranteed to see a flag set before it releases the
        lease. So at deadline expiry, *before* releasing, the flag is
        re-read (and the tick re-acts if set); and *after* releasing, it is
        re-read once more and a successor spawned (see the ``finally`` in
        :func:`_tick_body`).
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.config.linger_seconds
        while True:
            if lease.lost:
                self.summary.outcome = "lease_lost"
                return
            stop, acted = await self.one_pass()
            if stop:
                return
            if acted:
                deadline = loop.time() + self.config.linger_seconds
                await _drain(
                    self.build_id,
                    registry=self.registry,
                    config=self.config,
                    summary=self.summary,
                )
                continue
            while True:
                if loop.time() >= deadline:
                    if self.config.linger_seconds <= 0:
                        return
                    flag = await self.registry.build_get_notify_aio(self.build_id)
                    if not flag.needs_tick:
                        return
                    self.summary.linger_extended += 1
                    deadline = loop.time() + self.config.linger_seconds
                    break
                await asyncio.sleep(self.config.poll_interval_seconds)
                if lease.lost:
                    self.summary.outcome = "lease_lost"
                    return
                flag = await self.registry.build_get_notify_aio(self.build_id)
                if flag.needs_tick:
                    break


_warned_missing_spawner = False


async def _tick_body(
    build_id: UUID,
    *,
    registry: RegistryABC,
    task_executor: TaskExecutorABC,
    config: TickConfig,
    summary: TickSummary,
    deployment_id: UUID | None,
    roll_over: RollOver | None,
) -> None:
    global _warned_missing_spawner
    lease = SchedulerLease(registry, build_id)
    driver = _Driver(
        build_id,
        registry=registry,
        task_executor=task_executor,
        config=config,
        summary=summary,
        deployment_id=deployment_id,
        roll_over=roll_over,
    )
    acquired = False
    try:
        async with lease:
            if not lease.acquired:
                summary.outcome = "lease_held"
                return
            acquired = True
            if config.spawn_tick is None and not _warned_missing_spawner:
                _warned_missing_spawner = True
                logger.warning(
                    "Scheduler tick running without TickConfig.spawn_tick: it "
                    "cannot hand off on the way out or wake other builds. "
                    "Reported once per process."
                )
            await driver.drive(lease)
    finally:
        drained: list[UUID] = []
        if acquired and summary.outcome != "not_reactive":
            drained = await _drain(
                build_id, registry=registry, config=config, summary=summary
            )
        if (
            acquired
            and driver.cleared_a_wakeup
            and build_id not in drained
            and summary.outcome not in _NO_HANDOFF_OUTCOMES
        ):
            await _hand_off_if_needed(
                build_id, registry=registry, config=config, summary=summary
            )
