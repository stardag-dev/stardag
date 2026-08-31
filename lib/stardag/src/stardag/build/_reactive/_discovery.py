from __future__ import annotations

import asyncio
import logging
import typing
from dataclasses import dataclass, field
from functools import partial
from typing import Callable, Coroutine, Sequence
from uuid import UUID

from stardag import (
    BaseTask,
    TaskStruct,
    flatten_task_struct,
)
from stardag.registry import (
    RegistryABC,
)

logger = logging.getLogger(__name__)


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
