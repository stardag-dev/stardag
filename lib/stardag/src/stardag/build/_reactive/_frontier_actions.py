"""One pass over a frontier: discovery jobs, then claims and spawns.

The frontier (design.md, "The runnable rule") lists three kinds of member
of the build's active plan, after the server's closure step:

- **discovery jobs** — never expanded under this scope. The tick
  rehydrates the instance body, evaluates ``requires()`` (the walk stops at
  complete tasks) and registers the result through ``/members``, which sets
  the closure flag. A job that fails — the class cannot be imported here, a
  ``requires()`` that raises, a conflicting construction — **excludes the
  member** (``discovery_failed``): a property of this plan's code, not of
  the promise, and never re-selected.
- **runnable** — expanded, actionable, every upstream COMPLETED. The tick
  claims each (a client-minted execution id, the TTL from the executor's
  timeout, the limit keys computed from the instance), spawns the claimed
  execution detached, and records its ref with a non-claiming start. The
  frontier is a hint and the claim is the decision: a refusal is counted,
  not an error.
  An INTERRUPTED member whose ``interruptions`` reached
  ``TickConfig.max_interruptions`` is not spawned: the tick claims it and
  records a failure naming the count (the spawn-failure path, without the
  spawn), so the build's fail mode decides. A RUNNING member with a lapsed
  claim whose ``attempts`` reached ``TickConfig.max_executions`` is not
  taken over, the same way.
- **running** — under a live claim, whoever holds it. Nothing to do: a
  worker that dies stops reporting and its claim lapses, and a lapsed claim
  is listed as runnable and taken over by the next claiming start.
"""

from __future__ import annotations

import asyncio
import logging
import typing
from dataclasses import dataclass, field
from functools import partial
from uuid import UUID

from stardag import BaseTask, task_from_registry_data
import httpx

from stardag._core.rehydrate import TaskRehydrationError
from stardag.build._base import DetachedHandle, TaskExecutorABC
from stardag.build._claims import claim_ttl_seconds
from stardag.build._registration import (
    new_id,
    register_members_aio,
    run_bounded,
    run_bounded_settled,
    walk_aio,
)
from stardag.build._task_modules import import_failure_note
from stardag.exceptions import (
    APIError,
    RateLimitError,
    StardagError,
    execution_not_wanted,
)
from stardag.registry import BuildFrontier, FrontierMember, RegistryABC
from stardag.registry._api_http import transport_retry_counts

if typing.TYPE_CHECKING:
    from stardag.build._reactive._config import TickConfig, TickSummary

logger = logging.getLogger(__name__)

# Claim refusals that mean "not now" rather than "error".
_CLAIM_DENIED_CODES = frozenset(
    {
        "task_already_completed",
        "task_already_running",
        "upstream_incomplete",
        "member_excluded",
        "execution_superseded",
        # The build stopped (an operator cancel) after the frontier was read:
        # the next frontier shows it terminal, and the tick ends there.
        "build_not_running",
    }
)

# --- The per-pass spawn cap ------------------------------------------------
# A tick lives in a container with a finite life, so the cap is a duration
# budget: how many spawns fit in a fraction of that life.
_SPAWN_BUDGET_FRACTION = 0.25
# Wall-clock cost of one spawn: the claim, the spawn, the ref record — a
# pessimistic p99, because underestimating it inflates the cap.
_SECONDS_PER_SPAWN = 2.0
# No wall clock known anywhere: a plain number, never unbounded.
_DEFAULT_MAX_SPAWNS_PER_TICK = 500
_MIN_SPAWN_CAP = 50
_MAX_SPAWN_CAP = 10_000


@dataclass
class PassResult:
    """What one pass did."""

    acted: bool = False
    limit_denied: int = 0
    claim_denied: int = 0
    # A claim was refused ``plan_superseded``: a newer request replaced this
    # tick's plan, so the tick stops.
    superseded: bool = False
    # A member's spawn work raised: the members still queued take no new
    # claim (each would wait out the same outage or rate limit), while the
    # ones already holding a claim carry it through.
    failed: bool = False
    # The scheduler lease was lost during the pass: another tick may be
    # driving the build, so nothing further was claimed or registered.
    lease_lost: bool = False
    # The tick's time ran out during the pass: the remaining claims were left
    # to a successor (see ``act_on_frontier``'s ``out_of_time``).
    out_of_time: bool = False
    spawned: list[str] = field(default_factory=list)


class SpawnCap(typing.NamedTuple):
    """A per-pass spawn cap, and where it came from (for the log line)."""

    limit: int
    source: str


def _derived_cap(seconds: float, config: "TickConfig") -> int:
    derived = int(
        _SPAWN_BUDGET_FRACTION
        * seconds
        * max(1, config.max_concurrent_actions)
        / _SECONDS_PER_SPAWN
    )
    return max(_MIN_SPAWN_CAP, min(_MAX_SPAWN_CAP, derived))


def spawn_cap(
    candidates: typing.Sequence[BaseTask],
    task_executor: TaskExecutorABC,
    config: "TickConfig",
) -> SpawnCap:
    """How many executions this pass may spawn: the explicit
    ``max_spawns_per_tick``; else derived from this tick container's
    timeout; else from the tightest execution timeout among the candidates
    (a proxy); else a default."""
    if config.max_spawns_per_tick is not None:
        return SpawnCap(max(1, config.max_spawns_per_tick), "max_spawns_per_tick")
    if config.tick_timeout_seconds is not None:
        return SpawnCap(
            _derived_cap(config.tick_timeout_seconds, config),
            f"this tick's timeout ({config.tick_timeout_seconds:.0f}s)",
        )
    timeouts = []
    for task in candidates:
        try:
            timeout = task_executor.execution_timeout_seconds(task)
        except Exception:
            continue
        if timeout is not None:
            timeouts.append(timeout)
    if not timeouts:
        return SpawnCap(_DEFAULT_MAX_SPAWNS_PER_TICK, "the default")
    return SpawnCap(
        _derived_cap(min(timeouts), config),
        f"the tightest execution timeout ({min(timeouts):.0f}s)",
    )


def _rate_limited_count() -> int:
    """Rate-limit refusals this process has waited out so far."""
    return transport_retry_counts().get("HTTP 429", 0)


class Admission:
    """Adaptive admission of spawns per pass, for one tick's lifetime.

    Additive increase, multiplicative decrease, driven by the registry's
    rate limit: after a pass in which this process waited out any 429, the
    spawns a pass may admit are halved; after a pass without one they grow
    back by an eighth of the cap, until the cap applies alone again. Each
    spawn costs several writes over its life (the claim and ref record now,
    the worker's start and end and its wake-up later), so admitting fewer
    is what lets a workspace at its limit drain the work already running
    rather than starve it (STA-135). Without a limit -- a self-hosted
    registry by default -- no 429 is ever seen and the cap applies as is.

    The count is the process's, so a 429 met by another tick in the same
    container throttles this one too: they share a workspace, and so the
    limit. The state lives for one tick: a successor starts from the cap
    again, and a run of short ticks under a lasting limit is throttled
    only within each one (each spawn still waits out its own 429s).
    """

    def __init__(self) -> None:
        self.limit: int | None = None
        self._cap: int | None = None
        self._seen = _rate_limited_count()

    def apply(self, cap: int) -> int:
        """The spawns this pass may admit under ``cap``."""
        self._cap = cap
        return cap if self.limit is None else min(cap, self.limit)

    def after_pass(self) -> None:
        """Adjust from what the pass met."""
        count = _rate_limited_count()
        throttled = count > self._seen
        self._seen = count
        if self._cap is None:
            return
        if throttled:
            # From what the pass could actually admit: the cap can shrink
            # between passes (it follows the candidates' timeouts).
            current = self._cap if self.limit is None else min(self.limit, self._cap)
            self.limit = max(1, current // 2)
            logger.warning(
                f"The registry rate-limited this tick's process; admitting at "
                f"most {self.limit} spawns per pass until it stops."
            )
        elif self.limit is not None:
            self.limit += max(1, self._cap // 8)
            if self.limit >= self._cap:
                self.limit = None


LeaseLost = typing.Callable[[], bool]
OutOfTime = typing.Callable[[], bool]


def _never_lost() -> bool:
    return False


def _stop_for_lost_lease(lease_lost: LeaseLost, result: PassResult) -> bool:
    if result.lease_lost or lease_lost():
        result.lease_lost = True
        return True
    return False


def rehydrate(member: FrontierMember) -> BaseTask:
    """The task object of a member, from its instance body — the only way a
    tick gets one. Strict on the task id (a significant field this code
    reads differently is a different promise).

    Raises:
        TaskRehydrationError: The class is not registered in this process,
            or the body does not validate into it.
    """
    return task_from_registry_data(member.body, expected_task_id=member.task_id)


async def _exclude(
    registry: RegistryABC,
    plan_id: UUID,
    member: FrontierMember,
    error: BaseException,
    summary: "TickSummary",
) -> None:
    message = f"{type(error).__name__}: {error}{import_failure_note()}"
    logger.warning(
        f"Excluding task {member.task_id} from plan {plan_id}: its discovery "
        f"failed under this code ({message})."
    )
    await registry.member_discovery_failed_aio(plan_id, member.task_id, error=message)
    summary.excluded += 1


async def _discovery_job(
    member: FrontierMember,
    *,
    plan_id: UUID,
    registry: RegistryABC,
    config: "TickConfig",
    summary: "TickSummary",
    result: PassResult,
    lease_lost: "LeaseLost",
) -> None:
    """Expand one unexpanded member and register it with its closure.

    The lease is checked before the walk and again before every write the
    walk leads to (the registration, an exclusion): discovery runs user
    code and completion checks, and can outlive the lease.
    """
    if _stop_for_lost_lease(lease_lost, result):
        return
    try:
        task = rehydrate(member)
        walk = await walk_aio(
            [task], max_concurrent_discover=config.max_concurrent_discover
        )
    except (TaskRehydrationError, StardagError) as e:
        # The code under this scope cannot state this member (a class not
        # importable here, a requires() that raised, a conflicting or
        # unstable construction). A completion check that raised is an
        # outage and propagates: the next tick tries again.
        if _stop_for_lost_lease(lease_lost, result):
            return
        await _exclude(registry, plan_id, member, e, summary)
        return
    if _stop_for_lost_lease(lease_lost, result):
        return
    try:
        await register_members_aio(registry, plan_id, walk.items())
    except APIError as e:
        if e.code != "instance_conflict":
            raise
        if _stop_for_lost_lease(lease_lost, result):
            return
        await _exclude(registry, plan_id, member, e, summary)
        return
    summary.discovered += 1


def is_registry_outage(error: BaseException) -> bool:
    """Whether ``error`` is the registry not answering (unreachable, a 5xx,
    a rate limit outlasting the client's retries) rather than refusing."""
    if isinstance(error, (httpx.TransportError, RateLimitError)):
        return True
    # Not every 429: a creation quota (``QuotaExceededError``) is a refusal
    # that lasts the day, and flagging for it would only repeat it.
    return isinstance(error, APIError) and (error.status_code or 0) >= 500


async def _spawn_marking_failure(
    task: BaseTask, member: FrontierMember, *, result: PassResult, **kwargs: typing.Any
) -> None:
    """:func:`_spawn`, recording a failure on the pass so the members still
    queued behind it take no new claim."""
    try:
        await _spawn(task, member, result=result, **kwargs)
    except Exception:
        result.failed = True
        raise


async def _spawn(
    task: BaseTask,
    member: FrontierMember,
    *,
    plan_id: UUID,
    registry: RegistryABC,
    task_executor: TaskExecutorABC,
    config: "TickConfig",
    summary: "TickSummary",
    result: PassResult,
    lease_lost: "LeaseLost",
    out_of_time: "OutOfTime",
    granted: "_Granted | None" = None,
) -> None:
    """Claim one runnable member, spawn it, record its ref.

    Checks the lease on entry and again immediately before the claim (after
    the executor-metadata await): once it is lost, no new claim is taken.
    A claim already granted is carried through its spawn
    and ref — the claim, not the lease, is what makes the task's execution
    exclusive, and abandoning it would strand it until its TTL lapses.
    """
    if _stop_for_lost_lease(lease_lost, result):
        return
    # Before the metadata await too: once one spawn met the deadline, every
    # one queued behind it must stop here, not after a call that can be slow.
    if result.out_of_time or out_of_time():
        result.out_of_time = True
        return
    limit_keys = (
        list(config.limit_key_selector(task)) if config.limit_key_selector else []
    )
    try:
        metadata = await task_executor.get_executor_metadata(task)
    except Exception:
        metadata = None
    # Named on the claim, metadata or not: a claim with no executor reads as
    # an in-process execution until the spawn reports its ref (STA-90).
    try:
        executor_name = task_executor.detached_executor_name(task)
    except Exception:
        executor_name = None
    if _stop_for_lost_lease(lease_lost, result):
        return
    if result.failed or result.superseded:
        return
    # Last, right before the claim: a claim taken is carried through its
    # spawn, so this is the latest point the pass can still stop cheaply.
    if result.out_of_time or out_of_time():
        result.out_of_time = True
        return
    execution_id = new_id()
    try:
        await registry.member_start_aio(
            plan_id,
            member.task_id,
            execution_id=execution_id,
            claim=True,
            claim_ttl_seconds=claim_ttl_seconds(task, task_executor),
            executor=executor_name,
            executor_metadata=metadata,
            limit_keys=limit_keys,
        )
    except APIError as e:
        if e.code == "concurrency_limit_reached":
            result.limit_denied += 1
            summary.limit_denied += 1
            return
        if e.code == "plan_superseded":
            result.superseded = True
            return
        if e.code in _CLAIM_DENIED_CODES:
            result.claim_denied += 1
            summary.claim_denied += 1
            return
        raise
    result.acted = True
    if granted is not None:
        granted.count += 1
    handle: DetachedHandle | None = None
    error: BaseException | None = None
    for _attempt in range(max(1, config.max_attempts)):
        try:
            handle = await task_executor.submit_detached(
                task, execution_id=execution_id
            )
            break
        except Exception as e:
            error = e
            logger.warning(f"Spawning task {member.task_id} failed: {e}")
    if handle is None:
        summary.spawn_failed += 1
        try:
            await registry.member_fail_aio(
                plan_id,
                member.task_id,
                execution_id=execution_id,
                error_message=f"Spawn failed: {type(error).__name__}: {error}",
            )
        except Exception as e:
            # Nothing ran; the claim lapses on its own.
            logger.warning(
                f"Could not record the failed spawn of task {member.task_id} "
                f"(its claim lapses on its own): {e}"
            )
        return
    try:
        await registry.member_start_aio(
            plan_id,
            member.task_id,
            execution_id=execution_id,
            claim=False,
            executor=handle.executor,
            executor_ref=handle.ref,
            executor_metadata=handle.executor_metadata,
        )
    except Exception as e:
        if not (isinstance(e, APIError) and execution_not_wanted(e)):
            if not is_registry_outage(e):
                raise
            # The registry did not answer. The container is running and
            # records its own ref when it starts, so this is not an orphan,
            # and failing the pass would strand the claims of the siblings
            # it has not reached yet.
            logger.warning(
                f"Could not record the ref of task {member.task_id} "
                f"({handle.ref!r}); its worker records it when it starts: {e}"
            )
            summary.spawned += 1
            result.spawned.append(member.task_id)
            return
        # This execution is over before its ref was recorded — the claim
        # moved on (to another execution, or to another plan:
        # ``not_claim_holder``), or the ledger has no such execution: this
        # container is an orphan nothing else can find. Stop it.
        logger.warning(
            f"Task {member.task_id} stopped being this execution's during its "
            f"spawn ({e.code}); stopping {handle.ref!r}."
        )
        try:
            await task_executor.cancel_detached(task, handle.executor, handle.ref)
        except Exception as cancel_err:
            logger.warning(f"Could not stop {handle.ref!r}: {cancel_err}")
        summary.cancelled_refs += 1
        return
    summary.spawned += 1
    result.spawned.append(member.task_id)


# The claim a budget-exhausted member is failed under lives for one request.
_EXHAUSTED_CLAIM_TTL_SECONDS = 60


def interruptions_exhausted(member: FrontierMember, config: "TickConfig") -> bool:
    """Whether an interrupted member has spent its interruption budget.

    Only an INTERRUPTED member is gated: an operator's ``retry`` (which
    makes it PENDING) is honoured with one more execution, and an
    interruption of that one fails it again, since the count is over the
    whole build.
    """
    return (
        member.status == "interrupted"
        and member.interruptions >= config.max_interruptions
    )


def executions_exhausted(member: FrontierMember, config: "TickConfig") -> bool:
    """Whether a lapsed member has spent its execution budget.

    Only a takeover is gated: a runnable member listed ``running`` is one
    whose claim lapsed without a report. ``attempts`` counts every
    execution of the task under the build (the lapsed one included), so
    with ``max_executions=20`` the twentieth lapse fails it rather than
    starting a twenty-first. A first run (PENDING, no executions) and an
    operator's ``retry`` (PENDING again: the ledger is not reset) are not
    gated, so a retry is honoured with one more execution, and a lapse of
    that one fails it again, since the count is over the whole build.
    """
    return member.status == "running" and member.attempts >= config.max_executions


def exhausted_budget(member: FrontierMember, config: "TickConfig") -> str | None:
    """Which budget a runnable member has spent, if any: the failure
    message to record instead of running it, else ``None``."""
    if interruptions_exhausted(member, config):
        return (
            f"Interrupted {member.interruptions} times in this build, which "
            f"reaches TickConfig.max_interruptions={config.max_interruptions}; "
            "not restarted. `stardag tasks retry` runs it once more; a new "
            "build starts a new count."
        )
    if executions_exhausted(member, config):
        return (
            f"{member.attempts} executions in this build, the latest ending "
            "with its claim lapsed without a report (the worker died, or a "
            "preemption's restart never came), which reaches "
            f"TickConfig.max_executions={config.max_executions}; not taken "
            "over. `stardag tasks retry` runs it once more; a new build "
            "starts a new count."
        )
    return None


async def _fail_exhausted(
    member: FrontierMember,
    message: str,
    *,
    plan_id: UUID,
    registry: RegistryABC,
    summary: "TickSummary",
    result: PassResult,
    lease_lost: "LeaseLost",
    out_of_time: "OutOfTime",
) -> None:
    """Fail a member at a budget instead of running it again: a claim (the
    registry fails only the execution holding a task's claim; for a lapsed
    member this claim is the takeover, closing the lapsed execution
    ``taken_over``), then its failure with ``message``. No container is
    spawned. The build's fail mode takes it from there."""
    if _stop_for_lost_lease(lease_lost, result):
        return
    # A claim like any other: past the deadline it is the successor's.
    if result.out_of_time or out_of_time():
        result.out_of_time = True
        return
    execution_id = new_id()
    try:
        await registry.member_start_aio(
            plan_id,
            member.task_id,
            execution_id=execution_id,
            claim=True,
            claim_ttl_seconds=_EXHAUSTED_CLAIM_TTL_SECONDS,
        )
    except APIError as e:
        if e.code == "plan_superseded":
            result.superseded = True
            return
        if e.code in _CLAIM_DENIED_CODES:
            result.claim_denied += 1
            summary.claim_denied += 1
            return
        raise
    result.acted = True
    logger.error(f"Task {member.task_id} of plan {plan_id}: {message}")
    await registry.member_fail_aio(
        plan_id, member.task_id, execution_id=execution_id, error_message=message
    )
    if member.status == "interrupted":
        summary.interruptions_exhausted += 1
    else:
        summary.executions_exhausted += 1


async def act_on_frontier(
    frontier: BuildFrontier,
    *,
    registry: RegistryABC,
    task_executor: TaskExecutorABC,
    config: "TickConfig",
    summary: "TickSummary",
    lease_lost: "LeaseLost | None" = None,
    out_of_time: "OutOfTime | None" = None,
    admission: Admission | None = None,
) -> PassResult:
    """One pass: discovery jobs, then (if the plan still stands) claims and
    spawns of the runnable members, each phase bounded by
    ``max_concurrent_actions``. See the module docstring.

    ``lease_lost`` is the scheduler lease's loss signal, read before every
    action (a discovery job's registration, the seal, each claim): the
    lease is single-flight for ticks, and a renewal that fails or is
    refused mid-pass means another tick may already be acting. The pass
    then stops and returns ``lease_lost``.

    ``out_of_time`` is read before every claim, a budget failure's included,
    and before each spawn's executor-metadata call. Once it answers True,
    the pass takes no further claim and returns ``out_of_time``. It bounds the
    pass by the clock rather than by the spawn cap, which an explicit
    ``max_spawns_per_tick`` or the cap's floor can set beyond what is left
    of the tick's container time.
    """
    lost: LeaseLost = lease_lost or _never_lost
    timed_out: OutOfTime = out_of_time or _never_lost
    assert frontier.plan_id is not None
    plan_id = frontier.plan_id
    result = PassResult()
    semaphore = asyncio.Semaphore(max(1, config.max_concurrent_actions))

    if frontier.discovery_jobs:
        await run_bounded(
            [
                partial(
                    _discovery_job,
                    member,
                    plan_id=plan_id,
                    registry=registry,
                    config=config,
                    summary=summary,
                    result=result,
                    lease_lost=lost,
                )
                for member in frontier.discovery_jobs
            ],
            semaphore,
        )
        result.acted = True
        if result.lease_lost:
            return result
    elif not frontier.sealed:
        # Nothing left to expand, and the plan is not sealed: the driver
        # that registered it stopped before its seal. Seal it (the seal
        # verifies the static phase; a refusal leaves it to the next pass).
        if _stop_for_lost_lease(lost, result):
            return result
        try:
            await registry.plan_seal_aio(plan_id)
            result.acted = True
        except APIError as e:
            if e.code == "plan_superseded":
                result.superseded = True
                return result
            logger.info(f"Plan {plan_id} not sealable yet: {e}")

    exhausted = [
        (member, message)
        for member in frontier.runnable
        if (message := exhausted_budget(member, config)) is not None
    ]
    if exhausted:
        await run_bounded(
            [
                partial(
                    _fail_exhausted,
                    member,
                    message,
                    plan_id=plan_id,
                    registry=registry,
                    summary=summary,
                    result=result,
                    lease_lost=lost,
                    out_of_time=timed_out,
                )
                for member, message in exhausted
            ],
            semaphore,
        )
        if result.lease_lost or result.superseded or result.out_of_time:
            return result

    loaded: list[tuple[BaseTask, FrontierMember]] = []
    for member in frontier.runnable:
        if exhausted_budget(member, config) is not None:
            continue
        if _stop_for_lost_lease(lost, result):
            return result
        try:
            loaded.append((rehydrate(member), member))
        except TaskRehydrationError as e:
            await _exclude(registry, plan_id, member, e, summary)
            result.acted = True
    cap = spawn_cap([t for t, _ in loaded], task_executor, config)
    if len(loaded) > cap.limit:
        logger.info(
            f"Tick for build {frontier.build_id}: {len(loaded)} runnable, "
            f"spawning {cap.limit} this pass (cap from {cap.source})."
        )
        loaded = loaded[: cap.limit]
    # The admission limit counts *granted* claims, not attempts: a member
    # denied its claim (a concurrency limit, another holder) gives its place
    # to the next one, so a throttled pass is never left idle by a denial
    # at its head while admissible work waits behind it.
    budget = len(loaded) if admission is None else admission.apply(cap.limit)
    if budget < len(loaded):
        logger.info(
            f"Tick for build {frontier.build_id}: admitting {budget} of "
            f"{len(loaded)} spawns this pass (throttled by the registry's "
            "rate limit)."
        )
    pending = list(loaded)
    while pending and budget > 0:
        batch, pending = pending[:budget], pending[budget:]
        granted = _Granted()
        # Settled, not fail-fast: a claim taken is carried through its spawn
        # and ref even when a sibling fails (see ``run_bounded_settled``).
        await run_bounded_settled(
            [
                partial(
                    _spawn_marking_failure,
                    task,
                    member,
                    plan_id=plan_id,
                    registry=registry,
                    task_executor=task_executor,
                    config=config,
                    summary=summary,
                    result=result,
                    lease_lost=lost,
                    out_of_time=timed_out,
                    granted=granted,
                )
                for task, member in batch
            ],
            semaphore,
            # An outage among the failures decides how the tick exits (it
            # flags rather than hands off), so it is the one raised.
            prefer=is_registry_outage,
        )
        budget -= granted.count
        if result.lease_lost or result.superseded or result.out_of_time:
            break
    return result


class _Granted:
    """Claims granted in one batch of a pass (see ``act_on_frontier``)."""

    def __init__(self) -> None:
        self.count = 0
