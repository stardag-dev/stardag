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
a build dead. For a RUNNING blocker the answer is *read*, not inferred:
the execution claim carries an expiry, so a live claim means wait and a
lapsed one means fail. For every other blocker status no claim is held and
no expiry exists, so the question "is anyone going to move it?" is put to
the build that owns the blocker's status. Either way a fatal blocker fails
the build with a message naming the task, the build that owns it and why
that owner will not move it. Against servers predating those fields the
list is always empty and detection degrades to its pre-fix behaviour.

Every start this tick records carries a claim TTL derived from the
executor's own timeout (see :func:`claim_ttl_seconds`), so the expiry other
schedulers read is tied to the moment the execution is actually killed
rather than to a generic server-side default.

Each tick reports its :class:`TickSummary` to the registry on the way out
(``TickConfig.report_tick_summaries``), so the scheduler's own account of
what it did survives the container it ran in — a build driven by dozens of
short-lived ticks otherwise leaves its reasoning scattered across as many
logs. A tick that crashes is reported too, as ``outcome="error"``. Strictly
best-effort throughout: it never fails a tick, never changes an outcome,
never masks an exception, and tolerates a server without the endpoint.
"""

from __future__ import annotations

import asyncio
import logging
import typing
from dataclasses import asdict, dataclass, field
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
from stardag.registry import (
    BuildFrontier,
    FrontierExternalBlocker,
    FrontierTaskRef,
    RegistryABC,
)

logger = logging.getLogger(__name__)

SCHEDULER_LOCK_PREFIX = "__scheduler__:"

# Statuses considered "in flight" for terminal detection.
_RUNNING_STATUSES = ("running",)
_TERMINAL_BUILD_STATUSES = ("completed", "failed", "cancelled")

# Slack added to an executor's own timeout when deriving a claim TTL. It
# covers the ways the claim's clock and the execution's clock differ: the
# claim is recorded at *acquire* time, BEFORE the spawn, so it absorbs
# queueing and cold-start latency the timeout clock has not started
# counting yet; a backend does not kill an execution the instant its
# timeout elapses; and client and server clocks are not identical.
#
# Deliberately generous, because the two errors are not symmetric: too
# short and a live execution's claim becomes stealable (a duplicate
# execution, which is what claims exist to prevent); too long and an
# abandoned claim merely heals later than it could have.
_CLAIM_TTL_GRACE_SECONDS = 900.0

# The server's accepted range for claim_ttl_seconds (outside it: 422).
# Clamped rather than raised on — a 30-second task and a 60-day task are
# both legitimate, and each should get the closest expiry the server can
# express rather than a failed start.
_MIN_CLAIM_TTL_SECONDS = 60
_MAX_CLAIM_TTL_SECONDS = 2592000  # 30 days


def claim_ttl_seconds(task: BaseTask, task_executor: TaskExecutorABC) -> int | None:
    """Claim TTL for ``task``, derived from the executor's own timeout.

    Returns None when the executor exposes no timeout for the task, which
    leaves the expiry to the registry's default.

    **Why derive it rather than take the default.** Putting an expiry on
    *every* start is what maximises healing: an execution claim with no
    expiry records no liveness evidence a third party can evaluate, which
    is precisely the gap that makes an abandoned RUNNING task wedge every
    other build downstream of it. But an expiry also makes a task that
    outlives its TTL *stealable while it is still alive* — a duplicate
    execution, i.e. the failure mode claims exist to prevent.

    Deriving the TTL from the backend's own wall-clock limit is what keeps
    that from being a real risk: the backend will have killed the execution
    before its claim lapses, so the executions whose claims can lapse are
    the ones that are already dead. A generic server-side default cannot
    make that promise for a task whose timeout it does not know — which is
    why "the server has a default" is not a reason to omit this.

    The executor is asked, never guessed at: an executor that enforces no
    limit says so by returning None, and a resolution failure is swallowed
    (this runs on the spawn path of every task and must not fail a start
    over a diagnostic).
    """
    try:
        timeout = task_executor.execution_timeout_seconds(task)
    except Exception:
        logger.debug(
            f"Execution timeout resolution failed for task {task.id}; "
            "claiming with the registry's default TTL.",
            exc_info=True,
        )
        return None
    if timeout is None:
        return None
    ttl = int(timeout + _CLAIM_TTL_GRACE_SECONDS)
    return max(_MIN_CLAIM_TTL_SECONDS, min(_MAX_CLAIM_TTL_SECONDS, ttl))


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
    limit_key_selector: "Callable[[BaseTask], Sequence[str]] | None" = None
    # Report each tick's :class:`TickSummary` to the registry, so "why is
    # this build not progressing?" is answerable without reading logs
    # across many short-lived tick containers. Strictly best-effort: a
    # reporting failure never fails a tick or changes its outcome.
    #
    # Deployed-app configuration, NOT a per-trigger tick_kwarg (see
    # ``_TICK_KWARGS_ALLOWED``): the reasons to turn this off — a registry
    # without the endpoint, or a deployment that keeps its observability
    # elsewhere — are properties of the whole deployment, never of one
    # build. Widening the allowlist later is additive; narrowing it is not.
    report_tick_summaries: bool = True


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

    # "not_reactive" | "lease_held" | "terminal" | "lingered_out" | "error"
    outcome: str
    terminal_status: str | None = None
    # Set only for outcome == "error": the exception that ended the tick.
    # A crashed tick is the single most informative thing a "why did this
    # build stall?" query can find, so it is recorded like any other
    # outcome — but the exception itself is never masked or replaced (see
    # ``run_tick_aio``). The message is bounded so a pathological
    # traceback-in-a-message cannot push the summary past the server's
    # size cap and turn a recorded failure into an unrecorded one.
    error_type: str | None = None
    error_message: str | None = None
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


# Outcomes worth persisting. ``not_reactive`` is excluded by definition:
# the build is not reactively scheduled, so the tick is a stray that did
# nothing and learnt nothing — pure noise in a finite retention window.
# Everything else says something: ``lease_held`` is contention (many of
# them means ticks are piling up on one build), ``lingered_out`` is a tick
# that found nothing to do, ``terminal`` carries the outcome and the
# counters explaining it, and ``error`` is a tick that crashed — the most
# informative of the lot.
_UNREPORTED_TICK_OUTCOMES = frozenset({"not_reactive"})

# Bounds on the recorded exception identity/text. The server caps a summary
# at 8 KiB of compact JSON and rejects anything larger, so an unbounded
# message — a chained traceback, a repr of a huge payload — would turn "this
# tick crashed" into no record at all, losing exactly the outcome most worth
# keeping. Generous enough that a real error message survives intact.
_MAX_ERROR_TYPE_CHARS = 128
_MAX_ERROR_MESSAGE_CHARS = 1024
_TRUNCATION_MARKER = "… [truncated]"


def _bounded(text: str, limit: int) -> str:
    """Clip ``text`` to ``limit`` characters, marking that it was clipped."""
    if len(text) <= limit:
        return text
    return text[: limit - len(_TRUNCATION_MARKER)] + _TRUNCATION_MARKER


# Set once per process when the registry answers the tick-summary route
# with a missing-route 404, so an SDK pointed at an older server stops
# paying for a doomed request on every tick. Process-global rather than
# per-registry because a process talks to one registry, and the cost of
# being wrong is one extra request in the (nonexistent) other case.
_tick_summary_route_missing = False


async def _report_tick_summary(
    build_id: UUID,
    registry: RegistryABC,
    config: TickConfig,
    summary: TickSummary,
) -> None:
    """Report a finished tick's summary to the registry. Best-effort.

    "Best-effort" is a contract, not a hope: this runs at the end of every
    tick — a hot path — and recording observability must never fail a tick
    or change its outcome. Hence a bare ``except Exception``, which is the
    correct breadth here precisely because *nothing* this call can raise
    is worth propagating to a scheduler.
    """
    global _tick_summary_route_missing
    if not config.report_tick_summaries:
        return
    if summary.outcome in _UNREPORTED_TICK_OUTCOMES:
        return
    if _tick_summary_route_missing:
        return
    try:
        await registry.build_report_tick_summary_aio(build_id, asdict(summary))
    except NotFoundError as e:
        # Same tolerance as ``_skip_blocked``: a server predating the
        # endpoint answers with FastAPI's generic missing-route 404, which
        # is version skew rather than an error. A resource-level 404 (the
        # build is gone) is also not worth escalating from here — the tick
        # already ran — but it is not a reason to disable reporting for
        # every other build in the process.
        if is_missing_route_error(e):
            _tick_summary_route_missing = True
            logger.debug(
                "Registry API does not support tick summaries; reporting "
                "disabled for this process. Upgrade stardag-api to see "
                "per-tick scheduler reasoning in the registry."
            )
        else:
            logger.debug("Tick summary for build %s not recorded: %s", build_id, e)
    except Exception as e:
        logger.warning(
            "Failed to report tick summary for build %s (ignored): %s",
            build_id,
            e,
        )


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

    The tick's summary is reported to the registry on the way out. The
    scheduling itself lives in ``_run_tick_body_aio``, which mutates the
    summary in place and has several early returns — wrapping it here is
    what keeps the report to exactly one call site instead of one per
    return, which is how a return added later would silently stop being
    observable.

    A tick that *raises* is reported too, as ``outcome="error"`` carrying
    the exception's type and (bounded) message: a crashed tick is the most
    informative answer a "why did this build stall?" query can get, and
    the summary is stored as an open blob so the extra fields need no
    server change. Reporting it never changes what the caller sees — the
    original exception is re-raised unconditionally, and a failure to
    record the failure is logged and swallowed, exactly like the
    success-path report.
    """
    config = config or TickConfig()
    task_store = task_store or BuildTaskStore(build_id)
    summary = TickSummary(outcome="lingered_out")
    try:
        await _run_tick_body_aio(
            build_id,
            registry=registry,
            task_executor=task_executor,
            lock_manager=lock_manager,
            task_store=task_store,
            config=config,
            summary=summary,
        )
    except Exception as e:
        summary.outcome = "error"
        summary.error_type = _bounded(type(e).__name__, _MAX_ERROR_TYPE_CHARS)
        summary.error_message = _bounded(str(e), _MAX_ERROR_MESSAGE_CHARS)
        # Best-effort, and cannot mask: _report_tick_summary swallows
        # everything it can raise. The bare `raise` re-raises the original
        # with its traceback intact.
        await _report_tick_summary(build_id, registry, config, summary)
        raise
    await _report_tick_summary(build_id, registry, config, summary)
    return summary


async def _run_tick_body_aio(
    build_id: UUID,
    *,
    registry: RegistryABC,
    task_executor: TaskExecutorABC,
    lock_manager: GlobalConcurrencyLockManager,
    task_store: BuildTaskStore,
    config: TickConfig,
    summary: TickSummary,
) -> None:
    """The tick proper — see :func:`run_tick_aio`. Mutates ``summary``."""
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
            return

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
                    return

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
                    return
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
                        return
                    await asyncio.sleep(config.poll_interval_seconds)
                    flag = await registry.build_get_frontier_aio(build_id)
                    if flag.needs_tick:
                        break  # outer loop clears the flag and re-acts
        finally:
            current_build_id_var.reset(build_id_token)
    return  # unreachable: the loop above always returns


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
            resolution = await _resolve_running(item, task, task_executor)
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
        # Atomic claiming start BEFORE spawning — the execution claim
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
        try:
            acquire_metadata = await task_executor.get_executor_metadata(task)
        except Exception:
            logger.debug(
                f"Executor metadata resolution failed for task "
                f"{task.id}; acquiring without it.",
                exc_info=True,
            )
        # Both starts below carry the same derived TTL. The post-spawn one
        # needs it as much as the claiming one: the TTL applies to every
        # start, so omitting it there would hand the claim straight back to
        # the registry's generic default and undo the derivation.
        ttl_seconds = claim_ttl_seconds(task, task_executor)
        claim_result = await registry.task_start_claim_aio(
            build_id,
            task,
            executor_metadata=acquire_metadata,
            limit_keys=limit_keys or None,
            claim_ttl_seconds=ttl_seconds,
        )
        if not claim_result.started:
            if claim_result.denied_reason == "limit":
                logger.info(
                    f"Task {task.id} denied by concurrency limits "
                    f"{limit_keys}; leaving in frontier."
                )
                summary.limit_denied += 1
            else:
                # already_running / already_completed: another scheduler
                # won the race (or the frontier snapshot is stale) — the
                # next frontier fetch reflects the true status and the
                # RUNNING-probe partition takes over.
                logger.info(
                    f"Claim for task {task.id} denied "
                    f"({claim_result.denied_reason}); leaving in frontier."
                )
                summary.claim_denied += 1
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
        await registry.task_start_aio(
            build_id,
            task,
            executor=handle.executor,
            executor_ref=handle.ref,
            executor_metadata=handle.executor_metadata,
            claim_ttl_seconds=ttl_seconds,
        )
        summary.spawned += 1
        acted = True
    return acted, denied_this_round


def _claim_has_lapsed(expires_at: datetime | None, now: datetime) -> bool:
    """Whether an execution claim is past its own expiry.

    None — "never lapses", i.e. an older server, a start recorded before
    the column existed, or a caller that asked for no expiry — is False.
    Absence of evidence is not evidence of death, and every caller here is
    deciding whether to kill something.

    Naive timestamps (a custom registry that drops the offset) are read as
    UTC rather than raising: this runs on paths that decide a task's or a
    build's fate, so a formatting quirk must not become a tick crash.
    """
    if expires_at is None:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return now > expires_at


async def _resolve_running(
    item: "FrontierTaskRef",
    task: BaseTask,
    task_executor: TaskExecutorABC,
) -> str:
    """Decide what to do with a RUNNING task: leave/complete/failed.

    Self-heal precedence: the target is the ground truth — if it exists the
    task is complete regardless of what happened to the execution (e.g. the
    worker wrote the output, then died before reporting). Then the executor
    is asked. A claim expiry is only a *floor* on liveness; the backend's
    own answer about the execution is the truth, so probing keeps
    precedence over anything the expiry says wherever a ref exists.
    """
    executor_name, ref = item.latest_executor, item.latest_executor_ref
    if await task.complete_aio():
        return "complete"
    if executor_name is None or ref is None:
        # RUNNING with no ref: nothing to probe, and no worker to report it.
        # The window is still reachable — the claiming start is recorded
        # BEFORE the spawn, so a tick that dies in between leaves exactly
        # this shape — and it does still need handling: while RUNNING the
        # task holds any concurrency-limit slots it acquired, starving those
        # keys environment-wide.
        #
        # What it no longer needs is a locally configured guess at how long
        # is too long. The claim carries its own expiry; past it the claim
        # is not honoured by anyone, which is the fact the old bound was
        # approximating. Lapsed → fail. Otherwise leave it: the
        # spawn-in-progress window of a perfectly healthy tick looks
        # identical from here, and failing that would kill a task that is
        # about to start.
        if _claim_has_lapsed(item.latest_status_expires_at, datetime.now(timezone.utc)):
            logger.error(
                f"Task {task.id} is RUNNING without an executor ref and its "
                f"execution claim lapsed at {item.latest_status_expires_at}; "
                "failing it (nothing can probe it and no worker will report "
                "it — most likely a scheduler crash between the claiming "
                "start and the spawn)."
            )
            return "failed"
        logger.warning(
            f"Task {task.id} is RUNNING without an executor ref; leaving it"
            + (
                " (its claim has not lapsed)."
                if item.latest_status_expires_at is not None
                else " (its claim carries no expiry, so it cannot be shown "
                "abandoned from here — cancel the task to release it)."
            )
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


class _BlockerVerdict(typing.NamedTuple):
    """A blocker paired with the reason it landed in its bucket.

    The reason is carried rather than re-derived at rendering time so the
    message can say *why* a blocker is fatal ("its owning build is failed")
    without re-issuing the lookups the classification already made.
    """

    blocker: FrontierExternalBlocker
    # Short why-clause appended to this blocker's description.
    note: str


class _ExternalBlockers(typing.NamedTuple):
    """Partition of a frontier's ``blocked_by_external`` (see below)."""

    # Out-of-build, RUNNING, claim not lapsed: someone else is executing
    # it, and their completion frees this build. Wait.
    executing: list[_BlockerVerdict]
    # Out-of-build, not running (so holding no claim, so carrying no
    # expiry), but owned by a build that is still live — that build is
    # going to schedule it. Wait.
    queued: list[_BlockerVerdict]
    # Out-of-build and nothing is going to run it. Fail.
    fatal: list[_BlockerVerdict]
    # In this build's own task set: already accounted for by actionable /
    # running / status_counts, so it must not influence the wait-or-fail
    # decision. Kept only to enrich the message.
    in_build: list[_BlockerVerdict]

    @property
    def waiting(self) -> list[_BlockerVerdict]:
        """Every blocker this build should wait on rather than fail over."""
        return self.executing + self.queued


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


async def _classify_external_blockers(
    frontier: BuildFrontier,
    now: datetime,
    *,
    registry: RegistryABC,
) -> _ExternalBlockers:
    """Split the frontier's external blockers into wait / fail / ignore.

    The frontier reports these only when the build has nothing actionable
    and nothing running — precisely the state the stuck-build check reads as
    "this build is dead". The split decides whether it actually is, on two
    questions: is the blocker in this build's own task set, and is anyone
    going to move it?

    ``blocking_in_build`` → **ignored**: already visible in ``actionable`` /
    ``running`` / ``status_counts``, so letting it drive this decision would
    double-count it (a blocker genuinely stuck *in* this build still fails
    the build, exactly as before).

    Out-of-build, the second question is answered from different evidence in
    the two halves of the table.

    **RUNNING — read the claim's expiry.** A RUNNING task holds an execution
    claim, and the claim's expiry is the one piece of liveness evidence a
    third party can evaluate without probing an executor it has no access to:

    - live expiry → **wait** (treated like a concurrency-limit denial:
      return, don't fail; the blocker's completion wakes this scheduler).
    - lapsed expiry → **fail**. Not *presumed* abandoned: the server no
      longer honours the claim, has stopped counting it against concurrency
      limits, and will hand it to the next claimant.
    - no expiry (``None``) → **wait**, unbounded, logged as such. Chosen
      deliberately: ``None`` is the server's encoding of "never lapses"
      (older server, or a start predating the column), and reading missing
      evidence as death would fail builds whose blocker is perfectly alive —
      the exact spurious failure this path exists to remove. The window is
      self-closing (new starts all carry an expiry) and the escape hatch is
      a task cancel, which the log line names.

    **Everything else — ask the owning build.** A PENDING/SUSPENDED/terminal
    task holds no claim, so the server clears the expiry with it and there
    is nothing to read. The wedge is real (a task abandoned SUSPENDED blocks
    every downstream build), so this half still decides, the only way it can:

    - owning build still live → **wait**; it is going to schedule it.
      Without this a PENDING dynamic dependency of a healthy concurrent
      build would fail this build.
    - owning build terminal → **fail**; nothing will move it.
    - ``blocking_status_build_id is None`` → **fail** without a lookup: no
      status-moving event was ever recorded against it, so there is nobody
      to ask.
    - owner status unresolvable (deleted build, unreachable registry, a
      registry that doesn't report it) → **fail**: unknown is not evidence
      of life, and a silent indefinite hang is the failure mode #208 exists
      to kill.

    So the owning-build lookup survives, for non-RUNNING blockers **only** —
    not out of caution but because it is the only evidence that exists for a
    status carrying no claim. What a live owner does not buy is a deadline:
    a build gone silent without transitioning is reaped server-side, not
    guessed at here from a task's age.

    None of this changes the outcome for a dead blocker outside this build's
    task set: this build can never run it, so the build still fails. What
    the expiry buys is certainty about *why*, and a message that says so.
    """
    executing: list[_BlockerVerdict] = []
    queued: list[_BlockerVerdict] = []
    fatal: list[_BlockerVerdict] = []
    in_build: list[_BlockerVerdict] = []

    # Owning-build statuses resolved during THIS classification only. A wide
    # DAG stalled behind one build yields one blocker entry per blocked edge,
    # every one naming the same owner, so without the memo this would be N
    # requests for one answer. Deliberately not cached across calls: a later
    # pass must be able to see the owner go terminal.
    #
    # The whole lookup only happens on the stalled path — the frontier
    # populates blocked_by_external solely when the build has nothing
    # actionable and nothing running — and now only for the non-RUNNING
    # blockers within it, so a healthy build issues zero extra requests,
    # however often it polls. Please don't "optimise" this away on the
    # assumption that it runs per tick in steady state; it does not.
    owner_statuses: dict[UUID, str | None] = {}

    async def owner_status(owner_id: UUID) -> str | None:
        if owner_id not in owner_statuses:
            try:
                info = await registry.build_get_aio(owner_id)
                owner_statuses[owner_id] = info.status
            except Exception as e:
                # Swallowed on purpose: this is a diagnostic lookup on the
                # path that decides a build's fate, and an unreachable or
                # deleted owner must produce a precise failure, not an
                # exception out of the tick.
                logger.warning(
                    f"Could not resolve the status of build {owner_id}, which "
                    f"owns a task blocking this build: {e}"
                )
                owner_statuses[owner_id] = None
        return owner_statuses[owner_id]

    for blocker in frontier.blocked_by_external:
        if blocker.blocking_in_build:
            in_build.append(_BlockerVerdict(blocker, "in this build's own task set"))
            continue

        if blocker.blocking_status in _RUNNING_STATUSES:
            # Two rows and no lookup: the claim says whether anyone is still
            # executing it. The owning build's status is not consulted even
            # when it is known — a live build proves nothing about one of its
            # claims, and a terminal one does not release them.
            expires_at = blocker.blocking_status_expires_at
            if _claim_has_lapsed(expires_at, now):
                fatal.append(
                    _BlockerVerdict(
                        blocker,
                        f"its execution claim lapsed at {expires_at}, so the "
                        "claim is abandoned and re-claimable — but not by "
                        "this build",
                    )
                )
            elif expires_at is None:
                executing.append(
                    _BlockerVerdict(
                        blocker,
                        "another build claims to be executing it, and the "
                        "claim carries no expiry, so nothing here can show "
                        "it abandoned",
                    )
                )
            else:
                executing.append(
                    _BlockerVerdict(
                        blocker,
                        "another build is executing it under a claim live "
                        f"until {expires_at}",
                    )
                )
            continue

        # Not running: no claim is held, so there is no expiry to read. It
        # moves only if the build owning its status is still going to
        # schedule it.
        owner_id = blocker.blocking_status_build_id
        if owner_id is None:
            fatal.append(
                _BlockerVerdict(
                    blocker,
                    "no build owns its status, so nothing has ever moved it",
                )
            )
            continue
        status = await owner_status(owner_id)
        if status is None:
            fatal.append(
                _BlockerVerdict(
                    blocker,
                    "its owning build's status is unknown (the lookup failed, "
                    "or this registry does not report it), which is not "
                    "evidence that anyone will run it",
                )
            )
        elif status in _TERMINAL_BUILD_STATUSES:
            fatal.append(_BlockerVerdict(blocker, f"its owning build is {status}"))
        else:
            # Includes an owner still PENDING: a build that has not started
            # yet may still start. A build that has gone silent *without*
            # transitioning is reaped server-side; a tick cannot tell the
            # difference from here and must not guess.
            queued.append(
                _BlockerVerdict(blocker, f"its owning build is still {status}")
            )

    return _ExternalBlockers(
        executing=executing, queued=queued, fatal=fatal, in_build=in_build
    )


# How many blockers a log line or build error names before summarising the
# rest. The server already caps its list (hence blocked_by_external_
# truncated); this second cap keeps a build's error_message readable when a
# wide DAG is stalled behind a single upstream.
_MAX_REPORTED_BLOCKERS = 5


def _describe_blockers(
    verdicts: Sequence[_BlockerVerdict], now: datetime, truncated: bool
) -> str:
    """One-line, user-actionable rendering of blockers (names, not ids only).

    ``truncated`` is the frontier's ``blocked_by_external_truncated``: the
    server capped its list, so this must not read as an exhaustive account.
    """
    described = "; ".join(
        (
            f"task {verdict.blocker.task_id} is blocked by "
            f"{_blocker_label(verdict.blocker)} "
            f"({verdict.blocker.blocking_task_id}), "
            f"{verdict.blocker.blocking_status.upper()}"
            + (
                ""
                if (age := _blocker_status_age_seconds(verdict.blocker, now)) is None
                else f" for {age:.0f}s"
            )
            + (
                f" under build {verdict.blocker.blocking_status_build_id}"
                if verdict.blocker.blocking_status_build_id is not None
                else " under no recorded build"
            )
            + f" — {verdict.note}"
        )
        for verdict in verdicts[:_MAX_REPORTED_BLOCKERS]
    )
    remaining = len(verdicts) - _MAX_REPORTED_BLOCKERS
    if remaining > 0:
        described += f"; and {remaining} more"
    if truncated:
        described += (
            "; the registry capped the blocker list, so there may be further "
            "blockers not shown"
        )
    return described


def _blocker_remediation(verdicts: Sequence[_BlockerVerdict]) -> str:
    """How to get out of it — the part the error used to lack.

    Both documented endpoints are addressed to a *build*, so a blocker
    whose owning build was never recorded has no build id to substitute
    into them. Telling that reader to "retry it under the build that owns
    it" is not a remedy, it is the shape of one — and this function exists
    precisely for the reader who is stuck.
    """
    unowned = [
        verdict
        for verdict in verdicts
        if verdict.blocker.blocking_status_build_id is None
    ]
    owned = [
        verdict
        for verdict in verdicts
        if verdict.blocker.blocking_status_build_id is not None
    ]

    parts: list[str] = []
    if owned:
        remedy = (
            "Retry the blocking task under the build that owns it "
            "(POST /api/v1/builds/{build_id}/tasks/{task_id}/retry) to reset "
            "it to pending — this now works from suspended as well as from "
            "failed/cancelled/skipped — then re-trigger this build"
        )
        if any(
            verdict.blocker.blocking_status in _RUNNING_STATUSES for verdict in owned
        ):
            remedy += (
                ". A blocker stuck RUNNING holds an execution claim no retry "
                "can take — and while a lapsed claim is re-claimable, no "
                "build is claiming it: cancel it (POST /api/v1/builds/"
                "{build_id}/tasks/{task_id}/cancel) to release the claim "
                "first"
            )
        parts.append(remedy + ".")
    if unowned:
        parts.append(
            "No build is recorded as having set the status of "
            f"{'some of these blockers' if owned else 'this blocker'}, so "
            "there is no build id to address a retry or cancel to. Find the "
            "task in the registry UI (or `stardag tasks list`), which names "
            "the build that owns each claim; if nothing owns it, the status "
            "predates claim recording and the task has to be re-run under a "
            "new build."
        )
    return " ".join(parts)


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
        blockers = await _classify_external_blockers(frontier, now, registry=registry)
        waiting = blockers.waiting
        summary.external_blockers += len(frontier.blocked_by_external)
        summary.external_blockers_waited += len(waiting)
        summary.external_blockers_fatal += len(blockers.fatal)

        if waiting and not blockers.fatal:
            # Waiting on work another build is doing or is about to do — the
            # same call the denied_this_round branch above makes, and for the
            # same reason: running == 0 here does not mean the environment is
            # idle. The blocker's completion wakes this scheduler; a lost
            # wake-up is covered by the watchdog. Logged every pass on
            # purpose: a build that sits here needs to be diagnosable from
            # the tick logs alone.
            # A RUNNING blocker whose claim carries no expiry cannot be shown
            # abandoned from here, so this particular wait has no end the tick
            # can see. Called out every pass rather than silently: it is the
            # one shape where waiting is a choice rather than a reading, and
            # the operator is the only one who can break the tie (by
            # cancelling the blocker) or remove the shape (by upgrading the
            # server, after which new starts carry an expiry).
            if any(
                verdict.blocker.blocking_status in _RUNNING_STATUSES
                and verdict.blocker.blocking_status_expires_at is None
                for verdict in waiting
            ):
                bound_note = (
                    " (a RUNNING blocker's claim carries no expiry, so this "
                    "wait cannot be shown to end — cancel the blocking task "
                    "to release its claim)"
                )
            else:
                bound_note = ""
            # Spelled out as "executing" vs "queued in a live build" because
            # the two are different operational situations: one is work in
            # progress, the other is work another build has not started yet.
            held_by = " and ".join(
                part
                for part in (
                    f"{len(blockers.executing)} being executed elsewhere"
                    if blockers.executing
                    else "",
                    f"{len(blockers.queued)} queued in a build that is still live"
                    if blockers.queued
                    else "",
                )
                if part
            )
            logger.info(
                f"Build {build_id} has nothing runnable or running of its own "
                f"but is waiting on {len(waiting)} upstream task(s) owned by "
                f"other builds ({held_by}); waiting rather than failing"
                f"{bound_note}: {_describe_blockers(waiting, now, truncated)}"
            )
            return None

        await _skip_blocked(registry, build_id, summary)
        if blockers.fatal:
            # Precise, actionable failure: which task, its name, its status,
            # how long it has been in it, which build owns it, why that owner
            # is not going to move it — plus how to release it. The status
            # counts alone (the whole of the old message) point nowhere near
            # the cause when the cause is not in this build's task set at all.
            reason = (
                f"Build cannot progress: it has nothing runnable or running "
                f"of its own, and {len(blockers.fatal)} of its task(s) are "
                "blocked by an upstream owned by another build that will not "
                f"move it (status counts: {counts}). Knowing the blocker is "
                "dead does not unblock this build — the blocker is not in "
                "this build's task set, so this build can never run it; it "
                "has to be released under the build that owns it. Blocked "
                f"by: {_describe_blockers(blockers.fatal, now, truncated)}"
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
        if not is_missing_route_error(e):
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
