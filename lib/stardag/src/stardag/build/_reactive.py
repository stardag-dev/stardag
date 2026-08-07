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
  the trigger (initial discovery) and by workers (dynamic deps) — or, when
  the pickle is absent, reconstructed from the registry's stored task data.
  The latter resolves only *registered* classes, i.e. classes whose
  defining module the tick process has imported; see
  ``stardag.build._task_modules`` for how an app declares those.
- The global concurrency lock and build-local ``ConcurrencyConfig`` limits
  are not applied by ticks (infra-level limits, e.g. Modal per-function
  ``concurrency_limit``, still apply). Registry-backed named limits *are*
  applied, via ``TickConfig.limit_key_selector``.
- On failure (FAIL_FAST) the build is failed, running executions are
  cancelled, and blocked descendants are marked SKIPPED (server-computed;
  older servers without the skip-blocked endpoint are tolerated).

A build with nothing actionable and nothing running is *not* automatically
stuck. Task rows and dependency edges are per environment, so an upstream
that some other build left non-COMPLETED gates this build's tasks while
contributing nothing to the counts this build can see. Terminal detection
therefore consults the frontier's ``blocked_by_external`` before declaring
a build dead: a blocker RUNNING under another build is waited on (bounded
by ``TickConfig.stale_external_blocker_seconds``), while a blocker nobody
is executing fails the build immediately with a message naming the task
and the build that owns it. Against servers predating those fields the
list is always empty and detection degrades to its pre-fix behaviour.
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
from stardag.build._task_modules import import_failure_note
from stardag.build._task_store import BuildTaskStore
from stardag.exceptions import NotFoundError, is_missing_route_error
from stardag.registry._api_registry import _is_route_not_found
from stardag.registry import (
    BuildFrontier,
    FrontierExternalBlocker,
    FrontierTaskRef,
    RegistryABC,
)
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
    # Tasks whose failed/cancelled/skipped/suspended registry status was
    # reset to pending (only when retry_failed=True).
    retried: list[BaseTask] = field(default_factory=list)


# Statuses a re-trigger resets to PENDING (see discover_and_register_aio's
# retry_failed). Mirrors the registry's own retryable set.
#
# SUSPENDED is in here — and is safe — because a suspended task has NO live
# execution: suspension means the execution registered its dynamic
# dependencies, yielded and *returned*. Resetting it therefore cannot orphan
# a running worker; re-running from scratch is exactly what the retry
# expresses. RUNNING is deliberately absent: it holds a live execution
# claim, and releasing that claim is cancellation, not retry.
#
# Only the re-trigger path passes retry_failed=True. Workers registering
# dynamically yielded deps call discover_and_register_aio with the default
# (False), so a worker can never reset its own parent's SUSPENDED status
# out from under itself.
_RETRYABLE_STATUSES = ("failed", "cancelled", "skipped", "suspended")


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
    failed/cancelled/skipped/suspended (from a previous build) are reset to
    pending via ``task_retry`` — without it, a previously failed task would
    never enter the frontier and would FAIL_FAST a new build on its first
    tick, and a task abandoned SUSPENDED (its orchestrator died, or its
    build was cancelled mid dynamic-dependency yield) would stay
    permanently unschedulable. See ``_RETRYABLE_STATUSES`` for why
    resetting a SUSPENDED task cannot orphan a live execution.
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
    # How long this build will wait on a blocker that is RUNNING under
    # ANOTHER build before giving up and failing (see
    # ``_classify_external_blockers``). Measured on the blocker's status
    # timestamp — how long it has been RUNNING — not on tick-local time: a
    # tick is short-lived and the watchdog keeps re-ticking, so a tick-local
    # bound would never expire and the build would hang silently forever,
    # which is the failure mode this whole path exists to remove.
    #
    # Deliberately generous (6 h). Waiting is the cheap direction: the
    # blocker's own build has far better information about it (a live
    # executor ref it can probe) and its completion wakes this build within
    # a watchdog period, whereas failing a build whose blocker was merely
    # slow is a regression of exactly the spurious-failure bug being fixed
    # here. The bound is a backstop for a claim nobody will ever release —
    # an abandoned RUNNING task whose owning build is gone — not a
    # scheduling deadline. Raise it if your tasks routinely run longer than
    # this; None waits indefinitely (pre-fix "hangs quietly" behaviour, with
    # a per-tick warning naming the blocker).
    #
    # A blocker whose ``blocking_status_at`` is None (a task row predating
    # server-side status denormalisation) cannot be aged, so it is waited on
    # regardless of this bound — failing on missing information would
    # reintroduce the spurious failures. The wait is logged as unbounded.
    stale_external_blocker_seconds: float | None = 21600.0


class _MissingTaskRef(typing.NamedTuple):
    """Stand-in passed to lifecycle registry calls for a task whose pickle
    is missing from the build task store. Registry backends only use
    ``task.id`` to address lifecycle endpoints."""

    id: UUID


@dataclass
class TickSummary:
    """Outcome of one scheduler tick, for logging/observability.

    Flat and JSON-friendly on purpose: the whole dataclass is serialized
    with ``dataclasses.asdict`` and reported to the registry.
    """

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
    # Cross-build blocking, summed over the terminal evaluations of this
    # tick (like limit_denied, these are counts of observations, not of
    # distinct tasks — one blocker seen on three linger passes counts
    # three times).
    #
    # ``external_blockers`` is every entry the frontier reported while the
    # build looked stalled, including in-build ones; ``waited`` and
    # ``fatal`` split only the out-of-build entries, so the three do not
    # add up. A tick with waited > 0 and fatal == 0 is the healthy
    # "waiting on another build" state; fatal > 0 always accompanies a
    # failed build.
    external_blockers: int = 0
    external_blockers_waited: int = 0
    external_blockers_fatal: int = 0


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

    A rehydration failure is annotated with any declared task modules that
    failed to import in this process (see
    ``stardag.build._task_modules``): "no task class registered for X" and
    "the module defining X blew up on import" are the same incident seen
    from two ends, and only the annotation connects them. The annotation is
    read from the task-module registry rather than plumbed through
    ``rehydrate.py``, which stays a pure reconstruction primitive with no
    notion of how its classes got imported.
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
        note = import_failure_note()
        if quiet:
            logger.warning(f"{message}: {e}{note}")
        else:
            logger.exception(f"{message}.{note}")
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
    # The claim method's signature always accepts executor_metadata; only the
    # legacy limits-start path needs reflection (pre-metadata custom
    # registries).
    acquiring_start_accepts_metadata = claim_active or limits_start_accepts_metadata
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
            if acquiring_start_accepts_metadata:
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


class _ExternalBlockers(typing.NamedTuple):
    """Partition of a frontier's ``blocked_by_external`` (see below)."""

    # Out-of-build and RUNNING within the staleness bound: someone else is
    # executing it, and their completion frees this build. Wait.
    waiting: list[FrontierExternalBlocker]
    # Out-of-build and NOT going to run — either not RUNNING at all, or
    # RUNNING for longer than ``stale_external_blocker_seconds``. Fail.
    fatal: list[FrontierExternalBlocker]
    # In this build's own task set: already accounted for by actionable /
    # running / status_counts, so it must not influence the wait-or-fail
    # decision. Kept only to enrich the message.
    in_build: list[FrontierExternalBlocker]


def _blocker_label(blocker: FrontierExternalBlocker) -> str:
    """``namespace.Name`` of a blocker (namespace is "" by default)."""
    if blocker.blocking_task_namespace:
        return f"{blocker.blocking_task_namespace}.{blocker.blocking_task_name}"
    return blocker.blocking_task_name


def _blocker_status_age_seconds(
    blocker: FrontierExternalBlocker, now: datetime
) -> float | None:
    """Seconds the blocker has been in its current status, None if unknown.

    Naive timestamps (a custom registry that drops the offset) are read as
    UTC rather than raising: this runs on the path that decides whether to
    fail a build, so a formatting quirk must not become a tick crash.
    """
    if blocker.blocking_status_at is None:
        return None
    status_at = blocker.blocking_status_at
    if status_at.tzinfo is None:
        status_at = status_at.replace(tzinfo=timezone.utc)
    return (now - status_at).total_seconds()


def _classify_external_blockers(
    frontier: BuildFrontier, config: TickConfig, now: datetime
) -> _ExternalBlockers:
    """Split the frontier's external blockers into wait / fail / ignore.

    The frontier reports these only when the build has nothing actionable
    and nothing running — precisely the state the stuck-build check reads
    as "this build is dead". The split decides whether it actually is:

    - ``blocking_in_build`` → ignored. The blocker is in this build's own
      task set, so it is already visible in ``actionable``/``running``/
      ``status_counts``; letting it also drive this decision would
      double-count it (and a blocker that is genuinely stuck *in* this
      build must still fail the build, exactly as before).
    - out-of-build and RUNNING → **wait**. Another build is executing it;
      its completion unblocks this build and wakes this scheduler. Treated
      like a concurrency-limit denial: return, don't fail. Bounded by
      ``config.stale_external_blocker_seconds`` against the blocker's own
      status timestamp, since a tick is too short-lived to bound anything
      itself — see that field for the rationale and the None cases.
    - out-of-build and anything else (pending/suspended/failed/cancelled/
      skipped) → **fail now**. Nobody is executing it and this build will
      never schedule it, so waiting would be waiting forever. (Narrow
      exception this deliberately does not carve out: a PENDING blocker
      belonging to another *live* build will be scheduled by that build.
      It is rare — a build's discovery registers the whole static
      dependency tree, so out-of-build blockers are dynamic dependencies
      of an earlier build — and the failure now says exactly which task to
      retry, unlike the silent hang it replaces.)
    """
    waiting: list[FrontierExternalBlocker] = []
    fatal: list[FrontierExternalBlocker] = []
    in_build: list[FrontierExternalBlocker] = []
    stale_after = config.stale_external_blocker_seconds
    for blocker in frontier.blocked_by_external:
        if blocker.blocking_in_build:
            in_build.append(blocker)
            continue
        if blocker.blocking_status not in _RUNNING_STATUSES:
            fatal.append(blocker)
            continue
        age = _blocker_status_age_seconds(blocker, now)
        if stale_after is not None and age is not None and age > stale_after:
            fatal.append(blocker)
        else:
            waiting.append(blocker)
    return _ExternalBlockers(waiting=waiting, fatal=fatal, in_build=in_build)


# How many blockers a log line or build error names before summarising the
# rest. The server already caps its list (hence blocked_by_external_
# truncated); this second cap keeps a build's error_message readable when a
# wide DAG is stalled behind a single upstream.
_MAX_REPORTED_BLOCKERS = 5


def _describe_blockers(
    blockers: Sequence[FrontierExternalBlocker], now: datetime, truncated: bool
) -> str:
    """One-line, user-actionable rendering of blockers (names, not ids only).

    ``truncated`` is the frontier's ``blocked_by_external_truncated``: the
    server capped its list, so this must not read as an exhaustive account.
    """
    described = "; ".join(
        (
            f"task {blocker.task_id} is blocked by {_blocker_label(blocker)} "
            f"({blocker.blocking_task_id}), {blocker.blocking_status.upper()}"
            + (
                ""
                if (age := _blocker_status_age_seconds(blocker, now)) is None
                else f" for {age:.0f}s"
            )
            + (
                f" under build {blocker.blocking_status_build_id}"
                if blocker.blocking_status_build_id is not None
                else " under an unrecorded build"
            )
        )
        for blocker in blockers[:_MAX_REPORTED_BLOCKERS]
    )
    remaining = len(blockers) - _MAX_REPORTED_BLOCKERS
    if remaining > 0:
        described += f"; and {remaining} more"
    if truncated:
        described += (
            "; the registry capped the blocker list, so there may be further "
            "blockers not shown"
        )
    return described


def _blocker_remediation(blockers: Sequence[FrontierExternalBlocker]) -> str:
    """How to get out of it — the part #208 says today's error lacks."""
    remedy = (
        "Retry the blocking task under the build that owns it "
        "(POST /api/v1/builds/{build_id}/tasks/{task_id}/retry) to reset it "
        "to pending — this now works from suspended as well as from "
        "failed/cancelled/skipped — then re-trigger this build"
    )
    if any(blocker.blocking_status in _RUNNING_STATUSES for blocker in blockers):
        remedy += (
            ". A blocker stuck RUNNING holds an execution claim no retry can "
            "take: cancel it (POST /api/v1/builds/{build_id}/tasks/{task_id}"
            "/cancel) to release the claim first"
        )
    return remedy + "."


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
    """Evaluate terminal conditions; emit build events. Returns terminal status.

    Returning ``None`` means "not terminal — keep waiting", which covers
    both a build with work in flight and a build with nothing of its own to
    do that is legitimately waiting on another build (see
    :func:`_classify_external_blockers`).
    """
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
        # Nothing runnable and nothing *in this build* running. That is not
        # the same as "the build can't progress": dependency gating is
        # environment-global while these counts are build-scoped, so a task
        # some other build is executing gates this build's tasks while
        # showing up in neither. Ask the frontier which it is before
        # declaring the build dead (#208 A1) — the list is populated only in
        # this exact state, and is empty against servers predating it, in
        # which case everything below degrades to the old unconditional
        # failure.
        now = datetime.now(timezone.utc)
        truncated = frontier.blocked_by_external_truncated
        blockers = _classify_external_blockers(frontier, config, now)
        summary.external_blockers += len(frontier.blocked_by_external)
        summary.external_blockers_waited += len(blockers.waiting)
        summary.external_blockers_fatal += len(blockers.fatal)

        if blockers.waiting and not blockers.fatal:
            # Waiting on work someone else is doing — the same call the
            # denied_this_round branch above makes, and for the same reason:
            # running == 0 here does not mean the environment is idle. The
            # blocker's completion wakes this scheduler; a lost wake-up is
            # covered by the watchdog. Logged every pass on purpose: a build
            # that sits here needs to be diagnosable from the tick logs
            # alone.
            if config.stale_external_blocker_seconds is None:
                bound_note = " (staleness bound disabled — this wait is unbounded)"
            elif any(
                blocker.blocking_status_at is None for blocker in blockers.waiting
            ):
                bound_note = (
                    " (a blocker carries no status timestamp, so its wait "
                    "cannot be bounded)"
                )
            else:
                bound_note = ""
            logger.info(
                f"Build {build_id} has nothing to schedule but is blocked by "
                f"{len(blockers.waiting)} task(s) running in other build(s); "
                f"waiting rather than failing{bound_note}: "
                f"{_describe_blockers(blockers.waiting, now, truncated)}"
            )
            return None

        await _skip_blocked(registry, build_id, summary)
        if blockers.fatal:
            # Precise, actionable failure: which task, its name, its status,
            # how long it has been in it, and which build owns it — plus how
            # to release it. The status counts alone (the whole of the old
            # message) point nowhere near the cause when the cause is not in
            # this build's task set at all.
            reason = (
                f"Build is blocked by {len(blockers.fatal)} task(s) owned by "
                "another build that nobody is executing, and has nothing "
                f"runnable or running of its own (status counts: {counts}). "
                f"Blocked by: {_describe_blockers(blockers.fatal, now, truncated)}"
                f". {_blocker_remediation(blockers.fatal)}"
            )
        else:
            # No out-of-build blocker: genuinely stuck (failed deps in
            # CONTINUE mode, a lost task pickle, or a blocker inside this
            # build that will never run). Fail rather than idle forever —
            # naming any in-build blockers, which are otherwise invisible in
            # the status counts.
            reason = (
                "No runnable or running tasks left but roots are not "
                f"complete (status counts: {counts})"
            )
            if blockers.in_build:
                reason += ". Blocked within this build by: " + _describe_blockers(
                    blockers.in_build, now, truncated
                )
        logger.error(f"Failing build {build_id}: {reason}")
        await registry.build_fail_aio(build_id, reason)
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
