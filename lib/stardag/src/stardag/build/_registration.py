"""Discovery and registration: the one static-phase path of every driver.

Every driver — the resident engines, the reactive bootstrap, a tick's
discovery job or rollover, and a worker's yield — does the same two things
(design.md, "Registration"):

1. **Walk** the DAG from some roots under its scope (:func:`walk_aio`). The
   walk stops at complete tasks and never evaluates their ``requires()``;
   it tracks instances by task id (:class:`SeenInstances`, raising
   :class:`~stardag.exceptions.InstanceConflictError` on two constructions
   of one task id) and checks each distinct instance's serialization is a
   fixed point (:func:`check_serialization_stability`), both before
   anything is sent. Completion checks run concurrently; the order is
   rebuilt afterwards by a sequential post-order pass, so nothing depends
   on which check answered first.
2. **Register** the walk as :class:`RegistrationItem` s: roots first via
   ``POST /builds/{id}/plans`` (unexpanded), then the post-order in chunks
   of at most 1000 via ``POST /plans/{id}/members``, then ``/seal``
   (:func:`register_plan_aio`); or, for a yield, one ``/yield`` batch
   (:func:`yield_batches`).

An item states what the driver saw: ``declared_upstreams`` (instance
hashes) for an expanded task, ``None`` for a pruned one;
``observed_complete`` and ``observed_at`` for its target. The registry
follows the world from those observations — marks a task COMPLETED, or
withdraws a completion whose target vanished.
"""

from __future__ import annotations

import asyncio
import logging
import typing
from collections.abc import Callable, Collection, Coroutine, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import partial
from uuid import UUID

import uuid6

from stardag._core.base_task import BaseTask, TaskStruct, flatten_task_struct
from stardag._core.instance import (
    SeenInstances,
    check_serialization_stability,
    extend_path,
)
from stardag.exceptions import APIError, StardagError
from stardag.registry import PlanInfo, RegistrationItem, RegistryABC
from stardag.target._freshness import begin_observation

logger = logging.getLogger(__name__)

#: The registry's per-chunk cap (``POST /plans/{id}/members``, ``/yield``).
MAX_CHUNK_ITEMS = 1000

#: Completion checks in flight during a walk. Bounded by the *target
#: backend's* tolerance, not the registry's: measured against a Modal volume
#: from a laptop, 16 finished a 64-task layer in ~26 s and 50 failed with
#: ResourceExhaustedError.
DEFAULT_MAX_CONCURRENT_DISCOVER = 16

#: Refusals of a retry that mean "nothing to reset": the task completed, or
#: another execution holds a live claim on it.
_RETRY_NOOP_CODES = frozenset({"task_already_completed", "task_already_running"})


class RequiresError(StardagError):
    """A task's ``requires()`` raised during a walk: a property of the code
    under this scope (the cause is chained), not an outage."""

    def __init__(self, task: BaseTask, cause: BaseException) -> None:
        super().__init__(
            f"{task.get_name()} {task.id}: requires() raised "
            f"{type(cause).__name__}: {cause}"
        )
        self.task_id = str(task.id)


def new_id() -> UUID:
    """A client-minted, time-ordered id (plans, executions, deployments,
    builds, yield batches)."""
    return UUID(str(uuid6.uuid7()))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _output_uri(task: BaseTask) -> str | None:
    """The task's target URI, best-effort (a URI is constructed, nothing is
    checked)."""
    target_method = getattr(task, "target", None)
    try:
        target = target_method() if callable(target_method) else None
    except Exception as e:
        logger.debug(f"Could not resolve the target of task {task.id}: {e}")
        return None
    uri = getattr(target, "uri", None)
    return uri if isinstance(uri, str) else None


def registration_item(
    task: BaseTask,
    *,
    declared_upstreams: Sequence[BaseTask] | None,
    observed_complete: bool,
    observed_at: datetime,
) -> RegistrationItem:
    """The registration item of one task object (one instance)."""
    return RegistrationItem(
        task_id=str(task.id),
        task_namespace=task.get_namespace(),
        task_name=task.get_name(),
        version=task.version,
        output_uri=_output_uri(task),
        instance_hash=str(task.instance_hash),
        body=task.instance_body(),
        declared_upstreams=None
        if declared_upstreams is None
        else [str(dep.instance_hash) for dep in declared_upstreams],
        observed_complete=observed_complete,
        observed_at=observed_at,
    )


# -----------------------------------------------------------------------------
# The walk
# -----------------------------------------------------------------------------


@dataclass
class Walk:
    """What a walk found. ``order`` is post-order (upstreams first), each
    task id once; ``deps`` holds the static upstreams of every task the walk
    expanded (an absent task was pruned at, complete)."""

    roots: list[BaseTask]
    order: list[BaseTask] = field(default_factory=list)
    complete: dict[UUID, bool] = field(default_factory=dict)
    deps: dict[UUID, list[BaseTask]] = field(default_factory=dict)
    observed_at: dict[UUID, datetime] = field(default_factory=dict)
    seen: SeenInstances = field(default_factory=SeenInstances)

    @property
    def incomplete(self) -> list[BaseTask]:
        return [t for t in self.order if not self.complete[t.id]]

    @property
    def previously_completed(self) -> list[BaseTask]:
        return [t for t in self.order if self.complete[t.id]]

    def item(self, task: BaseTask, *, expanded: bool = True) -> RegistrationItem:
        return registration_item(
            task,
            declared_upstreams=self.deps.get(task.id) if expanded else None,
            observed_complete=self.complete[task.id],
            observed_at=self.observed_at[task.id],
        )

    def items(self, tasks: Sequence[BaseTask] | None = None) -> list[RegistrationItem]:
        """Items for ``tasks`` (default: the whole walk), in walk order."""
        return [self.item(t) for t in (self.order if tasks is None else tasks)]

    def root_items(self) -> list[RegistrationItem]:
        """The roots, unexpanded — what ``POST /builds/{id}/plans`` admits."""
        unique = {r.id: r for r in self.roots}
        return [self.item(r, expanded=False) for r in unique.values()]


_Factory = Callable[[], Coroutine[typing.Any, typing.Any, typing.Any]]


def _first_leaf(error: BaseException) -> BaseException:
    inner = getattr(error, "exceptions", None)
    return _first_leaf(inner[0]) if inner else error


async def run_concurrently(factories: Sequence[_Factory]) -> None:
    """Run the factories' coroutines concurrently and wait for all of them;
    a failure surfaces as itself (the first leaf of the group, its own cause
    kept; the group is its context). Factories rather than coroutines, so
    nothing is constructed that a cancellation could leave un-awaited."""
    if not factories:
        return
    try:
        async with asyncio.TaskGroup() as group:
            for factory in factories:
                group.create_task(factory())
    except BaseExceptionGroup as group_error:  # noqa: F821
        raise _first_leaf(group_error)


async def run_bounded(
    factories: Sequence[_Factory], semaphore: asyncio.Semaphore
) -> None:
    """:func:`run_concurrently` with ``semaphore`` held per coroutine."""

    async def one(factory: _Factory) -> None:
        async with semaphore:
            await factory()

    await run_concurrently([partial(one, f) for f in factories])


async def walk_aio(
    roots: TaskStruct,
    *,
    max_concurrent_discover: int = DEFAULT_MAX_CONCURRENT_DISCOVER,
    register_all: bool = False,
    seen: SeenInstances | None = None,
    prior: Walk | None = None,
    root_path: str | None = None,
    check_stability: bool = True,
) -> Walk:
    """Walk ``roots``' DAG under the current scope; see the module docstring.

    Args:
        roots: The task objects to start from.
        max_concurrent_discover: Completion checks in flight.
        register_all: Expand complete tasks too (a resident build that wants
            every edge recorded); the default stops at them.
        seen: The instances already seen in this build's discovery, shared
            across walks so a later walk (a yield) detects a conflict with an
            earlier one.
        prior: An earlier walk of this build; its completion and expansion
            results are reused rather than recomputed.
        root_path: The construction path of the task that reached ``roots``
            (a yielding parent), for conflict messages.
        check_stability: Run the serialization round trip once per distinct
            instance.

    Raises:
        InstanceConflictError: Two constructions of one task id differ.
        UnstableSerializationError: An instance body is not a fixed point.
        RequiresError: A task's ``requires()`` raised.

    A completion check that raises (the target backend is unreachable)
    propagates as itself: an outage, not a verdict on the task.
    """
    # Every completion answered below must be at least as fresh as this
    # walk (see ``stardag.target._freshness``).
    begin_observation()
    root_list = flatten_task_struct(roots)
    walk = Walk(roots=root_list, seen=seen if seen is not None else SeenInstances())
    lock = asyncio.Lock()
    visited: set[UUID] = set()
    semaphore = asyncio.Semaphore(max(1, max_concurrent_discover))

    async def visit(task: BaseTask, parent_path: str | None) -> None:
        path = extend_path(parent_path, task)
        async with lock:
            first = walk.seen.observe(task, path)
            if task.id in visited:
                return
            visited.add(task.id)
        if first and check_stability:
            check_serialization_stability(task)
        if prior is not None and task.id in prior.complete:
            complete = prior.complete[task.id]
            walk.observed_at[task.id] = prior.observed_at[task.id]
        else:
            async with semaphore:
                complete = await task.complete_aio()
            walk.observed_at[task.id] = _now()
        walk.complete[task.id] = complete
        if complete and not register_all:
            return
        if prior is not None and task.id in prior.deps:
            deps = prior.deps[task.id]
        else:
            try:
                deps = flatten_task_struct(task.requires())
            except Exception as e:
                raise RequiresError(task, e) from e
        walk.deps[task.id] = deps
        await run_concurrently([partial(visit, dep, path) for dep in deps])

    await run_concurrently([partial(visit, root, root_path) for root in root_list])

    emitted: set[UUID] = set()

    def emit(task: BaseTask) -> None:
        if task.id in emitted:
            return
        emitted.add(task.id)
        for dep in walk.deps.get(task.id, ()):
            emit(dep)
        walk.order.append(task)

    for root in root_list:
        emit(root)
    return walk


# -----------------------------------------------------------------------------
# Registration
# -----------------------------------------------------------------------------


def chunks(items: Sequence[RegistrationItem], size: int = MAX_CHUNK_ITEMS):
    for start in range(0, len(items), size):
        yield list(items[start : start + size])


async def _retry_members_aio(
    registry: RegistryABC,
    plan_id: UUID,
    task_ids: Sequence[str],
    semaphore: asyncio.Semaphore,
) -> None:
    """Reset members to PENDING (a re-trigger's retry of what failed before);
    a member that completed, or that someone runs, needs nothing."""

    async def one(task_id: str) -> None:
        try:
            await registry.member_retry_aio(plan_id, task_id)
        except APIError as e:
            if e.code not in _RETRY_NOOP_CODES:
                raise

    await run_bounded([partial(one, t) for t in task_ids], semaphore)


async def register_members_aio(
    registry: RegistryABC,
    plan_id: UUID,
    items: Sequence[RegistrationItem],
    *,
    retry_failed: bool = False,
    chunk_size: int = MAX_CHUNK_ITEMS,
    max_concurrent: int = DEFAULT_MAX_CONCURRENT_DISCOVER,
) -> None:
    """Register ``items`` (post-order) in sequential chunks.

    Sequential, because chunk *n* may hold the upstreams of chunk *n+1*.
    With ``retry_failed``, the not-complete members of a chunk that did not
    consist only of new tasks are reset to PENDING afterwards — a task that
    failed (or was cancelled, skipped, suspended, interrupted) before gets
    another run under this request.
    """
    semaphore = asyncio.Semaphore(max(1, max_concurrent))
    for chunk in chunks(items, chunk_size):
        result = await registry.plan_register_members_aio(plan_id, chunk)
        if retry_failed and result.tasks_created < len(chunk):
            await _retry_members_aio(
                registry,
                plan_id,
                [i.task_id for i in chunk if not i.observed_complete],
                semaphore,
            )


async def register_plan_aio(
    registry: RegistryABC,
    build_id: UUID,
    walk: Walk,
    *,
    deployment_id: UUID,
    settings: typing.Mapping[str, str],
    retry_failed: bool = False,
    chunk_size: int = MAX_CHUNK_ITEMS,
) -> PlanInfo:
    """The static phase: roots first, the walk in chunks, then seal.

    Lookup-or-create by ``(build, scope)``: a build re-triggered under the
    same scope gets its existing plan back and every step is a no-op except
    the observations, which are re-sent (a vanished output is invalidated,
    and with ``retry_failed`` a failed member is reset). A crash at any
    point is recoverable — the roots land first, unexpanded, so any tick
    finds them as discovery jobs.

    Returns the sealed plan.

    Raises:
        APIError: A refusal (``root_instance_conflict``,
            ``instance_conflict``, ``plan_superseded``,
            ``deployment_not_current``, ...).
    """
    plan = await registry.plan_create_aio(
        build_id,
        plan_id=new_id(),
        deployment_id=deployment_id,
        settings=settings,
        roots=walk.root_items(),
    )
    await register_members_aio(
        registry,
        plan.id,
        walk.items(),
        retry_failed=retry_failed,
        chunk_size=chunk_size,
    )
    return await registry.plan_seal_aio(plan.id)


# -----------------------------------------------------------------------------
# Yields
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class YieldBatch:
    """One request of a yield: ``items`` go to ``/members`` when ``yielded``
    is empty (closure that does not fit beside the children), otherwise to
    ``/yield``."""

    items: list[RegistrationItem]
    yielded: list[str]
    suspend: bool


def yield_batches(
    walk: Walk,
    children: Sequence[BaseTask],
    *,
    suspend: bool,
    known: Collection[UUID] = (),
    chunk_size: int = MAX_CHUNK_ITEMS,
) -> list[YieldBatch]:
    """Split one yield into requests.

    Normally one ``/yield`` carrying the children and their static closure
    (post-order). A closure larger than a chunk is registered first through
    ``/members`` (upstreams before their downstreams, as a static chunk),
    and the children follow in ``/yield`` batches; only the last carries
    ``suspend``. Tasks in ``known`` (already registered into the plan by
    this driver) are not re-sent unless they are children themselves.
    """
    child_ids = {c.id for c in children}
    closure = [t for t in walk.order if t.id not in child_ids and t.id not in known]
    unique_children = list({c.id: c for c in children}.values())
    if len(closure) + len(unique_children) <= chunk_size:
        items = walk.items(
            [t for t in walk.order if t.id in child_ids or t.id not in known]
        )
        return [
            YieldBatch(
                items=items,
                yielded=[str(c.instance_hash) for c in unique_children],
                suspend=suspend,
            )
        ]
    batches = [
        YieldBatch(items=chunk, yielded=[], suspend=False)
        for chunk in chunks(walk.items(closure), chunk_size)
    ]
    child_items = walk.items(unique_children)
    child_chunks = list(chunks(child_items, chunk_size))
    for index, chunk in enumerate(child_chunks):
        batches.append(
            YieldBatch(
                items=chunk,
                yielded=[i.instance_hash for i in chunk],
                suspend=suspend and index == len(child_chunks) - 1,
            )
        )
    return batches


async def send_yield_aio(
    registry: RegistryABC,
    batches: Sequence[YieldBatch],
    *,
    plan_id: UUID,
    task_id: str,
    execution_id: UUID,
    deployment_id: UUID,
) -> None:
    """Send a yield's batches in order (see :func:`yield_batches`). Each
    ``/yield`` batch gets its own client-minted ``batch_id``, so a retry
    after a lost response is replayed rather than refused."""
    for batch in batches:
        if not batch.yielded:
            await registry.plan_register_members_aio(plan_id, batch.items)
            continue
        await registry.member_yield_aio(
            plan_id,
            task_id,
            execution_id=execution_id,
            deployment_id=deployment_id,
            batch_id=new_id(),
            items=batch.items,
            yielded=batch.yielded,
            suspend=batch.suspend,
        )


def send_yield(
    registry: RegistryABC,
    batches: Sequence[YieldBatch],
    *,
    plan_id: UUID,
    task_id: str,
    execution_id: UUID,
    deployment_id: UUID,
) -> None:
    """The sync form of :func:`send_yield_aio` (a Modal worker's reporter)."""
    for batch in batches:
        if not batch.yielded:
            registry.plan_register_members(plan_id, batch.items)
            continue
        registry.member_yield(
            plan_id,
            task_id,
            execution_id=execution_id,
            deployment_id=deployment_id,
            batch_id=new_id(),
            items=batch.items,
            yielded=batch.yielded,
            suspend=batch.suspend,
        )
