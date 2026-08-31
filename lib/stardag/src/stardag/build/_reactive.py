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
3. On the way out, re-read the flag once before releasing the lease and
   once after — the **exit handshake**, which is what makes it safe for a
   worker to skip spawning a tick while a scheduler is live. See
   :func:`_run_tick_body_aio`.

Acting on a frontier is **bounded-concurrent**, not serial: a wide layer
is thousands of independent registry round-trips and executor spawns, and
doing them one after another in a single container is the difference
between a tick that finishes and a tick that is killed by its function
timeout mid-fan-out. The bound (``TickConfig.max_concurrent_actions``) and
the per-tick spawn cap (``TickConfig.max_spawns_per_tick``) are what keep
"concurrent" from meaning "unbounded" — see :func:`_act_on_frontier` and
:func:`_spawn_cap`. Truncating at the cap is not a stall: the tick acted,
so it re-evaluates immediately on a fresh frontier rather than lingering.

Workers self-report their lifecycle (see the Modal ``Runner``) and wake the
scheduler when they finish, so no process needs to stay alive while
long-running tasks execute. A wake-up is "set the flag, then make sure
somebody looks at it", and the second half is skipped when the registry
answers the flag-set with ``scheduler_live`` — a tick already holds the
lease and will see it. On a build of short tasks that is the difference
between one working tick and one per completion, each paying a container
start to discover it has nothing to do. A periodic watchdog tick covers
lost wake-ups (worker died silently) and externally-triggered state
changes (e.g. build cancelled from the UI).

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

**Retries.** A failure a tick *observes or causes* is retried, up to
``TickConfig.max_attempts`` attempts per task per build *round*. That is a
narrower promise than it sounds, and the narrowness is the point: the only
failures reaching the tick are the ones no execution backend can retry for
you — a spawn that failed before any container existed, and an execution
the backend killed or lost (OOM, preemption, a worker that vanished and
let its claim lapse). An exception *inside* the container never gets here
at all; the worker self-reports TASK_FAILED, which takes the task out of
the frontier, and covering that is what a backend's own function-level
retries (e.g. Modal's ``retries=``) are for. So this budget is spent on
infrastructure failures, not on deterministic ones — and a failure that
*is* deterministic from where the tick stands (a task whose object cannot
be rehydrated) is excluded explicitly.

A tick is short-lived and remembers nothing, so the count comes from the
server: ``FrontierTaskRef.attempt_count``, riding the frontier the tick
already fetches. Its window is the build's current **round** — starts
since the most recent BUILD_RESUMED, or since the build began. So the
budget is spent for the round, not for all time, and the escape is the one
operators already reach for: **re-trigger the build**, which emits
BUILD_RESUMED ahead of its discovery retries and starts every task at
zero again. A *bare* retry (the retry route, the UI's Retry button,
``stardag tasks retry``) emits no such event and does **not** reset the
budget — which is exactly the trap the second exhaustion message exists to
name, because from the operator's side the retry succeeds and then nothing
happens.

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
from functools import partial
from typing import Callable, Coroutine, Sequence
from uuid import UUID, uuid4

from stardag import (
    BaseTask,
    TaskStruct,
    flatten_task_struct,
    task_from_registry_data,
)
from stardag.build._base import (
    DetachedExecutionStatus,
    FailMode,
    TaskExecutorABC,
    current_build_id_var,
)
from stardag.build._task_modules import import_failure_note
from stardag.build._task_store import BuildTaskStore
from stardag.build._wakeups import SpawnTick, drain_wake_candidates
from stardag.exceptions import NotFoundError, is_missing_route_error
from stardag.registry import (
    BuildFrontier,
    FrontierExternalBlocker,
    FrontierTaskRef,
    RegistryABC,
)

logger = logging.getLogger(__name__)

# Statuses considered "in flight" for terminal detection.
_RUNNING_STATUSES = ("running",)
_TERMINAL_BUILD_STATUSES = ("completed", "failed", "cancelled")

# The platform ended an execution for a reason unrelated to the task (a
# function timeout, a reclaimed container) and the worker said so before
# it died. Deliberately NOT in ``_RUNNING_STATUSES``: an interrupted task
# holds no claim and occupies no concurrency slot, which is the point of
# reporting it. It arrives in the frontier's actionable set like a pending
# task, and ``_act_on_interrupted`` decides what happens to it.
_INTERRUPTED_STATUS = "interrupted"


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

# Default in-flight bound for the frontier actions a tick performs per task
# (load / probe / claim / spawn / record). These are registry HTTP calls, and
# 50 is the resident engine's long-standing bound against the same registry.
_DEFAULT_MAX_CONCURRENCY = 50

# Discovery gets its own, lower bound, because it is limited by something
# else entirely: ``complete_aio()`` asks the *target backend* whether an
# output exists, so the ceiling is that backend's tolerance, not the
# registry's. Object stores and network volumes are far less forgiving than
# an HTTP API, and a trigger running outside the execution environment pays
# full network cost for every check.
#
# Measured against a Modal volume target root from a laptop, discovering a
# 64-task layer: 16 in flight completed in ~26 s; 32 had not finished after
# 240 s; 50 failed outright with Modal's ResourceExhaustedError. Sharing one
# constant with the actions above looked tidy but conflated two different
# limits, and only the slower one is load-bearing.
#
# Tunable per call — a deployment whose target root is a fast local
# filesystem can raise it, and one on a stricter backend can lower it.
_DEFAULT_MAX_CONCURRENT_DISCOVER = 16


def _format_age(seconds: float) -> str:
    """Render an age the way an operator reads it.

    The blocker message is the one a stalled build's owner acts on, and
    "RUNNING for 10889s" makes them do arithmetic before they can judge
    whether that is alarming. Coarse on purpose — nobody needs seconds
    once it has been running for hours.
    """
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h {(seconds % 3600) // 60}m".replace(" 0m", "")
    return f"{seconds // 86400}d {(seconds % 86400) // 3600}h".replace(" 0h", "")


# --- Per-tick spawn cap ------------------------------------------------
#
# A tick runs in a container with a finite life, so "spawn everything that
# is actionable" is a bound on nothing: a wide enough layer outlives the
# container and the tick is killed mid-fan-out. These constants turn the
# cap into a duration budget instead of a magic count — see
# :func:`_spawn_cap`, which also documents the ladder of inputs the budget
# is derived from.

# Fraction of the container's wall-clock limit that one fan-out pass may
# spend. Well under half, because the pass is not all a tick does: the
# frontier fetch, terminal evaluation, the summary report and (usually) a
# second pass on a fresh frontier all have to fit in the same container.
_SPAWN_BUDGET_FRACTION = 0.25

# Wall-clock cost of putting ONE actionable task on a worker: a task-store
# read, the acquiring start, the executor spawn, and the ref-recording
# start — three network round-trips and a local read. Deliberately
# pessimistic (a p99 round-trip, not a median), because underestimating it
# inflates the cap, and an inflated cap is the failure this exists to
# prevent.
_SECONDS_PER_SPAWN = 2.0

# Used when NO wall-clock limit is known at all — neither the tick's own
# nor the executor's. There is nothing to derive from, so this is the one
# place a plain number is unavoidable. Chosen so that the pass stays short
# on any plausible container: 500 spawns x 2 s / 50 in flight is ~20 s of
# round-trips.
_DEFAULT_MAX_SPAWNS_PER_TICK = 500

# Floor and ceiling on the derived cap. The floor keeps a tight timeout
# from producing a cap so small that a build advances by dribs; the ceiling
# is where the fan-out stops being the limiting factor anyway (a frontier
# carrying 10k actionable refs is itself the expensive part of the tick).
#
# Note what the ceiling is NOT: a substitute for reading the right timeout.
# It bounds the absurd, not the merely wrong — 10k spawns is still far more
# than a five-minute container can do, so a cap derived from the wrong
# duration is not rescued by clamping it.
_MIN_SPAWN_CAP = 50
_MAX_SPAWN_CAP = 10_000

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


# The scheduler lease's TTL, and how often it is renewed while a tick
# lingers.
#
# The TTL is the **dead-tick recovery window**, and that is what sizes it:
# nothing releases the lease of a container that vanished, so until it
# lapses the build is invisible to drainers *and* ``notify`` answers
# ``scheduler_live=True``, so workers skip their spawn too. A preempted tick
# therefore stalls its build for up to one TTL, with the watchdog off by
# default. 60 s is what the lock-table lease this replaces used, and there
# was no reason to lengthen it.
#
# Renewing at a third rather than the old half means two consecutive
# renewals can fail before the lease is at risk — and a third failure is
# survivable too, because a refused renewal re-acquires (see
# ``SchedulerLease._renew_once``).
_LEASE_TTL_SECONDS = 60
_LEASE_RENEW_INTERVAL_SECONDS = _LEASE_TTL_SECONDS / 3


class SchedulerLease:
    """The build's single-flight lease, held for the life of one tick.

    Replaces the lease that used to ride on the global concurrency lock —
    the one live use of a deprecated subsystem, which meant every reader
    assembled a lock name from a build id and queried a second table to
    answer "does this build have a scheduler?".

    Renews itself in the background while the tick lingers, and stops
    driving if it can no longer show that it holds the lease: continuing
    past that point is the double-scheduling the lease exists to prevent.

    "Can no longer show" is deliberately broader than "the server said no".
    A renewal that *raises* proves nothing, and a lease has an expiry
    whether or not anyone is reachable to confirm it — so the deadline is
    tracked client-side and consulted directly. Without that, a registry
    outage spanning the whole TTL would leave a tick driving a build whose
    lease had lapsed server-side and could already have been taken over,
    with nothing having returned an answer to notice.
    """

    def __init__(self, registry: RegistryABC, build_id: UUID) -> None:
        self._registry = registry
        self._build_id = build_id
        # Per-tick identity, not per-process: two ticks for one build in one
        # container must not be able to renew or release each other's lease.
        self._owner_id = uuid4().hex
        self.acquired = False
        self._renewal: asyncio.Task | None = None
        self._lost = False
        # Monotonic, because this is a duration from a local acquire, not a
        # point in the server's calendar — a clock skew between the two
        # must not be able to extend it.
        self._expires_at: float | None = None

    @property
    def lost(self) -> bool:
        """Whether this tick can still show that it holds the lease."""
        if self._lost:
            return True
        if self._expires_at is None:
            return False
        return asyncio.get_event_loop().time() >= self._expires_at

    def _arm(self, granted_after: float) -> None:
        # Anchored to a timestamp taken BEFORE the granting request was
        # sent, not to when its answer arrived: the server starts the TTL
        # when it writes the row, which is inside that window, so anchoring
        # at the reply would leave the client believing in the lease for up
        # to one network round-trip after the server had let it lapse. The
        # error must land on the safe side — expiring early is a spurious
        # re-acquire; expiring late is the double-drive the deadline exists
        # to rule out.
        self._expires_at = granted_after + _LEASE_TTL_SECONDS

    async def __aenter__(self) -> "SchedulerLease":
        asked_at = asyncio.get_event_loop().time()
        result = await self._registry.build_acquire_scheduler_lease_aio(
            self._build_id, owner_id=self._owner_id, ttl_seconds=_LEASE_TTL_SECONDS
        )
        self.acquired = result.held
        if self.acquired:
            self._arm(asked_at)
            self._renewal = asyncio.create_task(self._renew_forever())
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._renewal is not None:
            renewal, self._renewal = self._renewal, None
            renewal.cancel()
            # ``gather(..., return_exceptions=True)`` reaps the child's
            # ``CancelledError`` as a *result* rather than raising it, while
            # still propagating a cancellation of this task. Catching
            # ``CancelledError`` here cannot tell the two apart — we
            # cancelled the child ourselves, so ``renewal.cancelled()`` is
            # true whichever one arrived — and swallowing it would strand
            # the caller's cancellation. ``run_tick_aio`` is public, so a
            # caller wrapping it in ``wait_for`` or a TaskGroup hits this.
            await asyncio.gather(renewal, return_exceptions=True)
        if not self.acquired:
            return
        try:
            await self._registry.build_release_scheduler_lease_aio(
                self._build_id, owner_id=self._owner_id
            )
        except Exception as e:
            # Best-effort: an unreleased lease lapses on its own, so the
            # cost is one TTL of delay before another tick may drive the
            # build — never a lost wake-up.
            logger.warning(
                "Could not release the scheduler lease for build %s "
                "(ignored; it expires on its own): %s",
                self._build_id,
                e,
            )

    async def _renew_once(self) -> bool:
        """Extend the lease, re-acquiring if it lapsed. False = really lost.

        A refused renewal has two causes and they are not the same event.
        Usually nothing else is competing for the build, and the refusal
        just means our own lease expired while we were between renewals —
        healed by taking it again, which is the same re-acquire the server
        performs for any lapsed lease. Only if that *also* fails is somebody
        else driving the build, and only then must this tick stop.
        """
        asked_at = asyncio.get_event_loop().time()
        result = await self._registry.build_renew_scheduler_lease_aio(
            self._build_id, owner_id=self._owner_id, ttl_seconds=_LEASE_TTL_SECONDS
        )
        if result.held:
            self._arm(asked_at)
            return True
        asked_at = asyncio.get_event_loop().time()
        retaken = await self._registry.build_acquire_scheduler_lease_aio(
            self._build_id, owner_id=self._owner_id, ttl_seconds=_LEASE_TTL_SECONDS
        )
        if retaken.held:
            self._arm(asked_at)
            logger.info(
                "Scheduler lease for build %s had lapsed and was re-taken; "
                "nothing else was driving it.",
                self._build_id,
            )
            return True
        return False

    async def _renew_forever(self) -> None:
        while True:
            await asyncio.sleep(_LEASE_RENEW_INTERVAL_SECONDS)
            try:
                held = await self._renew_once()
            except Exception as e:
                # A blip is not a loss — but it is not proof of the
                # opposite either, so nothing is re-armed here. If the
                # registry stays unreachable past the TTL, ``lost`` turns
                # true on the clock alone and the tick stops.
                logger.warning(
                    "Could not renew the scheduler lease for build %s "
                    "(will retry; the lease expires on its own if this "
                    "keeps failing): %s",
                    self._build_id,
                    e,
                )
                continue
            if not held:
                logger.warning(
                    "Lost the scheduler lease for build %s to another tick. "
                    "Stopping rather than driving a build a successor is "
                    "already driving.",
                    self._build_id,
                )
                self._lost = True
                return


# =============================================================================
# Bounded concurrency (shared by discovery and the tick)
# =============================================================================


# A unit of concurrent work: a zero-argument callable producing the
# coroutine, not the coroutine itself (see :func:`_run_concurrently`).
_ActionFactory = Callable[[], Coroutine[typing.Any, typing.Any, typing.Any]]


def _first_leaf_exception(error: BaseException) -> BaseException:
    """The first non-group exception inside a (possibly nested) group."""
    exceptions = getattr(error, "exceptions", None)
    if not exceptions:
        return error
    return _first_leaf_exception(exceptions[0])


async def _run_concurrently(
    factories: "Sequence[_ActionFactory]",
) -> None:
    """Run ``factories``' coroutines concurrently; wait for all of them.

    The resident engine's idiom (``asyncio.TaskGroup``; see
    ``build/_concurrent.py``), factored out so the tick and discovery use
    exactly one pattern between them rather than growing a second. This
    helper carries **no** bound of its own — every caller supplies one,
    either by passing a shared semaphore through :func:`_run_bounded` or,
    where the fan-out is recursive, by gating the expensive await inside
    the coroutine with a semaphore that spans the whole walk. A per-call
    semaphore would be no bound at all under recursion: each nesting level
    would mint a fresh one.

    **Factories, not coroutines.** TaskGroup cancels its siblings the
    moment one of them raises, and a sibling cancelled before it started
    would leave an already-constructed coroutine un-awaited — a "coroutine
    was never awaited" warning attached to the *unrelated* failure that
    triggered the cancellation. Nothing is constructed until it runs, so
    there is nothing to leak.

    **Failures surface as themselves.** TaskGroup wraps everything in an
    ``ExceptionGroup``; the tick's error handling — and the ``error_type``
    a crashed tick reports in its :class:`TickSummary` — is meant to name
    the thing that actually broke (a registry timeout, say), exactly as it
    did when this work ran in a plain ``for`` loop. The group is therefore
    unwrapped to its first leaf, with the group kept as the cause so
    siblings that failed at the same moment are still in the traceback.
    """
    if not factories:
        return
    try:
        async with asyncio.TaskGroup() as task_group:
            for factory in factories:
                task_group.create_task(factory())
    except BaseExceptionGroup as group:  # noqa: F821 (3.11+; TaskGroup is too)
        raise _first_leaf_exception(group) from group


async def _run_bounded(
    factories: "Sequence[_ActionFactory]",
    semaphore: asyncio.Semaphore,
) -> None:
    """:func:`_run_concurrently` with ``semaphore`` held for each coroutine.

    For flat fan-outs, where "at most N in flight" and "at most N of these
    coroutines running" are the same statement. The semaphore is passed in
    rather than created here so a caller can share one bound across
    several fan-outs (or across a recursion).
    """

    async def run_one(factory: "_ActionFactory") -> None:
        async with semaphore:
            await factory()

    await _run_concurrently([partial(run_one, factory) for factory in factories])


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
# expresses. INTERRUPTED is here on the same argument: the platform ended
# the execution, so there is nothing live to orphan. RUNNING is deliberately
# absent: it holds a live execution claim, and releasing that claim is
# cancellation, not retry.
#
# Every trigger passes retry_failed=True — a new build id and a resume
# alike, because both go through ``run_reactive_bootstrap`` now that
# discovery runs inside Modal. Workers registering dynamically yielded deps
# call discover_and_register_aio with the default (False), so a worker can
# never reset its own parent's SUSPENDED status out from under itself.
#
# So RUNNING is the only status that blocks a trigger, while a *mid-flight*
# tick resets only CANCELLED (see ``_classify_external_blockers``). The
# asymmetry is intended: at trigger *you asked*, so retrying a previous
# failure is what you meant. Mid-flight nobody asked, and ``fail_mode`` owns
# what happens to a failure.
_RETRYABLE_STATUSES = ("failed", "cancelled", "skipped", "suspended", "interrupted")


def _limit_keys_for(
    tasks: Sequence[BaseTask],
    selector: "Callable[[BaseTask], Sequence[str]] | None",
) -> "dict[UUID, Sequence[str]] | None":
    """Per-task limit keys for a registration chunk, or None without a selector.

    A selector that raises propagates: the tick's spawn path calls the same
    selector unguarded, so hiding the error here would only move it from
    the bootstrap — where it fails the build loudly, once — to a later pass.
    """
    if selector is None:
        return None
    return {task.id: list(selector(task)) for task in tasks}


async def discover_and_register_aio(
    registry: RegistryABC,
    build_id: UUID,
    tasks: TaskStruct,
    retry_failed: bool = False,
    _chunk_size: int = 50,
    max_concurrent_discover: int = _DEFAULT_MAX_CONCURRENT_DISCOVER,
    limit_key_selector: "Callable[[BaseTask], Sequence[str]] | None" = None,
) -> DiscoveryResult:
    """Walk ``tasks``' dependency trees, register everything, return state.

    Post-order walk (deps before parents, so the bulk endpoint resolves
    ``dependency_task_ids`` without phantom rows), stopping at
    already-complete tasks (their subtrees are irrelevant). Complete tasks
    are additionally marked complete in the registry so the frontier
    reflects them (in reactive mode the registry *is* the scheduler state).

    Used by the reactive trigger (initial discovery) and by workers
    registering dynamically yielded deps. That second caller is why the
    I/O here is worth bounded concurrency rather than a plain recursive
    ``await``: it is not a once-per-build cost, it is paid on the hot path
    of *every* dynamic-dependency yield, in every worker, on top of every
    reactive re-trigger.

    **Two phases, on purpose.** The expensive part of the walk is the
    per-task ``complete_aio()`` — a target existence check, i.e. remote
    I/O. That is what runs concurrently (``max_concurrent_discover``
    checks in flight, matching the resident engine's default so the two
    discovery paths stop being an order of magnitude apart). The
    *ordering* is then reconstructed by a second, purely local, strictly
    sequential post-order pass over the memoised results. Nothing about
    the returned :class:`DiscoveryResult` or the registration order
    therefore depends on which completion check happened to answer first:
    ``post_order``, ``incomplete``, ``previously_completed`` and
    ``retried`` come out exactly as the serial walk produced them, for any
    DAG — diamonds included. Concurrency buys throughput here and nothing
    else, which is the only way to add it to a path whose whole job is to
    get an ordering right.

    Bulk registration stays sequential across chunks for the same reason
    the walk is post-order: chunk *n* may contain the dependencies of
    chunk *n+1*, and overlapping them would reintroduce exactly the
    phantom-row window the ordering exists to close. Only the per-task
    calls that are independent of each other — the retry resets and the
    completion marks — are run concurrently, and both preserve their
    result order.

    ``limit_key_selector`` — the deployed app's mapping from a task to the
    named concurrency-limit keys it runs under — is applied to every
    registered task and the keys sent with the registration. That is what
    lets the registry wake the builds queued on a key when a slot frees:
    the relation it needs is "which pending tasks want this key", and keys
    are a property of the task, so plan time is when it can learn them. A
    selector that raises propagates, as it does on the tick's spawn path.

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

    # --- phase 1: concurrent completion checks -------------------------
    # Memoised per task id: whether it is complete, and (only when it is
    # not) its static dependencies. Both are exactly what the serial walk
    # computed inline; phase 2 replays the same recursion over them.
    is_complete: dict[UUID, bool] = {}
    deps_of: dict[UUID, list[BaseTask]] = {}
    # Guards the visited set. A task reached from two parents at once must
    # be checked once and recursed into once — the dedupe the serial walk
    # got for free from being serial. Held across no await but the set
    # mutation itself, so it never serialises the I/O below.
    visit_lock = asyncio.Lock()
    visited: set[UUID] = set()
    # ONE semaphore for the whole walk (not one per recursion level, which
    # would bound nothing), gating exactly the remote call: the target
    # existence check. Mirrors ``build/_concurrent.py``'s
    # ``discover_semaphore``.
    discover_semaphore = asyncio.Semaphore(max(1, max_concurrent_discover))

    async def visit(task: BaseTask) -> None:
        """Check ``task`` for completion and recurse into its deps.

        Order-free by construction: it records facts about tasks and never
        appends to an ordered collection, so sibling subtrees may finish in
        any interleaving. The set of tasks it visits is a property of the
        DAG and the completion predicate, not of the traversal order, so it
        is the same set the serial walk visited.
        """
        async with visit_lock:
            if task.id in visited:
                return
            visited.add(task.id)
        async with discover_semaphore:
            complete = await task.complete_aio()
        is_complete[task.id] = complete
        if complete:
            return  # don't recurse below complete tasks
        deps = flatten_task_struct(task.requires())
        deps_of[task.id] = deps
        await _run_concurrently([partial(visit, dep) for dep in deps])

    roots = flatten_task_struct(tasks)
    await _run_concurrently([partial(visit, task) for task in roots])

    # --- phase 2: sequential post-order over the memoised results ------
    # No I/O, no awaits: the same recursion the serial implementation ran,
    # with the completion check replaced by a dict lookup. This is what
    # makes the result byte-identical to the serial version's.
    seen: set[UUID] = set()

    def emit(task: BaseTask) -> None:
        if task.id in seen:
            return
        seen.add(task.id)
        if is_complete[task.id]:
            result.previously_completed.append(task)
            post_order.append(task)
            return
        for dep in deps_of[task.id]:
            emit(dep)
        result.incomplete[task.id] = task
        post_order.append(task)

    for task in roots:
        emit(task)

    for chunk_start in range(0, len(post_order), _chunk_size):
        chunk = post_order[chunk_start : chunk_start + _chunk_size]
        # The kwarg is passed only when there is something to pass, so a
        # registry whose bulk registration predates it is untouched unless
        # a selector is actually configured.
        keys = _limit_keys_for(chunk, limit_key_selector)
        infos = await registry.task_register_bulk_aio(
            build_id, chunk, **({"limit_keys": keys} if keys is not None else {})
        )
        if not retry_failed:
            continue
        # Resets are independent of each other (each addresses one task
        # row), so they run concurrently — but ``retried`` is appended in
        # ``infos`` order, not completion order, so the caller sees the
        # same list the serial version returned.
        to_retry = [
            result.incomplete[UUID(info.task_id)]
            for info in infos or []
            if info.latest_status in _RETRYABLE_STATUSES
            and UUID(info.task_id) in result.incomplete
        ]
        await _run_bounded(
            [partial(registry.task_retry_aio, build_id, task) for task in to_retry],
            discover_semaphore,
        )
        result.retried.extend(to_retry)

    await _run_bounded(
        [
            partial(registry.task_complete_aio, build_id, task)
            for task in result.previously_completed
        ],
        discover_semaphore,
    )

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
    # Budget, per task per build **round**, on how many executions a tick
    # will start for one task — counted server-side and read off the
    # frontier as ``FrontierTaskRef.attempt_count``, since a tick is
    # short-lived and remembers nothing. A round runs from the build's most
    # recent BUILD_RESUMED event (so re-triggering an existing build id
    # starts a fresh one and resets every task's count) or from the build's
    # beginning. A bare retry emits no such event and does not reset it.
    #
    # ``1`` disables retrying entirely: a failure is recorded and never
    # respawned, exactly as before this existed — and, being a budget of
    # one start, a task reset to PENDING after its one attempt in the
    # round is refused rather than started again.
    #
    # **Why the default is 2, not 1.** 1 would be the conservative choice
    # if the failures on this path were a random sample of failures — you
    # do not want a deterministic bug run three times. They are not. A tick
    # only ever sees the failures no backend can retry for it: a spawn that
    # failed before a container existed, and an execution the backend
    # killed or lost. An exception inside the container is self-reported by
    # the worker and leaves the frontier without a tick ever touching it,
    # so a task that is simply broken cannot spend this budget — which
    # removes the usual reason to default a retry policy to "off". What is
    # left is the status quo it replaces: under FAIL_FAST, one transient
    # spawn failure kills an entire build, and the only recovery is a human
    # noticing and re-triggering.
    #
    # **Why 2 and not more.** One retry covers the shape these failures
    # actually have — a spot preemption, an OOM on one worker, a backend
    # that refused a spawn for a moment. A task that fails the same way
    # twice is telling you something a third attempt will not fix, and
    # every extra attempt is paid in full on the pathological case. 2 is
    # also the smallest step that closes the gap, which is what a default
    # that changes existing behaviour should be.
    #
    # Note for DAGs with **dynamic dependencies**: resuming a SUSPENDED
    # task records a fresh start, so such a task accumulates attempts
    # simply by yielding. Resumption is never budget-gated (see
    # ``_act_on_frontier``) — a gate there would wedge dynamic DAGs
    # outright — but a suspend-heavy task does reach its retry budget
    # sooner than a plain one, so raise this if you retry those.
    max_attempts: int = 2
    # Budget, per task per build round, on how many times a task may ask
    # to be resumed before the scheduler stops obliging — the companion to
    # ``max_attempts``, and reached only by a task that raised
    # ``ResumableInterruption``.
    #
    # **Why a second budget rather than one.** An interruption is the
    # platform taking the container away: a function timeout, or a
    # reclaimed instance. The task did nothing wrong, and for the workload
    # this whole path exists for — a trainer that checkpoints and is
    # *supposed* to be killed and resumed until it converges — being
    # interrupted is not an error condition, it is the operating mode.
    # Charging those to ``max_attempts`` would exhaust a budget meant for
    # genuine failures and fail the build for the one reason it was
    # designed to survive. The precedent is resuming a SUSPENDED task,
    # which is never budget-gated for the same reason (see
    # ``_act_on_frontier``).
    #
    # **Why bounded at all, then.** "Not charged to the retry budget"
    # cannot mean "free": a task that times out every time would otherwise
    # loop forever, paying for a full container each round. So the bound
    # exists, and is set generously rather than tightly — 20 resumes of a
    # long training run is a plausible afternoon, 20 identical timeouts of
    # a hung task is a clear signal and a bounded bill.
    max_interruptions: int = 20
    # How many of a pass's per-task actions may be in flight at once. Each
    # actionable task costs a task-store read, an acquiring start, an
    # executor spawn and a ref-recording start; doing that serially makes a
    # wide layer a queue of thousands of round-trips in one container.
    #
    # Bounded rather than unbounded because the failure mode of "spawn
    # everything at once" is not better than the failure mode of "spawn
    # them one at a time" — it just moves it from the tick's own clock to
    # the registry's connection pool. The default is the resident engine's
    # (see ``_DEFAULT_MAX_CONCURRENCY``).
    max_concurrent_actions: int = _DEFAULT_MAX_CONCURRENCY
    # Hard cap on how many tasks ONE pass may spawn, i.e. how much work a
    # single container commits to. ``None`` (the default) derives it from a
    # wall-clock limit — see :func:`_spawn_cap` for the ladder.
    #
    # Truncation is never silent, and never a stall: the pass acted, so the
    # tick re-evaluates immediately on a fresh frontier (see
    # ``_run_tick_body_aio``'s ``if acted:`` branch) and picks up the rest.
    max_spawns_per_tick: int | None = None
    # The wall-clock limit of the container running THIS tick, when the
    # caller knows it (e.g. the Modal integration knows the ``timeout`` its
    # ``tick`` function was registered with). This is the quantity the
    # spawn cap actually wants: how long this process may live, not how
    # long the executions it spawns may run.
    #
    # Deployment infrastructure, not per-build configuration — deliberately
    # NOT in the Modal ``_TICK_KWARGS_ALLOWED`` allowlist, because a value
    # persisted in a build's stored tick config at trigger time would go
    # stale the moment the app is redeployed with a different timeout. The
    # one legitimate override is a caller that runs several ticks inside
    # one container and is passing on a *share* of it (the Modal watchdog
    # sweep does exactly this).
    tick_timeout_seconds: float | None = None
    # Maps a task to the named concurrency-limit keys it runs under (see
    # the registry's environment concurrency limits). Acquisition happens
    # atomically at task start, before the spawn: a denied task simply
    # stays in the frontier — a slot-holder's completion wakes the
    # scheduler (cross-build slot releases are covered by the watchdog).
    limit_key_selector: "Callable[[BaseTask], Sequence[str]] | None" = None
    # How to spawn a scheduler tick for a build on a deployed app. Used for
    # two things that mean the same — "somebody has to look at this build,
    # and it is not me": the post-release half of the exit handshake (see
    # ``_hand_off_if_needed``), and the cross-build drain that spawns ticks
    # for flagged builds nobody is serving (see ``_drain_wake_candidates``).
    # Deployed-app configuration for the same reason ``limit_key_selector``
    # is: it is a callable, and "how do I start a tick" is knowledge only
    # the executor integration has — ``run_tick_aio`` is deliberately
    # executor-agnostic.
    #
    # ``None`` disables both. That is correct for a caller whose wake-ups
    # spawn a tick unconditionally and that has no neighbours to wake — a
    # test harness, say. The two halves of the conditional wake-up go
    # together: an integration that makes its wake-ups conditional on
    # ``scheduler_live`` MUST provide this, or a wake-up that lands in the
    # release window is served by nobody until the watchdog.
    spawn_tick: "SpawnTick | None" = None
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

    # "not_reactive" | "lease_held" | "lease_lost" | "terminal" |
    # "lingered_out" | "error"
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
    # --- attempt budget (TickConfig.max_attempts) ---
    # Failures this tick recorded and then reset to PENDING because the
    # task's attempt budget for this build round still had room.
    retried: int = 0
    # Failures this tick recorded and deliberately left failed because the
    # budget was spent. Under FAIL_FAST every one of these fails the build
    # on the next pass, so a non-zero value here is the direct answer to
    # "why did this build fail when the failure looked transient?".
    retry_exhausted: int = 0
    # Starts this tick refused to make because the task arrived PENDING
    # with its budget already spent — i.e. a *bare* retry (the API's retry
    # route, the UI's Retry, ``stardag tasks retry``) put a budget-
    # exhausted task back in the frontier without starting a new round.
    # Counted separately from ``retry_exhausted`` because it is a different
    # event with a different remedy: not "a failure we won't retry" but "a
    # retry that cannot do anything". See ``_act_on_frontier``'s budget
    # gate.
    budget_denied: int = 0
    # --- interruptions (TickConfig.max_interruptions) ---
    # Interrupted tasks this tick resumed. A steadily climbing count on a
    # long-running task is the healthy checkpoint-and-resume signal, not a
    # problem.
    interruptions_restarted: int = 0
    # Interrupted tasks this tick refused to respawn because the
    # interruption budget for the round was spent, and failed instead.
    # Under FAIL_FAST each of these fails the build, so a non-zero value is
    # the direct answer to "why did my long-running task's build die?".
    interruptions_exhausted: int = 0
    # Interrupted tasks this tick converted to a retryable failure because
    # the registry cannot report interruption counts, so no budget could
    # bound resuming them. The only route to this counter.
    interruptions_failed: int = 0
    # Interrupted tasks left alone because their executor ref still probes
    # as live: the execution backend is retrying the input itself, so
    # spawning here would duplicate the execution. Not an error — it is the
    # guard working.
    interruptions_backend_retrying: int = 0
    # Cross-build blocking, summed over the terminal evaluations of this
    # tick (like limit_denied, these are counts of observations, not of
    # distinct tasks — one blocker seen on three linger passes counts
    # three times).
    #
    # ``external_blockers`` is every entry the frontier reported while the
    # build looked stalled; ``waited`` and ``fatal`` cover only the entries
    # that drove the wait-or-fail decision, so the three do not add up —
    # a blocker whose status is a result influences neither (see
    # ``_ExternalBlockers.inert``). A tick with waited > 0 and fatal == 0 is
    # the healthy "waiting on another build" state; fatal > 0 always
    # accompanies a failed build.
    external_blockers: int = 0
    external_blockers_waited: int = 0
    external_blockers_fatal: int = 0
    # Blockers this build reset so it could run them itself (the
    # collaboration path: a shared task another build cancelled is still this
    # build's to run). Only ever a task in this build's plan — the attempt
    # budget the reset is bounded by does not exist for anything else.
    in_build_blockers_reset: int = 0
    # --- exit handshake (see ``_hand_off_if_needed``) ---
    # Times the linger deadline expired with the wake-up flag set, so the
    # tick kept the lease and re-acted instead of exiting. The fast half of
    # the handshake: no successor container was needed.
    linger_extended: int = 0
    # Successor ticks spawned because the flag was set in the window
    # between this tick's last look and its release of the lease. Rare by
    # construction; a non-zero value is the handshake doing exactly the job
    # it exists for, not a problem.
    successor_spawned: int = 0
    # --- cross-build wake-ups (see ``_drain_wake_candidates``) ---
    # Ticks this tick spawned for *other* builds: flagged by the registry
    # (a task they hold changed status, or a slot they were queued on
    # freed) with no scheduler of their own live. Each is one neighbour
    # this tick's pass, or its exit, unblocked.
    neighbour_ticks_spawned: int = 0


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


# Outcomes that end a tick with nothing left for *this* tick to hand off,
# so the exit handshake's post-release re-read is skipped (see
# :func:`_hand_off_if_needed`). ``terminal`` means the build is finished —
# a wake-up arriving after it changes nothing a tick could act on;
# ``not_reactive`` means this build is not tick-driven at all; and
# ``lease_lost`` means a successor is already driving it, so spawning one
# would only add a container that finds the lease held and exits. (The
# drain is not skipped for ``lease_lost``: it is about *other* builds, and
# our own is filtered out of the candidates by the successor's live lease.)
#
# Every other outcome that got as far as holding the lease
# (``lingered_out``, and the crash path, which is still ``lingered_out``
# here because ``run_tick_aio`` relabels it afterwards) may have left a
# wake-up unserved.
_NO_HANDOFF_OUTCOMES = frozenset({"terminal", "not_reactive", "lease_lost"})


async def _hand_off_if_needed(
    build_id: UUID,
    *,
    registry: RegistryABC,
    config: TickConfig,
    summary: TickSummary,
) -> None:
    """Post-release half of the exit handshake — see ``_run_tick_body_aio``.

    Called once the scheduler lease is **released**, on every exit that may
    have left a wake-up unserved. Re-reads the wake-up flag and, if it is
    set, spawns a successor tick.

    A no-op without ``TickConfig.spawn_tick``, and deliberately
    so: an integration whose wake-ups always spawn a tick has no window to
    close, and paying a frontier fetch at the end of every tick to discover
    that would be pure cost. Skipping the read entirely (rather than
    reading and then finding nothing to do with the answer) is the point.

    Best-effort, like ``_report_tick_summary``: this runs in a ``finally``
    that may be unwinding an exception, so anything it raises would replace
    the error the caller is about to see with an unrelated one. A failed
    hand-off degrades to today's behaviour — the flag stays set, and the
    next completion or the watchdog picks it up.
    """
    spawn = config.spawn_tick
    if spawn is None:
        return
    try:
        flag = await registry.build_get_notify_aio(build_id)
        if not flag.needs_tick:
            return
        # The frontier read stays here, but only past the flag check: the
        # common case is an unset flag, which now costs one row instead of
        # a frontier. Deliberately not the slim ``build_get`` — this path
        # asks nothing of a backend that the pre-STA-18 code did not.
        #
        # The trade is honest rather than free. When the flag *is* set —
        # the case this exists for — it is two round trips where it used to
        # be one; and against a backend on the ABC default, or a server old
        # enough to have latched the fallback, the flag read *is* a
        # frontier fetch, so it is two frontier reads. This runs once per
        # tick, on the way out, so the poll's saving dominates either way.
        info = await registry.build_get_frontier_aio(build_id)
        if info.reactive_app_name is None:
            # Cannot happen for a build this tick just drove — the marker
            # never flips back — but the spawner needs an app to reach, and
            # guessing one would be worse than leaving the flag for the
            # next completion or the watchdog.
            return
        # Sync call in async code, on purpose: spawning is what every
        # executor integration offers, this is the last thing the tick does
        # (the lease is released, its renewal task cancelled, nothing else
        # is in flight), and requiring an async spawner would exclude the
        # integrations that only have a blocking one.
        spawn(build_id, info.reactive_app_name)
        summary.successor_spawned += 1
        logger.info(
            "Build %s was notified while this tick released the scheduler "
            "lease; handed off to a successor tick.",
            build_id,
        )
    except Exception as e:
        logger.warning(
            "Failed to hand off the scheduler for build %s (ignored — the "
            "wake-up flag stays set for the next completion or the "
            "watchdog): %s",
            build_id,
            e,
        )


async def _drain_wake_candidates(
    build_id: UUID,
    *,
    registry: RegistryABC,
    config: TickConfig,
    summary: TickSummary,
) -> list[UUID]:
    """Spawn ticks for the other builds the registry says need one.

    The cross-build half of a wake-up. The registry flags every reactive
    build whose frontier a status change may have touched — a task it holds
    changed status, a concurrency slot it was queued on freed, it was
    cancelled from the UI — but it has no executor. This tick has one, and
    is already here, so it asks for the flagged builds nobody is serving
    and spawns one tick each. The server hands each build out once per
    window, so N ticks draining at once still cost one container per
    flagged build: the storm that made every-writer-spawns unworkable
    cannot happen here by construction.

    Called after every pass that acted (the pass is what may have flagged a
    neighbour) and on every exit path (a finishing build's last act is
    often what unblocks its neighbours, and the exit is the last chance to
    say so). Not called when the lease was held — the tick that holds it
    drains on its own passes. Best-effort, see :mod:`stardag.build._wakeups`.
    """
    if config.spawn_tick is None:
        return []
    spawned = await drain_wake_candidates(
        registry, config.spawn_tick, build_id=build_id
    )
    # The tick's own build can be handed out on the exit path (flagged while
    # this tick held the lease, lease now released): that is a successor,
    # not a neighbour, and is counted as the hand-off it replaces.
    own = build_id in spawned
    summary.neighbour_ticks_spawned += len(spawned) - (1 if own else 0)
    if own:
        summary.successor_spawned += 1
    return spawned


# Set once per process the first time a tick takes the scheduler lease
# without a way to hand off (see :func:`_warn_if_no_successor_spawner`).
# Process-global for the same reason ``_tick_summary_route_missing`` is: the
# condition is a property of how this process was configured, not of one
# build, so saying it once is saying it.
_warned_missing_successor_spawner = False


def _warn_if_no_successor_spawner(config: TickConfig) -> None:
    """Say something when this tick holds the lease and cannot hand off.

    The two halves of the conditional wake-up are enforced in different
    places, and nothing connects them: a worker skips its tick spawn
    because the *registry* reports a scheduler live, while the ability to
    hand off at the end belongs to whoever *holds the lease*. A tick
    running without ``TickConfig.spawn_tick`` therefore disables
    the post-release read entirely — for every worker of that build, on the
    strength of a decision it never sees.

    In-tree that cannot happen: the Modal integration is the only thing
    that runs ticks and it always supplies the spawner. What this catches
    is the public entry point being driven by hand — ``run_tick_aio`` with
    a default ``TickConfig``, to poke at a build — which holds a real lease
    against real workers.

    Once per process, and not an error: an integration whose wake-ups
    always spawn unconditionally is correct without a spawner, and has
    nothing to be told.
    """
    global _warned_missing_successor_spawner
    if config.spawn_tick is not None or _warned_missing_successor_spawner:
        return
    _warned_missing_successor_spawner = True
    logger.warning(
        "Scheduler tick running without TickConfig.spawn_tick, so it cannot "
        "hand off on the way out or wake other builds. If this deployment's workers "
        "skip their tick spawn when the registry reports a live scheduler "
        "(BuildNotifyResult.scheduler_live), a wake-up arriving as this tick "
        "releases the lease is served by nobody until the next completion or "
        "the watchdog. Harmless if the wake-ups always spawn a tick "
        "regardless. Reported once per process."
    )


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

    **If your workers can skip spawning a tick**, pass
    ``TickConfig.spawn_tick``. The two halves of that
    optimisation live apart: a worker skips because the registry reports a
    scheduler live, while handing off at the end is the lease holder's job.
    A tick without a spawner disables the hand-off for every worker of that
    build, so driving this by hand against a live reactive build — the
    "manual" case above — is the one place the pairing can silently come
    apart. It warns once per process when it does.

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
    task_store: BuildTaskStore,
    config: TickConfig,
    summary: TickSummary,
) -> None:
    """The tick proper — see :func:`run_tick_aio`. Mutates ``summary``.

    **The exit handshake.** A wake-up is two steps — set the build's
    wake-up flag, then make sure somebody looks at it — and a caller may
    skip the second step when the registry reports a scheduler already
    live (``BuildNotifyResult.scheduler_live``). That skip is sound only
    if a live scheduler is *guaranteed* to observe a flag set before it
    releases the lease, and "poll until the deadline, then unwind" does not
    guarantee it: nothing re-reads the flag between the final poll and the
    release, so a flag set in that window would be served by nobody.

    The window is closed from both sides:

    * at deadline expiry, **before** giving up the lease, the flag is
      re-read; if it is set the tick keeps the lease and re-acts
      (``summary.linger_extended``);
    * **after** the lease is released, the flag is re-read once more and a
      successor tick spawned if it is set (``summary.successor_spawned``,
      see :func:`_hand_off_if_needed`).

    Releasing *before* the second read is what makes it airtight, and it
    needs no new server state. A wake-up landing while this tick unwinds
    either finds the lease already released — and spawns its own tick — or
    finds it held, in which case this tick has not yet done its
    post-release read and will find the flag. Both ticks happening is
    possible and harmless: one wins the lease, the other no-ops.

    **What it does not do.** This closes the release window; it is not
    crash recovery. A tick that clears the flag and then dies leaves the
    flag false, so the hand-off has nothing to see and the wake-up that
    tick had taken responsibility for waits for the next completion or the
    watchdog — exactly as it did before any of this existed. The
    ``finally`` is there so that an *exception* cannot skip the
    release-window check, not to resurrect the crashed tick's own wake-up.

    The pre-release re-read is the optimisation (one fewer cold start);
    the post-release hand-off is the correctness guarantee. Only the
    second is load-bearing, which is why the first may be skipped when
    extending the linger would not make sense (see the linger loop).
    """
    # Scheduler lease: renews itself while the tick lingers and releases on
    # exit. A held lease means immediate no-op — the wake-up that spawned
    # this tick was flagged before the spawn, so the holder's re-checks (the
    # linger poll, then the exit handshake above) cover it.
    lease = SchedulerLease(registry, build_id)
    acquired = False
    # Whether this tick ever cleared the wake-up flag — i.e. whether it took
    # responsibility for a wake-up at all. Gates the hand-off; see the
    # ``finally`` below for why that matters.
    cleared_a_wakeup = False
    try:
        async with lease:
            if not lease.acquired:
                logger.info(f"Scheduler lease for build {build_id} held; tick no-op.")
                summary.outcome = "lease_held"
                return

            acquired = True
            _warn_if_no_successor_spawner(config)

            # Expose the ambient build id (the executor forwards it to
            # self-reporting workers, exactly like the resident engine does).
            build_id_token = current_build_id_var.set(build_id)
            try:
                loop = asyncio.get_event_loop()
                deadline = loop.time() + config.linger_seconds
                while True:
                    if lease.lost:
                        summary.outcome = "lease_lost"
                        return
                    summary.iterations += 1
                    try:
                        await registry.build_clear_notify_aio(build_id)
                        cleared_a_wakeup = True
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

                    acted, denied_this_round, awaiting_backend = await _act_on_frontier(
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
                        #
                        # This is also what makes the per-tick spawn cap a
                        # throttle rather than a stall: a pass that truncated at
                        # the cap necessarily spawned (or failed to spawn, or
                        # was denied) every task it did attempt, so either it
                        # acted — and lands here, taking the next batch off a
                        # fresh frontier without waiting for anything — or every
                        # attempt was denied by a concurrency limit, in which
                        # case lingering for a slot to free up is precisely the
                        # right thing to do and re-acting immediately would be a
                        # hot loop against the registry.
                        deadline = loop.time() + config.linger_seconds
                        # This pass may have flagged a neighbour (a shared
                        # task changed status, a slot freed); wake it now
                        # rather than at exit, which may be minutes away.
                        await _drain_wake_candidates(
                            build_id, registry=registry, config=config, summary=summary
                        )
                        continue

                    # Linger: poll the wake-up flag until deadline.
                    #
                    # Two ways out. ``needs_tick`` is the ordinary one: some
                    # worker reported something.
                    #
                    # The other is an interrupted task whose execution still
                    # probes as live. That one is NOT waiting for an event —
                    # nothing will ever emit one, because the worker that would
                    # have reported is dead and an interrupted task produces
                    # nothing further. Waiting on the flag for it stalls the
                    # build until the watchdog, which is off by default.
                    #
                    # So those refs are re-probed directly, and only they: this
                    # deliberately does not re-enter the pass. Re-acting on
                    # every poll would re-attempt the claim of every
                    # limit-denied task at the poll interval, which is exactly
                    # the hot loop the ``acted`` branch above declines to
                    # create. The pass resumes only once a ref stops being
                    # live, which is the single fact being waited on.
                    #
                    # Residual, and honest: a ref that stays live past
                    # ``linger_seconds`` still lingers out with nothing
                    # scheduled to follow up. For a genuine backend retry that
                    # is fine — the restarted worker's own events re-tick the
                    # build. It is the unwinding race this closes.
                    while True:
                        if loop.time() >= deadline:
                            # Exit handshake, pre-release half (see the
                            # docstring): the flag may have been set since
                            # the last poll, and a worker that saw the
                            # lease held will not have spawned for it.
                            #
                            # Skipped when lingering is disabled
                            # (``linger_seconds <= 0``): extending a
                            # zero-length linger re-arms an already-expired
                            # deadline, so a build being notified steadily
                            # would spin here without ever sleeping.
                            #
                            # The watchdog sweep is still the caller that
                            # makes this load-bearing, for a changed reason:
                            # it used to run one pass per build across many
                            # builds in one container, so a spinning build
                            # starved the rest of the sweep. It spawns now,
                            # and asks each tick for one pass — so the spin
                            # would cost that build's container instead of
                            # the sweep, which is better and still wrong.
                            # Nothing is lost either way: the post-release
                            # hand-off is the guarantee, and handing the
                            # wake-up to a dedicated tick is what should
                            # happen anyway.
                            if config.linger_seconds <= 0:
                                return
                            flag = await registry.build_get_notify_aio(build_id)
                            if not flag.needs_tick:
                                return
                            summary.linger_extended += 1
                            # Re-arm before re-acting: a pass that finds
                            # nothing actionable does not reset the deadline
                            # (only ``acted`` does), so without this the
                            # tick would come straight back to an expired
                            # deadline and re-read the flag with no sleep in
                            # between.
                            deadline = loop.time() + config.linger_seconds
                            break  # outer loop clears the flag and re-acts
                        await asyncio.sleep(config.poll_interval_seconds)
                        if lease.lost:
                            # A successor already holds this build. Acting
                            # now is the double-scheduling the lease exists
                            # to prevent, and the successor has the flag.
                            summary.outcome = "lease_lost"
                            return
                        if awaiting_backend and await _any_ref_settled(
                            awaiting_backend, task_executor
                        ):
                            break  # a ref resolved — re-act and pick it up
                        flag = await registry.build_get_notify_aio(build_id)
                        if flag.needs_tick:
                            break  # outer loop clears the flag and re-acts
            finally:
                current_build_id_var.reset(build_id_token)
    finally:
        # Post-release half of the exit handshake (see this function's
        # docstring). In a ``finally`` so that an exception cannot bypass
        # it: a wake-up landing while a *crashing* tick unwinds is in the
        # same release window as one landing while a healthy tick exits,
        # and the worker that set it skipped its spawn either way.
        #
        # It is NOT crash recovery, and does not pretend to be. A tick that
        # cleared the flag and then died leaves it false, so nothing is
        # spawned and the wake-up it had taken responsibility for waits for
        # the next completion or the watchdog. That is unchanged from before
        # this handshake existed.
        #
        # ``cleared_a_wakeup`` is what stops the hand-off from turning a
        # repeatable failure into an unbounded chain of containers. The
        # first thing each pass does is clear the flag; if that call itself
        # keeps failing — a rate-limited or 5xx ``DELETE /notify`` while the
        # frontier ``GET`` stays healthy — then every tick would raise with
        # the flag still set, hand off, and be replaced by a successor that
        # fails identically, one cold container after another, forever. The
        # earlier reasoning here ("the successor clears the flag before it
        # can do the same") assumed the clear succeeds, which is exactly
        # what is broken in that state.
        #
        # So a tick that never cleared anything hands nothing on: it took no
        # responsibility, and its successor could only repeat its failure.
        # The cost is one narrow case — a worker skipped its spawn and this
        # tick died before its first clear — where the wake-up now waits for
        # the next completion or the watchdog. In that state the registry is
        # refusing writes and the build is not progressing regardless, which
        # makes it much the cheaper of the two failures.
        # Cross-build drain on the way out, whatever the outcome — terminal
        # included, since a finishing build's last completion is often
        # exactly what unblocked a neighbour. A tick that never held the
        # lease skips it: the holder drains on its own passes.
        #
        # BEFORE the hand-off, on purpose. With the lease released, this
        # tick's own build is a candidate if a wake-up landed while it held
        # the lease (the notifier saw a live scheduler and did not spawn), so
        # the drain may hand it out and spawn its successor — stamped
        # server-side, so no other drainer doubles it. The hand-off below
        # then only covers what the drain could not: a registry without the
        # route, or a hand-out refused because the build was stamped within
        # the window by a notifier that will spawn anyway.
        drained: list[UUID] = []
        if acquired and summary.outcome != "not_reactive":
            drained = await _drain_wake_candidates(
                build_id, registry=registry, config=config, summary=summary
            )
        if (
            acquired
            and cleared_a_wakeup
            and build_id not in drained
            and summary.outcome not in _NO_HANDOFF_OUTCOMES
        ):
            await _hand_off_if_needed(
                build_id, registry=registry, config=config, summary=summary
            )
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


class _SpawnCap(typing.NamedTuple):
    """A per-pass spawn cap and a plain-English account of where it came from.

    The source is carried, not re-derived at logging time, because the one
    question an operator has when a tick truncates is "which number did it
    read?" — and the ladder in :func:`_spawn_cap` has four rungs, three of
    which produce plausible-looking caps from very different inputs.
    """

    limit: int
    source: str


def _derived_spawn_cap(budget_source_seconds: float, config: TickConfig) -> int:
    """Cap from a wall-clock limit: how many spawns fit in a fraction of it.

    Putting one task on a worker costs a bounded amount of round-trips
    (``_SECONDS_PER_SPAWN``) and ``max_concurrent_actions`` of them are in
    flight at once, so the largest batch a pass can finish inside its
    budget is ``budget * concurrency / cost``, clamped.
    """
    budget_seconds = _SPAWN_BUDGET_FRACTION * budget_source_seconds
    derived = int(
        budget_seconds * max(1, config.max_concurrent_actions) / _SECONDS_PER_SPAWN
    )
    return max(_MIN_SPAWN_CAP, min(_MAX_SPAWN_CAP, derived))


def _spawn_cap(
    candidates: Sequence[BaseTask],
    task_executor: TaskExecutorABC,
    config: TickConfig,
) -> _SpawnCap:
    """How many tasks this pass may spawn, and why.

    The number that matters is not a count at all — it is a duration. A
    tick lives in a container with a wall-clock limit, and the cap exists
    to stop it starting more work than it can live long enough to finish.
    So the whole question is *which* duration to read, resolved down this
    ladder, most specific first:

    1. **``TickConfig.max_spawns_per_tick``** — an explicit answer, taken
       as given. The override always wins.
    2. **``TickConfig.tick_timeout_seconds``** — the wall-clock limit of
       *this* container, when the caller knows it (the Modal integration
       reads the ``timeout`` its ``tick`` function was registered with).
       This is the quantity the cap is actually about, so it is preferred
       over anything below it.
    3. **The executor's ``execution_timeout_seconds``** — a *proxy*, used
       only when rung 2 is unavailable. It measures how long the spawned
       executions may run, which is a different quantity and can differ by
       orders of magnitude (a 24-hour worker under a 5-minute tick would
       derive a cap the tick cannot possibly work through). It is still
       better than a bare constant for the common case of a tick sized
       like the work it schedules, and the log line says which rung
       produced the cap so a truncating tick is diagnosable. The
       **smallest** timeout among the candidates is used: with
       heterogeneous routing the tightest backend limit is the one that
       bounds the pass.
    4. **``_DEFAULT_MAX_SPAWNS_PER_TICK``** — no wall clock is known
       anywhere. Never unbounded: "however many are actionable" is exactly
       what this replaces.
    """
    if config.max_spawns_per_tick is not None:
        return _SpawnCap(
            max(1, config.max_spawns_per_tick),
            "TickConfig.max_spawns_per_tick (set explicitly)",
        )
    if config.tick_timeout_seconds is not None:
        return _SpawnCap(
            _derived_spawn_cap(config.tick_timeout_seconds, config),
            f"this tick container's own timeout ({config.tick_timeout_seconds:.0f}s)",
        )
    timeouts: list[float] = []
    for task in candidates:
        try:
            timeout = task_executor.execution_timeout_seconds(task)
        except Exception:
            # The ABC says this must not raise, but a spawn cap is not
            # worth failing a tick over — an executor that misbehaves here
            # simply contributes no timeout.
            logger.debug(
                f"Execution timeout resolution failed for task {task.id} "
                "while sizing the spawn cap; ignoring it.",
                exc_info=True,
            )
            continue
        if timeout is not None:
            timeouts.append(timeout)
    if not timeouts:
        return _SpawnCap(
            _DEFAULT_MAX_SPAWNS_PER_TICK,
            "the default (no wall-clock limit is known for this tick or its executor)",
        )
    return _SpawnCap(
        _derived_spawn_cap(min(timeouts), config),
        f"the executor's tightest execution timeout ({min(timeouts):.0f}s) "
        "as a proxy — this tick does not know its own container's timeout; "
        "set TickConfig.tick_timeout_seconds (or max_spawns_per_tick) if "
        "the tick is sized differently from its workers",
    )


# =============================================================================
# The attempt budget (TickConfig.max_attempts)
# =============================================================================


def _retry_allowed(attempts_spent: "int | None", max_attempts: int) -> bool:
    """Whether one more attempt fits inside a ``max_attempts`` budget.

    ``attempts_spent`` is the number of attempts this build has spent on
    the task *including* the one that just failed, or ``None`` when the
    registry does not report attempt counts at all (see
    ``FrontierTaskRef.attempt_count``).

    **``None`` refuses the retry**, and that is the one rule here worth
    arguing about, since everywhere else in this module a missing field
    means "no evidence, don't act on it". A retry is different from the
    other decisions taken on missing evidence: it is the only one that
    *creates more of the same decision*. Failing a task on no evidence
    costs one build; retrying on no evidence costs an unbounded loop —
    fail, respawn, fail, respawn — because the thing that would eventually
    stop it is the counter that is missing. So an unreported count degrades
    to precisely the pre-``max_attempts`` behaviour (record the failure,
    never respawn) rather than to an unbounded one.

    A budget of one or less is checked first and refuses regardless: it is
    the explicit "no retries" setting, and no count changes that.
    """
    if max_attempts <= 1:
        return False
    if attempts_spent is None:
        return False
    return attempts_spent < max_attempts


def _start_denied_by_budget(attempt_count: "int | None", max_attempts: int) -> bool:
    """Whether a task arriving PENDING has already spent its whole budget.

    The mirror image of :func:`_retry_allowed` on missing evidence, and for
    the same reason read the other way round: refusing a start is an act,
    and acting on an unreported (``None``) or zero count would stop builds
    that are perfectly healthy — a task nobody has counted is an ordinary
    spawn candidate, not an exhausted one. Only a positive, server-reported
    count that has reached the budget denies anything.
    """
    if attempt_count is None:
        return False
    return attempt_count >= max(1, max_attempts)


def _attempts_phrase(attempts_spent: int, max_attempts: int) -> str:
    """How the attempt count reads in a log line or a failure message."""
    return f"{attempts_spent} of {max_attempts} allowed attempt(s) spent"


async def _record_task_failure(
    task: BaseTask,
    reason: str,
    *,
    build_id: UUID,
    registry: RegistryABC,
    config: TickConfig,
    summary: TickSummary,
    retryable: bool,
    attempts_spent: "int | None" = None,
) -> None:
    """Record a task failure, and retry it when the budget allows.

    The failure is **always** recorded first, even when the task is about
    to be reset to pending: the TASK_FAILED event is what releases the
    execution claim and the concurrency-limit slots the attempt was
    holding, and it is the only trace a later reader has that the attempt
    happened at all. The retry is then exactly what the server's retry
    endpoint does for an operator (and what discovery does on re-trigger) —
    flip a terminal-but-retryable status back to PENDING — so a respawn
    needs no new mechanism and no new server route.

    **Why this does not fight ``_handle_terminal``'s FAIL_FAST check.**
    That check reads ``frontier.status_counts``, which is the snapshot
    taken *before* this pass acted. A failure recorded and retried inside
    one pass is therefore never counted as a build-killing failure: by the
    time the next frontier is fetched — which happens immediately, since
    the pass acted — the task is PENDING again. A failure left failed
    (budget spent, or not retryable) shows up in the very next snapshot and
    fails the build promptly, exactly as before.

    **``retryable`` is the caller's verdict on the failure's nature**, and
    is deliberately not derivable here. See :func:`_act_on_frontier` for
    the split; the short version is that a tick retries the failures no
    execution backend can retry for it (a spawn that never produced a
    container, an execution the backend killed or lost) and never retries
    one it can already see is deterministic (a task object that cannot be
    rehydrated will not rehydrate on the second reading either — retrying
    that spends the whole budget to arrive at the same failure, later).
    """
    await registry.task_fail_aio(build_id, task, reason)
    summary.failed_recorded += 1
    if not retryable:
        return

    if not _retry_allowed(attempts_spent, config.max_attempts):
        # Three different "no retry" situations, and conflating them is how
        # an operator ends up debugging the wrong one.
        if attempts_spent is None:
            # The registry cannot count attempts, so no budget can bound a
            # retry loop and none is attempted. Worth a line every time: it
            # is the only signal that a *configured* retry policy is inert.
            logger.warning(
                f"Task {task.id} of build {build_id} failed and will not be "
                "retried: this registry does not report per-round attempt "
                "counts, so TickConfig.max_attempts "
                f"({config.max_attempts}) cannot be enforced and retrying "
                "would be unbounded. Upgrade stardag-api to enable "
                f"scheduler retries. Failure: {reason}"
            )
        elif config.max_attempts <= 1:
            # Retries switched off deliberately. Not news; the recorded
            # failure is the event, and this line just says why nothing
            # followed it.
            logger.info(
                f"Task {task.id} of build {build_id} failed and will not be "
                "retried (TickConfig.max_attempts="
                f"{config.max_attempts} allows one attempt per task)."
            )
        else:
            summary.retry_exhausted += 1
            logger.error(
                f"Task {task.id} of build {build_id} failed and will NOT be "
                "retried: its attempt budget for this build round is spent "
                f"({_attempts_phrase(attempts_spent, config.max_attempts)}). "
                "To run it again, RE-TRIGGER THIS BUILD — "
                f"build_trigger(..., build_id={build_id}, reactive=True) — "
                "which starts a new round and resets every task's attempt "
                "count to zero. Retrying the task on its own does NOT reset "
                "the count: a bare retry (the UI's Retry, `stardag tasks "
                "retry`, the retry API route) makes the task pending again "
                "but leaves the budget spent, so this scheduler would "
                "decline to start it. If the task needs more attempts per "
                "round, re-trigger with a raised budget, e.g. "
                'tick_kwargs={"max_attempts": 4}. Last failure: '
                f"{reason}"
            )
        return

    try:
        await registry.task_retry_aio(build_id, task)
    except Exception as e:
        # Swallowed rather than raised: the failure is already recorded, so
        # the task is in a consistent terminal state and the build fails
        # the way it would have before this existed. Crashing the tick here
        # would trade a lost retry for a lost pass — and the pass's other
        # spawns with it.
        logger.error(
            f"Task {task.id} of build {build_id} failed and could not be "
            f"reset to pending for another attempt: {e}. It stays failed."
        )
        return
    summary.retried += 1
    logger.warning(
        f"Task {task.id} of build {build_id} failed and has been reset to "
        "pending for another attempt "
        f"({_attempts_phrase(attempts_spent or 0, config.max_attempts)}): "
        f"{reason}"
    )


async def _act_on_frontier(
    frontier: BuildFrontier,
    *,
    build_id: UUID,
    registry: RegistryABC,
    task_executor: TaskExecutorABC,
    task_store: BuildTaskStore,
    config: TickConfig,
    summary: TickSummary,
) -> tuple[bool, int, "list[tuple[FrontierTaskRef, BaseTask]]"]:
    """Spawn/probe/heal the actionable tasks, with bounded concurrency.

    Returns ``(acted, denied_this_round, awaiting_backend)``: whether
    anything acted, how
    many tasks were denied by concurrency limits in THIS pass (used by
    terminal detection — a cumulative count would keep suppressing the
    stuck-build check long after the denied tasks have run).

    **Three phases, each bounded by ``max_concurrent_actions``.** Resolve
    every actionable task's object; probe the ones already RUNNING; spawn
    the rest. Phases exist because the spawn cap has to be sized against
    the tasks that are actually spawn candidates, which is not knowable
    until the objects are loaded. Within a phase the work is independent
    per task; between phases nothing is.

    **What concurrency does NOT change.** Each spawn coroutine still runs
    its three steps in order — acquiring start, spawn, ref-recording start
    — so a task denied by a concurrency limit or by a competing claim never
    reaches ``submit_detached`` and never occupies a worker, and no
    executor ref is ever recorded for an execution that does not exist.
    Ordering *between* tasks was never guaranteed and is not relied upon:
    the frontier's actionable set is by definition a set of tasks whose
    dependencies are all satisfied.

    **Nor does it change the claim.** Every task here is claimed at most
    once per pass — the frontier lists a task once, and phases do not
    overlap — so these coroutines are not racing each other for the same
    claim. Nor is this tick racing another tick of the same build: it holds
    the build's scheduler lease. The claim is still what arbitrates against
    *other builds* (and against a tick of this build that the lease
    manager's own failure modes let through), which is exactly the job it
    had before, unchanged.

    **Which failures are retried.** Three failure paths run through here,
    and they are not the same kind of thing:

    - a **spawn** that raised before any container existed → retryable.
      This is the case no execution backend can cover: its function-level
      retries start counting once there is a function call to retry, and
      there is not one.
    - an execution the backend reports **failed**, or one whose claim
      lapsed with no ref to probe → retryable. OOM kills, preemptions and
      workers that died mid-write all land here, and every one of them is
      transient by nature.
    - a task whose **object cannot be resolved** (no stored pickle and no
      rehydratable registry data) → *not* retryable. The inputs to that
      failure are the task store and the imported task classes; neither
      changes between two passes of the same tick, so a retry re-reads the
      same absence and fails identically, having spent the budget that a
      genuinely transient failure elsewhere in the build might have needed.

    Note what is absent: an exception *inside* a task never appears here.
    The worker self-reports TASK_FAILED, which takes the task out of the
    frontier entirely, so the deterministic failures — the ones where
    "retry it" is the wrong answer — are structurally out of reach of this
    budget rather than excluded by a judgement call.

    **The budget gate.** A task arriving PENDING with its budget already
    spent is refused a start and failed again, with a message saying why.
    Exactly one thing produces that shape: a **bare retry** of a
    budget-exhausted task — the UI's Retry button, ``stardag tasks retry``,
    the API's retry route. Those flip the status without recording
    BUILD_RESUMED, so the round the count is measured against is unchanged
    and the task comes back pending with nothing left to spend. The retry
    *succeeds* server-side, so without the gate saying something the
    operator sees a task go PENDING and then quietly do nothing at all.

    A **re-trigger** is not this case and never lands here: it records
    BUILD_RESUMED *before* its discovery retries the failed tasks, so the
    round boundary is ahead of them and they arrive at zero. That is why
    the message points at a re-trigger rather than at a retry.

    Resumption of a **SUSPENDED** task is never gated either: a
    dynamic-dependency yield records a fresh start, so gating there would
    refuse to resume any task that yielded more times than the budget
    allows — turning a retry policy into a cap on dynamic dependencies.

    **Counters.** ``summary``'s fields and the two returned values are
    mutated from several coroutines. That is safe without a lock because
    every mutation is a bare ``+=`` on the same event loop with no
    ``await`` between the read and the write — asyncio switches tasks only
    at suspension points. Please keep it that way: a counter update that
    grows an ``await`` in the middle stops being atomic.
    """
    if frontier.build_status in _TERMINAL_BUILD_STATUSES:
        return False, 0, []  # terminal handling deals with it
    acted = False
    denied_this_round = 0
    # Interrupted tasks left alone because their execution still probes as
    # live, as ``(ref, task)``. Returned so the caller can keep re-probing
    # *just these*: unlike everything else a pass can be waiting for,
    # NOTHING will emit an event when this resolves — the worker that would
    # have is already dead.
    awaiting_backend: list[tuple[FrontierTaskRef, BaseTask]] = []
    # Task ids appended to ``spawn_candidates`` because they asked to be
    # resumed, so the spawn phase can count only the ones it actually
    # spawned (see ``summary.interruptions_restarted``).
    resumption_requests: set[UUID] = set()
    semaphore = asyncio.Semaphore(max(1, config.max_concurrent_actions))

    # --- phase 1: resolve task objects --------------------------------
    # Results are written by index, not appended, so the partition below
    # follows the frontier's own order regardless of which loads finished
    # first — a tick's logs stay readable in the order the registry
    # reported.
    resolved: list[BaseTask | None] = [None] * len(frontier.actionable)

    async def resolve(index: int, item: FrontierTaskRef) -> None:
        nonlocal acted
        task = await _load_task(
            item.task_id,
            registry,
            task_store,
            quiet=item.latest_status in _RUNNING_STATUSES,
        )
        if task is not None:
            resolved[index] = task
            return
        if item.latest_status in _RUNNING_STATUSES:
            # Can't probe without the object, but the worker reports its
            # own terminal events — leave it to resolve itself.
            return
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
            await _record_task_failure(
                typing.cast(BaseTask, _MissingTaskRef(id=UUID(item.task_id))),
                "Task object missing from the build task store",
                build_id=build_id,
                registry=registry,
                config=config,
                summary=summary,
                # Deterministic: neither the task store nor this process's
                # imported task classes change between passes, so a retry
                # buys a second identical failure at the cost of an
                # attempt. Fail it once and let the build say so.
                retryable=False,
            )
            acted = True
        except Exception as e:
            logger.error(
                f"Failed to record store-missing failure for task {item.task_id}: {e}"
            )

    await _run_bounded(
        [
            partial(resolve, index, item)
            for index, item in enumerate(frontier.actionable)
        ],
        semaphore,
    )

    running_items: list[tuple[FrontierTaskRef, BaseTask]] = []
    spawn_candidates: list[BaseTask] = []
    budget_denied: list[tuple[FrontierTaskRef, BaseTask]] = []
    interrupted_items: list[tuple[FrontierTaskRef, BaseTask]] = []
    for item, task in zip(frontier.actionable, resolved):
        if task is None:
            continue
        if item.latest_status in _RUNNING_STATUSES:
            running_items.append((item, task))
        elif item.latest_status == _INTERRUPTED_STATUS:
            # Its own phase, not a spawn candidate: what happens to an
            # interrupted task depends on a policy, on a separate budget,
            # and — when the execution backend runs its own retries — on
            # whether the backend is already restarting the very same
            # input. See ``_act_on_interrupted``.
            interrupted_items.append((item, task))
        elif item.latest_status == "pending" and _start_denied_by_budget(
            item.attempt_count, config.max_attempts
        ):
            # PENDING with the budget already spent — see the docstring's
            # "budget gate". Only a BARE retry gets a task into this shape
            # (a re-trigger records BUILD_RESUMED first, so its retried
            # tasks arrive at zero), and the gate exists so that retry is
            # not silently inert.
            #
            # SUSPENDED is excluded by the status check above, on purpose:
            # resuming a task that yielded dynamic dependencies records a
            # fresh start, so a suspend-heavy task is "over budget" while
            # being entirely healthy, and gating it would refuse to resume
            # it — a wedged DAG, not a declined retry.
            budget_denied.append((item, task))
        else:
            spawn_candidates.append(task)

    # Attempt counts by task id, so the spawn coroutine can size the budget
    # for a spawn failure. It takes the task *object* (that is what the cap
    # and the executor need), and the count lives on the frontier ref.
    attempts_by_task_id = {
        item.task_id: item.attempt_count for item in frontier.actionable
    }

    # --- phase 2: probe the RUNNING ones ------------------------------
    async def probe(item: FrontierTaskRef, task: BaseTask) -> None:
        nonlocal acted
        resolution = await _resolve_running(item, task, task_executor)
        if resolution == "complete":
            await registry.task_complete_aio(build_id, task)
            summary.self_healed += 1
            acted = True
        elif resolution == "failed":
            # Retryable: the backend killed or lost this execution (OOM,
            # preemption, a worker that vanished and let its claim lapse),
            # and none of those are things the backend's own function-level
            # retries cover. The attempt being closed here is the one the
            # frontier already counted — the claim-expiry path arrives via
            # exactly this branch, so the two mechanisms record one attempt
            # between them, not two.
            await _record_task_failure(
                task,
                "Detached execution failed (observed by tick)",
                build_id=build_id,
                registry=registry,
                config=config,
                summary=summary,
                retryable=True,
                attempts_spent=item.attempt_count,
            )
            acted = True
        # "leave": still running (or unprobeable) — nothing to do.

    await _run_bounded(
        [partial(probe, item, task) for item, task in running_items],
        semaphore,
    )

    # --- phase 2b: refuse starts for tasks already at their budget -----
    async def deny_budget(item: FrontierTaskRef, task: BaseTask) -> None:
        nonlocal acted
        # Non-None by construction: ``_start_denied_by_budget`` denies only
        # on a positive, server-reported count.
        spent = item.attempt_count or 0
        logger.error(
            f"Task {task.id} of build {build_id} is PENDING again with its "
            "attempt budget for this build round already spent "
            f"({_attempts_phrase(spent, config.max_attempts)}), so a BARE "
            "RETRY put it back — the UI's Retry button, `stardag tasks "
            "retry`, or the retry API route. That retry SUCCEEDED: the task "
            "really is pending again. But a bare retry does not start a new "
            "build round, so the attempt count it is measured against is "
            "unchanged, and the scheduler will not start it. Failing it "
            "again rather than leaving it PENDING and inert, so this is "
            "visible instead of looking like nothing happened. What you "
            "wanted is a RE-TRIGGER of this build — "
            f"build_trigger(..., build_id={build_id}, reactive=True) — which "
            "records BUILD_RESUMED, starts a new round, resets every task's "
            "attempt count to zero and re-runs exactly this task. Add "
            'tick_kwargs={"max_attempts": 4} to the re-trigger if it needs '
            "more attempts per round."
        )
        await _record_task_failure(
            task,
            (
                f"Attempt budget spent ({spent} of {config.max_attempts} "
                "allowed attempt(s) in this build round). A bare retry does "
                "not reset it — re-trigger this build "
                f"(build_id={build_id}) to start a new round, optionally "
                'with tick_kwargs={"max_attempts": N}.'
            ),
            build_id=build_id,
            registry=registry,
            config=config,
            summary=summary,
            # Already over budget by construction — going through the
            # retry branch would only re-derive that and log it twice.
            retryable=False,
        )
        summary.budget_denied += 1
        # Deliberately NOT counted in ``denied_this_round``: that number
        # suppresses terminal detection because a limit-denied task is
        # waiting for a slot and will run. This one will not run, and the
        # failure just recorded is what should fail the build.
        acted = True

    await _run_bounded(
        [partial(deny_budget, item, task) for item, task in budget_denied],
        semaphore,
    )

    # --- phase 2c: decide what an interruption meant ------------------
    async def act_on_interrupted(item: FrontierTaskRef, task: BaseTask) -> None:
        nonlocal acted
        # The backend may be retrying the input itself. Modal, for one,
        # restarts a timed-out input when the worker function declares
        # ``retries``, and it does so under the SAME executor ref — so the
        # ref probing as live is proof that a restart is in flight and that
        # spawning here would run the task twice. Probed rather than
        # assumed, because the tick cannot see the worker's retry config.
        #
        # Cost is one non-blocking probe per interrupted task, on a path
        # that only runs when something was actually interrupted.
        if item.latest_executor_ref and item.latest_executor:
            status = await _probe_detached(item, task, task_executor)
            if status == DetachedExecutionStatus.RUNNING:
                awaiting_backend.append((item, task))
                summary.interruptions_backend_retrying += 1
                logger.info(
                    f"Task {task.id} of build {build_id} was interrupted but "
                    f"its execution {item.latest_executor_ref} still probes "
                    "as live — either the backend is retrying the input "
                    "itself, or the call has not finished unwinding yet. "
                    "Leaving it alone this pass and re-probing shortly, "
                    "rather than spawning a duplicate."
                )
                return

        # Every INTERRUPTED task is here because a worker asked to be
        # resumed — that status is only ever written for a task that raised
        # ``ResumableInterruption``. An interruption the task did NOT catch
        # never reaches this branch: the worker reports nothing, the
        # execution dies, and a later pass records it as an ordinary
        # retryable failure. So there is no policy to consult here, and no
        # per-task configuration deciding whether a timeout was "expected" —
        # the task said so by raising, or it did not.
        spent = item.interrupt_count
        if spent is None:
            # No counter, so nothing can bound a resume loop. Degrade to the
            # thing that IS bounded (``max_attempts``) rather than to an
            # unbounded one. See ``FrontierTaskRef.interrupt_count``.
            logger.warning(
                f"Task {task.id} of build {build_id} asked to be resumed, "
                "but this registry does not report per-round interruption "
                "counts, so TickConfig.max_interruptions cannot be enforced "
                "and resuming would be unbounded. Recording a retryable "
                "failure instead. Upgrade stardag-api to enable resumption."
            )
            summary.interruptions_failed += 1
            await _record_task_failure(
                task,
                "Execution interrupted; this registry cannot bound resumption",
                build_id=build_id,
                registry=registry,
                config=config,
                summary=summary,
                retryable=True,
                attempts_spent=item.attempt_count,
            )
            acted = True
            return

        if spent < config.max_interruptions:
            # Counted at spawn time, not here: this phase runs before the
            # per-pass spawn cap, and interrupted tasks are appended after
            # the pending ones, so they are the first to be truncated.
            # Incrementing here would report resumes that did not happen.
            resumption_requests.add(task.id)
            spawn_candidates.append(task)
            logger.info(
                f"Task {task.id} of build {build_id} checkpointed and asked "
                f"to be resumed; starting it again ({spent} of "
                f"{config.max_interruptions} allowed interruption(s) "
                "absorbed this build round)."
            )
            return

        summary.interruptions_exhausted += 1
        logger.error(
            f"Task {task.id} of build {build_id} has been interrupted "
            f"{spent} time(s) this build round, which is its whole budget "
            f"(TickConfig.max_interruptions={config.max_interruptions}). "
            "Failing it rather than resuming it again. If this task "
            "legitimately needs more resumes — a long training run that "
            "checkpoints, say — re-trigger this build with "
            'tick_kwargs={"max_interruptions": N}, which also starts a new '
            "round and resets the count."
        )
        await _record_task_failure(
            task,
            (
                f"Interruption budget spent ({spent} of "
                f"{config.max_interruptions} allowed interruption(s) in this "
                f"build round). Re-trigger this build (build_id={build_id}) "
                "to start a new round, optionally with "
                'tick_kwargs={"max_interruptions": N}.'
            ),
            build_id=build_id,
            registry=registry,
            config=config,
            summary=summary,
            # Already over its budget; the retry branch would only re-derive
            # that against a different budget and log it twice.
            retryable=False,
        )
        acted = True

    await _run_bounded(
        [partial(act_on_interrupted, item, task) for item, task in interrupted_items],
        semaphore,
    )

    # --- phase 3: spawn, up to this pass's cap ------------------------
    cap = _spawn_cap(spawn_candidates, task_executor, config)
    if summary.iterations <= 1:
        # Once per tick, at INFO: the cap and — more importantly — which of
        # :func:`_spawn_cap`'s four rungs produced it. Three of them yield
        # plausible-looking numbers from very different inputs, so a
        # truncating tick is only diagnosable if the log says which was
        # read. Subsequent passes of the same tick re-derive the same
        # answer and would only repeat themselves.
        logger.info(
            f"Tick for build {build_id} will spawn at most {cap.limit} "
            f"task(s) per pass, from {cap.source}."
        )
    if len(spawn_candidates) > cap.limit:
        # Loud, always: a build that spawns in batches is a build whose
        # logs must say so, or the next reader concludes the frontier is
        # shrinking for some other reason. Not a stall — see the
        # ``if acted:`` branch in ``_run_tick_body_aio``.
        logger.info(
            f"Build {build_id} has {len(spawn_candidates)} spawnable tasks "
            f"this pass, more than the per-tick cap of {cap.limit} (from "
            f"{cap.source}); spawning the first {cap.limit} and "
            "re-evaluating on a fresh frontier immediately. Set "
            "TickConfig.max_spawns_per_tick to change the cap."
        )
        spawn_candidates = spawn_candidates[: cap.limit]

    async def spawn(task: BaseTask) -> None:
        nonlocal acted, denied_this_round
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
            return
        try:
            handle = await task_executor.submit_detached(task)
        except Exception as e:
            logger.error(f"Failed to spawn task {task.id}: {e}")
            # The one failure no execution backend can retry for us: there
            # is no function call to retry. The claiming start above went
            # through, so the attempt the server has counted by now is one
            # more than the frontier reported when this pass began.
            spent = attempts_by_task_id.get(str(task.id))
            await _record_task_failure(
                task,
                f"Spawn failed: {e}",
                build_id=build_id,
                registry=registry,
                config=config,
                summary=summary,
                retryable=True,
                attempts_spent=None if spent is None else spent + 1,
            )
            acted = True
            return
        await registry.task_start_aio(
            build_id,
            task,
            executor=handle.executor,
            executor_ref=handle.ref,
            executor_metadata=handle.executor_metadata,
            claim_ttl_seconds=ttl_seconds,
        )
        summary.spawned += 1
        if task.id in resumption_requests:
            # Counted here rather than where the request was read, so a
            # pass truncated by the spawn cap does not report resumes it
            # never made.
            summary.interruptions_restarted += 1
        acted = True

    await _run_bounded([partial(spawn, task) for task in spawn_candidates], semaphore)
    return acted, denied_this_round, awaiting_backend


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


async def _any_ref_settled(
    awaiting: "list[tuple[FrontierTaskRef, BaseTask]]",
    task_executor: TaskExecutorABC,
) -> bool:
    """Whether any awaited execution has stopped probing as live.

    The linger loop's cheap poll: one non-blocking probe per interrupted
    task the pass left behind, and nothing else. Returning True sends the
    tick back through a full pass, which will re-probe and act.
    """
    for item, task in awaiting:
        status = await _probe_detached(item, task, task_executor)
        if status != DetachedExecutionStatus.RUNNING:
            return True
    return False


async def _probe_detached(
    item: "FrontierTaskRef",
    task: BaseTask,
    task_executor: TaskExecutorABC,
) -> DetachedExecutionStatus:
    """Ask the executor about ``item``'s recorded execution, never raising.

    Thin because the interesting judgement is at the call site: only
    :data:`DetachedExecutionStatus.RUNNING` is treated as "hands off". In
    particular ``UNKNOWN`` is NOT — and that is a deliberate departure from
    :func:`_resolve_running`, where UNKNOWN means "leave it".

    The difference is what "leave it" costs. A RUNNING task that is left
    keeps a live claim and gets re-probed by the next tick, so waiting is
    free and duplicate-safe. An *interrupted* task holds no claim and is
    nobody else's to run, so leaving it on UNKNOWN risks stalling the build
    outright — and UNKNOWN is not rare here: an executor that does not
    recognise the recorded ref's backend (a resident build reading a ref a
    Modal tick wrote, say) answers UNKNOWN for every task, forever.

    So this guard covers exactly the case it was built for — a backend
    retrying the same input under the same ref, which probes as RUNNING —
    and does not pretend to cover an unreachable backend.
    """
    executor_name, ref = item.latest_executor, item.latest_executor_ref
    if executor_name is None or ref is None:
        return DetachedExecutionStatus.UNKNOWN
    try:
        return await task_executor.detached_status(task, executor_name, ref)
    except Exception:
        logger.warning(
            f"Probing detached execution {ref!r} for task {task.id} raised; "
            "treating its status as unknown.",
            exc_info=True,
        )
        return DetachedExecutionStatus.UNKNOWN


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

    # RUNNING with a claim that has not lapsed: someone is executing it, and
    # their completion frees this build. Wait.
    executing: list[_BlockerVerdict]
    # Not running (so holding no claim, so carrying no expiry) and in a
    # status its owning build is still driving — that build is going to move
    # it. Wait.
    queued: list[_BlockerVerdict]
    # Nothing is going to move it and this build cannot reset it. Fail.
    fatal: list[_BlockerVerdict]
    # CANCELLED with attempt budget left: a revocation, not a verdict, so
    # this build's to run. Reset and schedule, don't fail — see
    # ``_classify_external_blockers``.
    recoverable: list[_BlockerVerdict]
    # A *result* (FAILED/SKIPPED), or a CANCELLED whose budget is spent: the
    # outcome belongs to ``fail_mode``, which already sees it in
    # ``status_counts``, so it must not influence the wait-or-fail decision.
    # Kept only to enrich the message.
    inert: list[_BlockerVerdict]

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


# Blocker statuses whose fate belongs to the build that owns them. None of
# them holds an execution claim, so the server clears the expiry with the
# claim and the owner's status is the only liveness evidence that exists:
# PENDING means that build has not scheduled it yet, SUSPENDED means that
# build is working through its dynamic dependencies, INTERRUPTED means the
# platform took its execution away and that build is due to start it again.
# All three are transient while the owner lives — and all three are a
# permanent wedge once it dies.
#
# INTERRUPTED belongs with these and NOT with the inert results: it is not
# a verdict on the task, so failing this build over it would be failing
# over a neighbour's timeout.
_OWNER_DRIVEN_STATUSES = ("pending", "suspended", "interrupted")


async def _classify_external_blockers(
    frontier: BuildFrontier,
    now: datetime,
    *,
    registry: RegistryABC,
    config: TickConfig,
) -> _ExternalBlockers:
    """Split the frontier's external blockers into reset / wait / fail / ignore.

    The frontier reports these only when the build has nothing actionable
    and nothing running — precisely the state the stuck-build check reads as
    "this build is dead". Each entry is an upstream of one of this build's
    tasks whose current status *another* build produced. The split decides,
    per blocker, whether anyone — this build included — is going to move it.

    **Plan membership is not one of the questions.** What decides is the
    blocker's *status*, because the status is what says whether it is a
    revocation, a result, or work in flight. A build's plan is closed under the
    dependency relation, so a gating upstream is *usually* this build's own
    task — but closure runs once, at registration, and an edge written after
    that is not in the plan. A concurrent build's worker yielding dynamic
    dependencies does exactly that, routinely. Such a blocker is still decided
    here, on the same evidence as any other, and the attempt budget is what
    stops the CANCELLED branch acting on one (see below). See
    ``docs/design/execution-claims-and-liveness.md``.

    **CANCELLED — reset it and run it.** A cancel releases the execution
    claim (that is the whole point of the fail-fast cascade) and leaves the
    task in a status nothing schedules. It revokes *permission to run*; it is
    not a verdict on the task, and permission is not build-scoped — a task in
    this build's plan is this build's to run, whatever build last touched it.
    Without this, one build's fail-fast became every overlapping build's
    failure. Bounded by the same per-task attempt budget an ordinary retry
    obeys, so a task that fails on every attempt cannot loop here. A blocker
    outside the plan excludes itself without a plan check: the server reports
    no attempt count for one, and a missing count refuses the retry (see
    :func:`_retry_allowed`).

    **FAILED / SKIPPED — leave them.** A failure is a *result*, and results
    belong to ``fail_mode``: FAIL_FAST has already failed the build on the
    same count, and CONTINUE means "finish what you can, then fail". A tick
    that reset them would override the policy the user chose, and would do it
    on nobody's request. They go to ``inert`` — named in the failure message,
    influencing nothing. A re-trigger, where the user *did* ask, resets the
    whole retryable set (see ``_RETRYABLE_STATUSES``).

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

    **PENDING / SUSPENDED — ask the owning build** (``_OWNER_DRIVEN_STATUSES``).
    Neither holds a claim, so the server clears the expiry with it and there
    is nothing to read. The wedge is real — a task abandoned SUSPENDED blocks
    every downstream build — so this half still decides, the only way it can:

    - owning build still live → **wait**; it is going to move it. Without
      this a SUSPENDED shared task would fail every *other* build that
      depends on it while the owner is legitimately mid-flight through its
      dynamic dependencies, and a PENDING dynamic dependency of a healthy
      concurrent build would do the same.
    - owning build terminal → **fail**; nothing will move it.
    - ``blocking_status_build_id is None`` → **fail** without a lookup: no
      status-moving event was ever recorded against it, so there is nobody
      to ask.
    - owner status unresolvable (deleted build, unreachable registry, a
      registry that doesn't report it) → **fail**: unknown is not evidence
      of life, and a silent indefinite hang is the failure mode #208 exists
      to kill.

    So the owning-build lookup earns its place, for these statuses **only** —
    not out of caution but because it is the only evidence that exists for a
    status carrying no claim. What a live owner does not buy is a deadline: a
    build gone silent without transitioning is reaped server-side, not
    guessed at here from a task's age.

    **Why waiting on a SUSPENDED blocker is bounded**, and not the open-ended
    hang it looks like: SUSPENDED persists only while the owner progresses the
    dynamic dependencies it yielded, or while the owner is itself stuck on a
    RUNNING task — and a RUNNING task carries a claim with an expiry. Once
    that lapses the owner recovers or fails it, ``skip_blocked`` moves the
    suspended parent to SKIPPED, the owner goes terminal, and this build stops
    waiting. The wait ends on the same bound everything else here uses.
    """
    executing: list[_BlockerVerdict] = []
    queued: list[_BlockerVerdict] = []
    fatal: list[_BlockerVerdict] = []
    recoverable: list[_BlockerVerdict] = []
    inert: list[_BlockerVerdict] = []

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
        if blocker.blocking_status == "cancelled":
            # A revocation, not a verdict: the cancel released the claim, and
            # a task in this build's plan is this build's to run whatever
            # build last touched it. Budget-bounded, and the budget also
            # excludes an out-of-plan blocker, which has no attempt count.
            if _retry_allowed(blocker.blocking_attempt_count, config.max_attempts):
                recoverable.append(
                    _BlockerVerdict(blocker, "cancelled, so this build's to reset")
                )
            else:
                inert.append(
                    _BlockerVerdict(
                        blocker,
                        "cancelled, but not resettable from here (its attempt "
                        "budget in this build is spent, or this build has no "
                        "attempts on it to count)",
                    )
                )
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
                        "claim is abandoned and re-claimable — but a RUNNING "
                        "task is not schedulable until someone releases it",
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

        if blocker.blocking_status not in _OWNER_DRIVEN_STATUSES:
            # A result — FAILED or SKIPPED — or a status no build drives at
            # all (an UNREGISTERED phantom, or one a future server adds).
            # Nothing is going to move it, but the decision is not this
            # path's to take: it is in this build's plan, so it is in
            # ``status_counts``, and ``fail_mode`` owns what happens to a
            # failure. Recorded for the message only.
            inert.append(
                _BlockerVerdict(
                    blocker,
                    "a result rather than a revocation, so this build's "
                    "fail_mode owns the outcome",
                )
            )
            continue

        # PENDING or SUSPENDED: no claim is held, so there is no expiry to
        # read. It moves only if the build owning its status is still going to
        # move it.
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
        executing=executing,
        queued=queued,
        fatal=fatal,
        recoverable=recoverable,
        inert=inert,
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
                else f" for {_format_age(age)}"
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


def _blocker_remedy(verdicts: Sequence[_BlockerVerdict]) -> str:
    """How to get out of it — the part the error used to lack.

    One remedy, because one covers it: re-triggering this build re-runs
    discovery, which closes the plan again over whatever edges exist *now* and
    resets the retryable set — so it reaches a blocker that was outside the
    plan as well as one inside it. The exception is a RUNNING blocker, which
    holds a claim no reset can take.

    Spelled in the **surfaces a user actually has** — the UI and the CLI — not
    as the REST routes underneath them. This text lands in a build's
    ``error_message``, read by someone whose build just died; a bare
    ``POST /api/v1/...`` leaves them to find a base URL, mint a token and
    assemble a body before they can act on it.

    The UI is named first because it cannot get the build id wrong: the
    scheduling panel addresses a claim action to the blocker's
    ``blocking_status_build_id``. That matters more than it looks. Any build in
    the environment is accepted by the route — it does not require the task to
    be in it — but the id given becomes the task's ``latest_status_build_id``,
    so cancelling under the *stuck* build makes that build the owner of the
    CANCELLED status. The frontier then stops reporting the task as an external
    blocker at all, so the very reset this message is steering towards never
    happens and the build has to be re-triggered anyway. Under the owner, the
    next tick resets it and runs it — which is why the CLI form spells the
    argument ``<owning-build-id>`` and says which build that is.
    """
    remedy = (
        "Re-trigger this build to reset the blocker and run it here — a "
        "trigger resets failed/cancelled/skipped/suspended tasks in the plan, "
        "which a mid-flight tick deliberately does not. Trigger it with this "
        "same build id: build_trigger(..., build_id=<this build>, "
        "reactive=True). A task-level Retry (in the UI, or 'stardag tasks "
        "retry') is not the same thing and will not do it"
    )
    if any(
        verdict.blocker.blocking_status in _RUNNING_STATUSES for verdict in verdicts
    ):
        remedy += (
            ". A blocker stuck RUNNING holds an execution claim no reset can "
            "take — and while a lapsed claim is re-claimable, no build is "
            "claiming it, so release it first: in the UI, open the blocking "
            "build's scheduling panel and use the blocker's 'Release claim' "
            "action, which addresses it to the owning build for you; from the "
            "CLI, 'stardag tasks cancel <owning-build-id> "
            "<blocking-task-id>'. It has to be the build that owns the blocker "
            "(named above), not this one — cancelling it under this build "
            "would make this build the owner of the cancelled status, which "
            "stops the next tick from picking the task up"
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
        blockers = await _classify_external_blockers(
            frontier, now, registry=registry, config=config
        )

        # Recoverable (cancelled) blockers first: this build can reset them
        # and run them itself, so there is nothing to wait for and nothing to
        # fail on. Done before the wait/fail decision because it *removes*
        # the reason for both — the next tick finds them actionable.
        if blockers.recoverable:
            # Deduplicated, because the frontier reports one entry per
            # (blocked, blocker) *edge*: a shared upstream appears once for
            # every task of this build that depends on it, which is the normal
            # shape for the fan-out this path exists to unblock. Without the
            # dedupe a diamond retries the same task N times — the second call
            # hitting a row that is already PENDING, so it fails and logs — and
            # ``in_build_blockers_reset`` counts edges rather than tasks.
            # ``dict.fromkeys`` rather than a set: registration order is what
            # makes the log line's truncated list stable.
            reset_ids = list(
                dict.fromkeys(v.blocker.blocking_task_id for v in blockers.recoverable)
            )
            for task_id in reset_ids:
                try:
                    await registry.task_retry_by_id_aio(build_id, task_id)
                except Exception as e:
                    # Best-effort: another tick may have reset it already, or
                    # completed it outright. Either way the next frontier read
                    # tells the truth, and failing the build over a lost race
                    # is the outcome this whole path exists to avoid.
                    logger.warning(f"Could not reset in-build blocker {task_id}: {e}")
            summary.in_build_blockers_reset += len(reset_ids)
            logger.info(
                f"Build {build_id}: reset {len(reset_ids)} cancelled blocker(s) "
                f"in this build's own plan so this build can run them: "
                f"{', '.join(reset_ids[:_MAX_REPORTED_BLOCKERS])}"
            )
            return None
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
            # is not going to move it — plus how to get it moving. The status
            # counts alone (the whole of the old message) point nowhere near
            # the cause when the cause is an upstream in a status no tick of
            # this build will touch.
            reason = (
                f"Build cannot progress: it has nothing runnable or running "
                f"of its own, and {len(blockers.fatal)} of its task(s) are "
                "blocked by an upstream that nothing is going to move "
                f"(status counts: {counts}). Blocked by: "
                f"{_describe_blockers(blockers.fatal, now, truncated)}"
                f". {_blocker_remedy(blockers.fatal)}"
            )
        else:
            # Nothing to wait for and nothing fatal: genuinely stuck (failed
            # deps in CONTINUE mode, a lost task pickle, a blocker whose
            # status is a result this tick will not override). Fail rather
            # than idle forever — naming the inert blockers, whose role in it
            # the status counts do not reveal.
            reason = (
                "No runnable or running tasks left but roots are not "
                f"complete (status counts: {counts})"
            )
            if blockers.inert:
                reason += ". Blocked by: " + _describe_blockers(
                    blockers.inert, now, truncated
                )
                reason += f". {_blocker_remedy(blockers.inert)}"
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

    **INTERRUPTED tasks are included, and for both of those reasons.** Such
    a task may still have a live execution — that is the whole premise of
    the backend-retry guard in ``_act_on_frontier`` — so a build that dies
    without cancelling it leaves a container running that nobody is waiting
    for. And left INTERRUPTED under a terminal build it is a permanent
    wedge for every *other* build gated on it: ``_OWNER_DRIVEN_STATUSES``
    reads interrupted as "the owner will move it", so a neighbour waits and
    then fails, where a CANCELLED task would have been reset and run. The
    argument is the one ``CASCADE_CANCEL_STATUSES`` already makes for
    SUSPENDED, word for word.

    Known gap: an interrupted task whose upstream is incomplete again (a
    dynamic dependency registered after it ran) is in neither ``running``
    nor ``actionable``, so nothing here reaches it. Narrow, and the
    server-side cascade — which queries the task table rather than the
    frontier — closes it whenever the build is cancelled through the API.
    """
    cancellable = _RUNNING_STATUSES + (_INTERRUPTED_STATUS,)
    # Re-read, because the snapshot the caller holds is the PRE-action one.
    # ``_act_on_frontier`` has already run by the time terminal handling
    # decides to cancel, so that snapshot can be wrong in both directions:
    # a task it resumed or spawned this pass is live under a ref the
    # snapshot has never seen, and a task the snapshot lists as INTERRUPTED
    # may now be RUNNING under a *different* ref.
    #
    # Acting on the stale copy is not merely incomplete, it is harmful:
    # cancelling the old ref is a no-op while the TASK_CANCELLED it records
    # releases the claim on the execution that just started — handing the
    # task to any other build while a container is still writing its
    # target. One extra read on a path that runs once, at build death.
    try:
        frontier = await registry.build_get_frontier_aio(build_id)
    except Exception as e:
        logger.warning(
            f"Could not re-read the frontier of build {build_id} before "
            f"cancelling ({e}); falling back to the pre-action snapshot, "
            "which may miss executions started in this pass."
        )
    running_items = list(frontier.running or [])
    seen = {item.task_id for item in running_items}
    running_items += [
        item
        for item in frontier.actionable
        if item.latest_status in cancellable and item.task_id not in seen
    ]
    for item in running_items:
        if (
            item.latest_status in cancellable
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
