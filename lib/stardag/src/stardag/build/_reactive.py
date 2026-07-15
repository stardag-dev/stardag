"""Reactive (tick-based) build scheduling.

Instead of a resident orchestrator process that lives for the whole build,
reactive scheduling runs short-lived, idempotent **ticks**:

1. Acquire the build's scheduler lease (single-flight — a second concurrent
   tick exits immediately; the wake-up it was spawned for is covered by the
   holder's dirty-flag re-check).
2. Loop: clear the build's wake-up flag → fetch the frontier from the
   registry → act on it (spawn pending/suspended tasks detached, probe
   running refs, self-heal completions, handle terminal states) → linger
   briefly polling the wake-up flag → exit when quiet.

Workers self-report their lifecycle (see the Modal ``Runner``) and wake the
scheduler when they finish, so no process needs to stay alive while
long-running tasks execute. A periodic watchdog tick covers lost wake-ups
(worker died silently) and externally-triggered state changes (e.g. build
cancelled from the UI).

The tick is executor-agnostic: it only needs a :class:`TaskExecutorABC`
with detached support. Requirements and current limitations:

- A real registry (frontier computation is registry-backed; the reactive
  marker/owner live in the frontier's ``reactive_app_name``).
- Task objects are rehydrated from the :class:`BuildTaskStore` — written by
  the trigger (initial discovery) and by workers (dynamic deps).
- The global concurrency lock and build-local ``ConcurrencyConfig`` limits
  are not applied by ticks (infra-level limits, e.g. Modal per-function
  ``concurrency_limit``, still apply). Registry-backed named limits *are*
  applied, via ``TickConfig.limit_key_selector``.
- On failure (FAIL_FAST) the build is failed, running executions are
  cancelled, and blocked descendants are marked SKIPPED (server-computed;
  older servers without the skip-blocked endpoint are tolerated).
"""

from __future__ import annotations

import asyncio
import logging
import typing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Sequence
from uuid import UUID

from stardag import (
    BaseTask,
    TaskStruct,
    flatten_task_struct,
    task_from_registry_data,
)
from stardag.build._base import (
    DetachedExecutionStatus,
    FailMode,
    GlobalConcurrencyLockManager,
    TaskExecutorABC,
    current_build_id_var,
)
from stardag.build._task_store import BuildTaskStore
from stardag.exceptions import NotFoundError, is_missing_route_error
from stardag.registry._api_registry import _is_route_not_found
from stardag.registry import BuildFrontier, FrontierTaskRef, RegistryABC
from stardag.registry._base import accepts_executor_metadata_kwarg

logger = logging.getLogger(__name__)

SCHEDULER_LOCK_PREFIX = "__scheduler__:"

# Statuses considered "in flight" for terminal detection.
_RUNNING_STATUSES = ("running",)
_TERMINAL_BUILD_STATUSES = ("completed", "failed", "cancelled")


def scheduler_lock_name(build_id: UUID) -> str:
    """Lease name for a build's scheduler single-flight lock."""
    return f"{SCHEDULER_LOCK_PREFIX}{build_id}"


# =============================================================================
# Discovery (shared by trigger and workers)
# =============================================================================


@dataclass
class DiscoveryResult:
    """Result of :func:`discover_and_register_aio`."""

    # Incomplete tasks by UUID — these need scheduling (and persisting).
    incomplete: dict[UUID, BaseTask] = field(default_factory=dict)
    # Tasks found already complete (registered + marked complete).
    previously_completed: list[BaseTask] = field(default_factory=list)
    # Tasks whose failed/cancelled/skipped registry status was reset to
    # pending (only when retry_failed=True).
    retried: list[BaseTask] = field(default_factory=list)


_RETRYABLE_STATUSES = ("failed", "cancelled", "skipped")


async def discover_and_register_aio(
    registry: RegistryABC,
    build_id: UUID,
    tasks: TaskStruct,
    retry_failed: bool = False,
    _chunk_size: int = 50,
) -> DiscoveryResult:
    """Walk ``tasks``' dependency trees, register everything, return state.

    Post-order walk (deps before parents, so the bulk endpoint resolves
    ``dependency_task_ids`` without phantom rows), stopping at
    already-complete tasks (their subtrees are irrelevant). Complete tasks
    are additionally marked complete in the registry so the frontier
    reflects them (in reactive mode the registry *is* the scheduler state).

    Used by the reactive trigger (initial discovery) and by workers
    registering dynamically yielded deps.

    With ``retry_failed=True``, incomplete tasks whose registry status is
    failed/cancelled/skipped (from a previous build) are reset to pending
    via ``task_retry`` — without it, a previously failed task would never
    enter the frontier and would FAIL_FAST a new build on its first tick.
    """
    result = DiscoveryResult()
    post_order: list[BaseTask] = []
    seen: set[UUID] = set()

    async def walk(task: BaseTask) -> None:
        if task.id in seen:
            return
        seen.add(task.id)
        if await task.complete_aio():
            result.previously_completed.append(task)
            post_order.append(task)
            return  # don't recurse below complete tasks
        for dep in flatten_task_struct(task.requires()):
            await walk(dep)
        result.incomplete[task.id] = task
        post_order.append(task)

    for task in flatten_task_struct(tasks):
        await walk(task)

    for chunk_start in range(0, len(post_order), _chunk_size):
        chunk = post_order[chunk_start : chunk_start + _chunk_size]
        infos = await registry.task_register_bulk_aio(build_id, chunk)
        if retry_failed:
            for info in infos or []:
                if (
                    info.latest_status in _RETRYABLE_STATUSES
                    and UUID(info.task_id) in result.incomplete
                ):
                    task = result.incomplete[UUID(info.task_id)]
                    await registry.task_retry_aio(build_id, task)
                    result.retried.append(task)
    for task in result.previously_completed:
        await registry.task_complete_aio(build_id, task)

    return result


# =============================================================================
# The tick
# =============================================================================


@dataclass
class TickConfig:
    """Configuration for reactive scheduler ticks.

    ``linger_seconds`` spans the many-small-tasks ↔ few-long-tasks
    spectrum: while the DAG is churning the tick stays resident (each
    action resets the linger deadline) and behaves like a tight scheduling
    loop; when only long-running work remains in flight, the tick exits and
    nothing runs until a worker (or the watchdog) wakes the scheduler.
    """

    linger_seconds: float = 120.0
    poll_interval_seconds: float = 3.0
    fail_mode: FailMode = FailMode.FAIL_FAST
    # Maps a task to the named concurrency-limit keys it runs under (see
    # the registry's environment concurrency limits). Acquisition happens
    # atomically at task start, before the spawn: a denied task simply
    # stays in the frontier — a slot-holder's completion wakes the
    # scheduler (cross-build slot releases are covered by the watchdog).
    # Per-task execution claim on the acquiring start (exactly-once across
    # builds): a start racing an already-RUNNING task is denied and the
    # task simply stays in the frontier — the existing RUNNING-probe
    # partition takes over on later passes. Degrades gracefully against
    # older servers (the claim parameter is ignored).
    claim: bool = True
    limit_key_selector: "Callable[[BaseTask], Sequence[str]] | None" = None
    # Staleness escape hatch for RUNNING tasks WITHOUT an executor ref
    # (e.g. a tick that crashed between limit-slot acquisition and spawn):
    # such a task can never resolve on its own — no worker reports it, no
    # ref can be probed — and while RUNNING it holds any concurrency-limit
    # slots it acquired, starving those keys env-wide. Tasks older than
    # this bound (by status timestamp) are failed. None disables. Note: a
    # task legitimately RUNNING via an old-version orchestrator (which
    # records no refs) in ANOTHER build could be status-failed by this —
    # its actual execution is unaffected and its completion (sticky) still
    # lands; generous default accordingly.
    stale_running_no_ref_seconds: float | None = 1800.0


class _MissingTaskRef(typing.NamedTuple):
    """Stand-in passed to lifecycle registry calls for a task whose pickle
    is missing from the build task store. Registry backends only use
    ``task.id`` to address lifecycle endpoints."""

    id: UUID


@dataclass
class TickSummary:
    """Outcome of one scheduler tick, for logging/observability."""

    outcome: str  # "not_reactive" | "lease_held" | "terminal" | "lingered_out"
    terminal_status: str | None = None
    spawned: int = 0
    self_healed: int = 0
    failed_recorded: int = 0
    cancelled_refs: int = 0
    iterations: int = 0
    limit_denied: int = 0
    claim_denied: int = 0
    skipped: int = 0


async def run_tick_aio(
    build_id: UUID,
    *,
    registry: RegistryABC,
    task_executor: TaskExecutorABC,
    lock_manager: GlobalConcurrencyLockManager,
    task_store: BuildTaskStore | None = None,
    config: TickConfig | None = None,
) -> TickSummary:
    """Run one reactive scheduler tick for ``build_id`` (see module docs).

    Idempotent and safe to invoke at any time from anywhere (worker
    wake-ups, periodic watchdog, manual): single-flighted per build via the
    scheduler lease, and a no-op for builds whose registry frontier carries
    no ``reactive_app_name`` (i.e. not reactively scheduled).
    """
    config = config or TickConfig()
    task_store = task_store or BuildTaskStore(build_id)
    summary = TickSummary(outcome="lingered_out")

    # Scheduler lease: the lock() handle auto-renews the TTL while the tick
    # lingers, and releases on exit. The manager should be configured with
    # lock_wait_timeout_seconds=None so a held lease means immediate no-op
    # (the wake-up that spawned this tick was flagged before the spawn, so
    # the lease holder's linger re-check covers it).
    lease = lock_manager.lock(scheduler_lock_name(build_id))
    async with lease:  # type: ignore[attr-defined]
        if not lease.result.acquired:
            logger.info(f"Scheduler lease for build {build_id} held; tick no-op.")
            summary.outcome = "lease_held"
            return summary

        # Expose the ambient build id (the executor forwards it to
        # self-reporting workers, exactly like the resident engine does).
        build_id_token = current_build_id_var.set(build_id)
        try:
            loop = asyncio.get_event_loop()
            deadline = loop.time() + config.linger_seconds
            while True:
                summary.iterations += 1
                try:
                    await registry.build_clear_notify_aio(build_id)
                    frontier = await registry.build_get_frontier_aio(build_id)
                except NotFoundError as e:
                    # Only a genuine missing-route 404 (server predating the
                    # frontier/notify endpoints) becomes the clear "server
                    # too old" error; a resource-level 404 (e.g. the build
                    # was deleted) is a real not-found and must propagate.
                    if not is_missing_route_error(e):
                        raise
                    raise RuntimeError(
                        "The registry server does not support reactive "
                        "scheduling (frontier/notify endpoints missing). "
                        "Upgrade stardag-api to a version matching this SDK."
                    ) from e

                if frontier.reactive_app_name is None:
                    # Not a reactively-scheduled build (e.g. a resident-
                    # orchestrator build, or the metadata was never set) —
                    # never schedule on top of it. The Modal tick wrapper
                    # short-circuits this before acquiring the lease; the
                    # check here is the backstop for direct callers. The
                    # marker never flips back to None mid-build, so it is
                    # safe to re-evaluate each iteration.
                    summary.outcome = "not_reactive"
                    return summary

                acted, denied_this_round = await _act_on_frontier(
                    frontier,
                    build_id=build_id,
                    registry=registry,
                    task_executor=task_executor,
                    task_store=task_store,
                    config=config,
                    summary=summary,
                )
                terminal = await _handle_terminal(
                    frontier,
                    build_id=build_id,
                    registry=registry,
                    task_executor=task_executor,
                    task_store=task_store,
                    config=config,
                    summary=summary,
                    denied_this_round=denied_this_round,
                )
                if terminal is not None:
                    summary.outcome = "terminal"
                    summary.terminal_status = terminal
                    return summary
                if acted:
                    # The tick's own actions (spawns recorded as started,
                    # self-healed completions, recorded failures) changed the
                    # scheduling state — re-evaluate immediately on a fresh
                    # frontier instead of waiting for an external wake-up
                    # (terminal detection above ran on the pre-action
                    # snapshot).
                    deadline = loop.time() + config.linger_seconds
                    continue

                # Linger: poll the wake-up flag until deadline.
                while True:
                    if loop.time() >= deadline:
                        return summary
                    await asyncio.sleep(config.poll_interval_seconds)
                    flag = await registry.build_get_frontier_aio(build_id)
                    if flag.needs_tick:
                        break  # outer loop clears the flag and re-acts
        finally:
            current_build_id_var.reset(build_id_token)
    return summary  # unreachable: the loop above always returns


async def _load_task(
    task_id: str,
    registry: RegistryABC,
    task_store: BuildTaskStore,
    *,
    quiet: bool = False,
) -> BaseTask | None:
    """Load a task object: store pickle first, registry rehydration second.

    The pickle-free fallback reconstructs the task from the registry's
    stored ``task_data`` (see ``stardag.task_from_registry_data``) — which
    also survives cases the pickle store can't (e.g. an app redeploy with
    compatible task definitions invalidating stored pickles). Successful
    rehydrations are written back to the store (best-effort: the task
    object is already in hand, so a transient store error must not abort
    the caller).

    With ``quiet=True`` a rehydration failure logs a single warning without
    the stack trace — for callers where a missing object is tolerated (a
    RUNNING task resolves via its worker's self-reporting), the repeated
    per-tick ``logger.exception`` would be noise.
    """
    task = task_store.load_task(task_id)
    if task is not None:
        return task
    try:
        metadata = await registry.task_get_metadata_aio(UUID(task_id))
        task = task_from_registry_data(metadata.body, expected_task_id=task_id)
    except Exception as e:
        message = (
            f"Task {task_id} is missing from the task store and could not "
            f"be rehydrated from registry data"
        )
        if quiet:
            logger.warning(f"{message}: {e}")
        else:
            logger.exception(f"{message}.")
        return None
    logger.info(f"Rehydrated task {task_id} from registry data.")
    try:
        task_store.save_task(task)
    except Exception as e:
        logger.warning(f"Failed to write rehydrated task {task_id} back: {e}")
    return task


async def _act_on_frontier(
    frontier: BuildFrontier,
    *,
    build_id: UUID,
    registry: RegistryABC,
    task_executor: TaskExecutorABC,
    task_store: BuildTaskStore,
    config: TickConfig,
    summary: TickSummary,
) -> tuple[bool, int]:
    """Spawn/probe/heal the actionable tasks.

    Returns ``(acted, denied_this_round)``: whether anything acted, and how
    many tasks were denied by concurrency limits in THIS pass (used by
    terminal detection — a cumulative count would keep suppressing the
    stuck-build check long after the denied tasks have run).
    """
    if frontier.build_status in _TERMINAL_BUILD_STATUSES:
        return False, 0  # terminal handling deals with it
    acted = False
    denied_this_round = 0
    # Hoisted signature reflection (mirrors _concurrent.py's once-per-build
    # check): whether the registry accepts the executor_metadata kwarg on
    # the two start surfaces used below.
    start_accepts_metadata = accepts_executor_metadata_kwarg(registry.task_start_aio)
    limits_start_accepts_metadata = accepts_executor_metadata_kwarg(
        registry.task_start_with_limits_aio
    )
    # Claim only through registries that implement arbitration (see the
    # resident engine's identical gate) — the ABC default would just add a
    # duplicate start. Keeps pre-claim custom registries on their old path.
    claim_active = config.claim and (
        type(registry).task_start_claim_aio is not RegistryABC.task_start_claim_aio
    )
    for item in frontier.actionable:
        task = await _load_task(
            item.task_id,
            registry,
            task_store,
            quiet=item.latest_status in _RUNNING_STATUSES,
        )
        if task is None:
            if item.latest_status in _RUNNING_STATUSES:
                # Can't probe without the object, but the worker reports its
                # own terminal events — leave it to resolve itself.
                continue
            # A pending/suspended task with no stored object AND no
            # rehydratable registry data can never be scheduled: fail it
            # (rather than leaving it in the frontier forever, where it
            # would block terminal detection and stall the build across
            # endless watchdog ticks).
            logger.error(
                f"Task {item.task_id} of build {build_id} has no stored "
                "task object and could not be rehydrated; failing it."
            )
            try:
                from uuid import UUID as _UUID

                await registry.task_fail_aio(
                    build_id,
                    typing.cast(BaseTask, _MissingTaskRef(id=_UUID(item.task_id))),
                    "Task object missing from the build task store",
                )
                summary.failed_recorded += 1
                acted = True
            except Exception as e:
                logger.error(
                    f"Failed to record store-missing failure for task "
                    f"{item.task_id}: {e}"
                )
            continue

        if item.latest_status in _RUNNING_STATUSES:
            resolution = await _resolve_running(item, task, task_executor, config)
            if resolution == "complete":
                await registry.task_complete_aio(build_id, task)
                summary.self_healed += 1
                acted = True
            elif resolution == "failed":
                # Failed executions are recorded, not respawned — retries
                # are the execution backend's job (e.g. Modal function
                # retries); a fresh attempt needs an explicit re-trigger.
                await registry.task_fail_aio(
                    build_id, task, "Detached execution failed (observed by tick)"
                )
                summary.failed_recorded += 1
                acted = True
            # "leave": still running (or unprobeable) — nothing to do.
            continue

        limit_keys: list[str] = (
            list(config.limit_key_selector(task))
            if config.limit_key_selector is not None
            else []
        )
        if limit_keys or claim_active:
            # Atomic acquiring start BEFORE spawning — the execution claim
            # (exactly-once arbitration) and any concurrency-limit slots in
            # one transaction, so a denied task never occupies a worker.
            # The acquiring TASK_STARTED carries no executor ref yet (there
            # is nothing to reference), but it does carry the executor
            # metadata when the executor can resolve it pre-spawn —
            # otherwise a UI read in the acquire→spawn window shows a
            # RUNNING task with blank executor info. The post-spawn start
            # below re-records with the ref (duplicate starts are
            # tolerated, and slots are counted per task, not per start).
            acquire_metadata: "dict[str, typing.Any] | None" = None
            if limits_start_accepts_metadata:
                try:
                    acquire_metadata = await task_executor.get_executor_metadata(task)
                except Exception:
                    logger.debug(
                        f"Executor metadata resolution failed for task "
                        f"{task.id}; acquiring without it.",
                        exc_info=True,
                    )
            if claim_active:
                claim_result = await registry.task_start_claim_aio(
                    build_id,
                    task,
                    executor_metadata=acquire_metadata,
                    limit_keys=limit_keys or None,
                )
                started = claim_result.started
                if not started:
                    if claim_result.denied_reason == "limit":
                        logger.info(
                            f"Task {task.id} denied by concurrency limits "
                            f"{limit_keys}; leaving in frontier."
                        )
                        summary.limit_denied += 1
                    else:
                        # already_running / already_completed: another
                        # scheduler won the race (or the frontier snapshot
                        # is stale) — the next frontier fetch reflects the
                        # true status and the RUNNING-probe partition takes
                        # over.
                        logger.info(
                            f"Claim for task {task.id} denied "
                            f"({claim_result.denied_reason}); leaving in "
                            "frontier."
                        )
                        summary.claim_denied += 1
                    denied_this_round += 1
                    continue
            else:
                if acquire_metadata is not None:
                    started = await registry.task_start_with_limits_aio(
                        build_id,
                        task,
                        executor_metadata=acquire_metadata,
                        limit_keys=limit_keys,
                    )
                else:
                    started = await registry.task_start_with_limits_aio(
                        build_id, task, limit_keys=limit_keys
                    )
                if not started:
                    logger.info(
                        f"Task {task.id} denied by concurrency limits "
                        f"{limit_keys}; leaving in frontier."
                    )
                    summary.limit_denied += 1
                    denied_this_round += 1
                    continue
        try:
            handle = await task_executor.submit_detached(task)
        except Exception as e:
            logger.error(f"Failed to spawn task {task.id}: {e}")
            await registry.task_fail_aio(build_id, task, f"Spawn failed: {e}")
            summary.failed_recorded += 1
            acted = True
            continue
        if handle.executor_metadata is not None and start_accepts_metadata:
            await registry.task_start_aio(
                build_id,
                task,
                executor=handle.executor,
                executor_ref=handle.ref,
                executor_metadata=handle.executor_metadata,
            )
        else:
            await registry.task_start_aio(
                build_id, task, executor=handle.executor, executor_ref=handle.ref
            )
        summary.spawned += 1
        acted = True
    return acted, denied_this_round


async def _resolve_running(
    item: "FrontierTaskRef",
    task: BaseTask,
    task_executor: TaskExecutorABC,
    config: TickConfig,
) -> str:
    """Decide what to do with a RUNNING task: leave/complete/failed.

    Self-heal precedence: the target is the ground truth — if it exists the
    task is complete regardless of what happened to the execution (e.g. the
    worker wrote the output, then died before reporting).
    """
    executor_name, ref = item.latest_executor, item.latest_executor_ref
    if await task.complete_aio():
        return "complete"
    if executor_name is None or ref is None:
        # RUNNING with no ref: can't probe, no worker will report. Fresh
        # occurrences are left alone (could be the spawn-in-progress window
        # of a live tick, or an old-version orchestrator's task), but past
        # the staleness bound the task is failed — otherwise it never
        # resolves and holds any concurrency-limit slots forever.
        stale_after = config.stale_running_no_ref_seconds
        if (
            stale_after is not None
            and item.latest_status_at is not None
            and (datetime.now(timezone.utc) - item.latest_status_at).total_seconds()
            > stale_after
        ):
            logger.error(
                f"Task {task.id} has been RUNNING without an executor ref "
                f"for over {stale_after:.0f}s; failing it (stale — likely a "
                "scheduler crash between slot acquisition and spawn)."
            )
            return "failed"
        logger.warning(
            f"Task {task.id} is RUNNING without an executor ref; leaving it."
        )
        return "leave"
    status = await task_executor.detached_status(task, executor_name, ref)
    if status == DetachedExecutionStatus.RUNNING:
        return "leave"
    if status == DetachedExecutionStatus.SUCCEEDED:
        # Finished successfully but target check above said incomplete —
        # eventual consistency; treat as complete (worker wrote it).
        return "complete"
    if status == DetachedExecutionStatus.FAILED:
        return "failed"
    # UNKNOWN: possibly still running somewhere we can't see — leave rather
    # than risk a duplicate execution. The watchdog re-probes periodically.
    logger.warning(
        f"Detached execution {ref!r} for task {task.id} has unknown status; "
        "leaving it (watchdog will re-check)."
    )
    return "leave"


async def _handle_terminal(
    frontier: BuildFrontier,
    *,
    build_id: UUID,
    registry: RegistryABC,
    task_executor: TaskExecutorABC,
    task_store: BuildTaskStore,
    config: TickConfig,
    summary: TickSummary,
    denied_this_round: int = 0,
) -> str | None:
    """Evaluate terminal conditions; emit build events. Returns terminal status."""
    if frontier.build_status in _TERMINAL_BUILD_STATUSES:
        if frontier.build_status == "cancelled":
            # Cancelled externally (e.g. UI): stop the running work.
            await _cancel_running(
                frontier, build_id, registry, task_executor, task_store, summary
            )
        return frontier.build_status

    counts = frontier.status_counts
    running = sum(counts.get(status, 0) for status in _RUNNING_STATUSES)
    failed = counts.get("failed", 0)

    if failed > 0 and config.fail_mode == FailMode.FAIL_FAST:
        await _cancel_running(
            frontier, build_id, registry, task_executor, task_store, summary
        )
        await _skip_blocked(registry, build_id, summary)
        await registry.build_fail_aio(
            build_id, f"{failed} task(s) failed (fail_mode=FAIL_FAST)"
        )
        return "failed"

    roots_known = len(frontier.roots) == len(frontier.root_task_ids) > 0
    if roots_known and all(r.latest_status == "completed" for r in frontier.roots):
        await registry.build_complete_aio(build_id)
        return "completed"

    if denied_this_round > 0:
        # Tasks denied by concurrency limits in THIS pass are waiting for
        # slots held possibly by OTHER builds (running == 0 here doesn't
        # mean the env is idle) — never declare the build stuck. Scoped to
        # the current pass: a cumulative count would keep suppressing the
        # stuck check long after the denied tasks have run. The watchdog
        # re-ticks periodically; same-build slot releases notify directly.
        return None

    # Note: spawns within this iteration imply frontier.actionable was
    # non-empty, so this check can't misfire on the pre-spawn snapshot.
    if not frontier.actionable and running == 0:
        # Nothing runnable and nothing running: the build can't progress
        # (failed deps in CONTINUE mode, or a lost task pickle). Fail
        # rather than idle forever.
        await _skip_blocked(registry, build_id, summary)
        await registry.build_fail_aio(
            build_id,
            "No runnable or running tasks left but roots are not complete "
            f"(status counts: {counts})",
        )
        return "failed"

    return None


async def _skip_blocked(
    registry: RegistryABC, build_id: UUID, summary: TickSummary
) -> None:
    """Mark tasks transitively blocked by failures as skipped (best-effort).

    Cosmetic-but-important: without it, blocked tasks dangle PENDING in the
    registry/UI forever while the build shows failed. Old servers without
    the endpoint are tolerated (missing-route 404 → skip silently omitted);
    app-level 404s (e.g. the build no longer exists) are re-raised — they
    signal a registry inconsistency the tick must not paper over.
    """
    try:
        skipped = await registry.build_skip_blocked_aio(build_id)
        summary.skipped += len(skipped)
    except NotFoundError as e:
        if not _is_route_not_found(e):
            raise
        logger.warning(
            "Registry server does not support skip-blocked; tasks blocked "
            "by the failure will remain pending."
        )
    except Exception as e:
        logger.warning(f"Failed to skip blocked tasks for build {build_id}: {e}")


async def _cancel_running(
    frontier: BuildFrontier,
    build_id: UUID,
    registry: RegistryABC,
    task_executor: TaskExecutorABC,
    task_store: BuildTaskStore,
    summary: TickSummary,
) -> None:
    """Best-effort cancel of all running detached executions in the build.

    Uses the frontier's full ``running`` list — a RUNNING task inside the
    dynamic-dep registration window drops out of ``actionable`` but must
    still be cancelled. Falls back to ``actionable`` for servers predating
    the field.

    Each successfully cancelled execution is also recorded as
    TASK_CANCELLED (best-effort): a worker killed by the executor's cancel
    can't reliably self-report, and without the event the task dangles
    RUNNING — keeping its pending descendants out of the skip-blocked
    closure (cancelled is a seed status) and holding any concurrency-limit
    slots forever.
    """
    running_items = frontier.running or [
        item for item in frontier.actionable if item.latest_status in _RUNNING_STATUSES
    ]
    for item in running_items:
        if (
            item.latest_status in _RUNNING_STATUSES
            and item.latest_executor is not None
            and item.latest_executor_ref is not None
        ):
            task = await _load_task(item.task_id, registry, task_store, quiet=True)
            if task is None:
                continue
            try:
                await task_executor.cancel_detached(
                    task, item.latest_executor, item.latest_executor_ref
                )
                summary.cancelled_refs += 1
            except Exception as e:
                logger.warning(
                    f"Failed to cancel detached execution "
                    f"{item.latest_executor_ref!r} for task {item.task_id}: {e}"
                )
                continue
            try:
                await registry.task_cancel_aio(build_id, task)
            except Exception as e:
                logger.warning(
                    f"Failed to record cancellation of task {item.task_id}: {e}"
                )
