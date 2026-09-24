"""One chunk of registration: the per-item steps, in one transaction.

The body of every registration route (``create_plan``'s roots, ``POST
/plans/{id}/members``, and from step 3 ``/yield``), and of the closure
step's admission. See ``registration.py`` for the locking rules and
design.md, "Registration", for the steps.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.models import (
    AdmittedBy,
    EventType,
    Plan,
    PlanMember,
    Task,
    TaskInstance,
    TaskInstanceDependency,
    TaskStatus,
)
from stardag_api.models.base import generate_uuid7
from stardag_api.schemas_v2 import RegistrationItem
from stardag_api.services import event_log
from stardag_api.services.errors import BadRequest, Conflict
from stardag_api.services.event_log import EventClock
from stardag_api.services.transitions import (
    Transition,
    TransitionKind,
    transition_task,
)

#: How far a driver's ``observed_at`` may run ahead of the server's clock.
#: Forward skew is the only direction that could pass the invalidation guard
#: wrongly; backward skew only makes an observation look older.
CLOCK_SKEW_TOLERANCE = timedelta(seconds=5)


@dataclass(frozen=True)
class MembersResult:
    """What one chunk changed. Every count is zero on a re-delivery."""

    tasks_created: int = 0
    instances_created: int = 0
    members_admitted: int = 0
    closure_admitted: int = 0
    edges_created: int = 0
    completed: int = 0
    invalidated: int = 0
    diverged: int = 0


def differing_fields(a: Mapping[str, Any], b: Mapping[str, Any]) -> list[str]:
    """Top-level body fields whose values differ (what a conflict names)."""
    return sorted(k for k in a.keys() | b.keys() if a.get(k) != b.get(k))


def _sorted_items(items: Iterable[RegistrationItem]) -> list[RegistrationItem]:
    """De-duplicated by instance hash, ordered by ``(task_id, instance_hash)``.

    Only an exact repeat is de-duplicated. The driver de-duplicates by
    instance, so two differing items under one hash are a client bug:
    another body is ``instance_body_conflict`` (409), and any other
    difference — another ``declared_upstreams`` above all, whose edges
    keeping the first would silently drop — is 400 ``duplicate_item``.
    """
    by_hash: dict[str, RegistrationItem] = {}
    for it in items:
        seen = by_hash.get(it.instance_hash)
        if seen is None:
            by_hash[it.instance_hash] = it
        elif seen.body != it.body or seen.task_id != it.task_id:
            raise Conflict(
                "instance_body_conflict",
                "one chunk carries two bodies under one instance hash",
                instance_hash=it.instance_hash,
            )
        elif fields := _item_differences(seen, it):
            raise BadRequest(
                "duplicate_item",
                f"one chunk carries instance {it.instance_hash} twice, with"
                f" different {', '.join(fields)}; send each instance once",
                instance_hash=it.instance_hash,
                fields=fields,
            )
    return sorted(by_hash.values(), key=lambda it: (it.task_id, it.instance_hash))


def _item_differences(a: RegistrationItem, b: RegistrationItem) -> list[str]:
    """Fields two items under one instance hash disagree on; upstreams are
    compared as sets (their order carries nothing). ``observed_at`` is not
    compared: two looks at one target a moment apart are one observation."""

    def norm(it: RegistrationItem) -> dict[str, Any]:
        d = it.model_dump(exclude={"observed_at"})
        ups = d["declared_upstreams"]
        d["declared_upstreams"] = None if ups is None else sorted(set(ups))
        return d

    return differing_fields(norm(a), norm(b))


def _instance_conflict(
    task_id: str, held: Mapping[str, Any], other: Mapping[str, Any], **detail: Any
) -> Conflict:
    fields = differing_fields(held, other)
    return Conflict(
        "instance_conflict",
        f"the plan already holds another instance of task {task_id}"
        f" (fields that differ: {', '.join(fields) or '-'})",
        task_id=task_id,
        fields=fields,
        **detail,
    )


@dataclass
class _Chunk:
    """Working state of one chunk registration."""

    plan: Plan
    items: list[RegistrationItem]
    clock: EventClock
    task_pk: dict[str, UUID] = field(default_factory=dict)
    task_status: dict[str, TaskStatus] = field(default_factory=dict)
    tasks_created: set[str] = field(default_factory=set)
    instance_id: dict[str, UUID] = field(default_factory=dict)
    # Instance ids already expanded before this chunk (divergence candidates).
    pre_expanded: set[UUID] = field(default_factory=set)
    # Existing instances this chunk expands for the first time.
    to_expand: list[UUID] = field(default_factory=list)
    instances_created: int = 0
    events: list[dict[str, Any]] = field(default_factory=list)


async def register_items(
    session: AsyncSession,
    environment_id: UUID,
    plan: Plan,
    items: Sequence[RegistrationItem],
    *,
    as_roots: bool,
    now: datetime,
) -> MembersResult:
    _check_clock_skew(items, now)
    chunk = _Chunk(plan=plan, items=_sorted_items(items), clock=EventClock(now))
    if not chunk.items:
        return MembersResult()

    await _insert_tasks(session, environment_id, chunk)
    await _insert_instances(session, environment_id, chunk)
    await _check_sealed(session, chunk)
    admitted = await _admit_items(session, environment_id, chunk, as_roots=as_roots)
    edges_created, diverged, closure_admitted = await _insert_edges(
        session, environment_id, chunk
    )
    await _expand(session, chunk)
    await event_log.append(session, chunk.events)
    completed, invalidated = await _apply_observations(session, environment_id, chunk)
    return MembersResult(
        tasks_created=len(chunk.tasks_created),
        instances_created=chunk.instances_created,
        members_admitted=admitted,
        closure_admitted=closure_admitted,
        edges_created=edges_created,
        completed=completed,
        invalidated=invalidated,
        diverged=diverged,
    )


def _check_clock_skew(items: Iterable[RegistrationItem], now: datetime) -> None:
    for it in items:
        if it.observed_at.tzinfo is None:
            raise BadRequest("observed_at_naive", "observed_at must carry a timezone")
        if it.observed_at - now > CLOCK_SKEW_TOLERANCE:
            raise BadRequest(
                "clock_skew",
                "observed_at is ahead of the registry's clock; fix the driver's clock",
                task_id=it.task_id,
                observed_at=it.observed_at.isoformat(),
                server_time=now.isoformat(),
            )


async def _insert_tasks(
    session: AsyncSession, environment_id: UUID, chunk: _Chunk
) -> None:
    """``task`` insert-if-absent by ``task_id``; an existing row must agree
    on every identity-level column."""
    firsts: dict[str, RegistrationItem] = {}
    for it in chunk.items:
        seen = firsts.setdefault(it.task_id, it)
        if _identity(seen) != _identity(it):
            raise _identity_conflict(it.task_id, _identity(seen), _identity(it))
    now = chunk.clock.now
    created = (
        await session.scalars(
            pg_insert(Task)
            .values(
                [
                    {
                        "id": generate_uuid7(),
                        "environment_id": environment_id,
                        "task_id": it.task_id,
                        "task_namespace": it.task_namespace,
                        "task_name": it.task_name,
                        "version": it.version,
                        "output_uri": it.output_uri,
                        "status": TaskStatus.PENDING,
                        "status_at": now,
                        "created_at": now,
                    }
                    for it in firsts.values()
                ]
            )
            .on_conflict_do_nothing(constraint="uq_task_environment_task_id")
            .returning(Task.task_id)
        )
    ).all()
    chunk.tasks_created = set(created)
    rows = await session.execute(
        select(
            Task.task_id,
            Task.id,
            Task.status,
            Task.task_namespace,
            Task.task_name,
            Task.version,
            Task.output_uri,
        ).where(Task.environment_id == environment_id, Task.task_id.in_(firsts))
    )
    for task_id, pk, status, namespace, name, version, output_uri in rows:
        recorded = (namespace, name, version, output_uri)
        if recorded != _identity(firsts[task_id]):
            raise _identity_conflict(task_id, recorded, _identity(firsts[task_id]))
        chunk.task_pk[task_id] = pk
        chunk.task_status[task_id] = status


def _identity(it: RegistrationItem) -> tuple[str, str, str | None, str | None]:
    return (it.task_namespace, it.task_name, it.version, it.output_uri)


_IDENTITY_FIELDS = ("task_namespace", "task_name", "version", "output_uri")


def _identity_conflict(
    task_id: str, recorded: tuple[Any, ...], sent: tuple[Any, ...]
) -> Conflict:
    fields = [f for f, a, b in zip(_IDENTITY_FIELDS, recorded, sent) if a != b]
    return Conflict(
        "task_identity_conflict",
        f"task {task_id} is recorded with another {', '.join(fields)}",
        task_id=task_id,
        fields=fields,
    )


async def _insert_instances(
    session: AsyncSession, environment_id: UUID, chunk: _Chunk
) -> None:
    """``task_instance`` insert-if-absent by ``(scope, instance_hash)``; an
    existing row must hold the same body (``instance_body_conflict``)."""
    plan, now = chunk.plan, chunk.clock.now
    created = (
        await session.scalars(
            pg_insert(TaskInstance)
            .values(
                [
                    {
                        "id": generate_uuid7(),
                        "environment_id": environment_id,
                        "deployment_id": plan.deployment_id,
                        "settings_hash": plan.settings_hash,
                        "instance_hash": it.instance_hash,
                        "task_pk": chunk.task_pk[it.task_id],
                        "body": it.body,
                        # A new instance lands expanded with its edges, in
                        # this same transaction.
                        "expanded_at": (
                            now if it.declared_upstreams is not None else None
                        ),
                        "created_at": now,
                    }
                    for it in chunk.items
                ]
            )
            .on_conflict_do_nothing(constraint="uq_task_instance_scope_hash")
            .returning(TaskInstance.instance_hash)
        )
    ).all()
    new = set(created)
    chunk.instances_created = len(new)
    by_hash = {it.instance_hash: it for it in chunk.items}
    rows = await session.execute(
        select(
            TaskInstance.instance_hash,
            TaskInstance.id,
            TaskInstance.task_pk,
            TaskInstance.body,
            TaskInstance.expanded_at,
        ).where(
            TaskInstance.deployment_id == plan.deployment_id,
            TaskInstance.settings_hash == plan.settings_hash,
            TaskInstance.instance_hash.in_(by_hash),
        )
    )
    for instance_hash, instance_id, task_pk, body, expanded_at in rows:
        it = by_hash[instance_hash]
        if task_pk != chunk.task_pk[it.task_id] or body != it.body:
            raise Conflict(
                "instance_body_conflict",
                "an instance with this hash is recorded with a different body",
                instance_hash=instance_hash,
                task_id=it.task_id,
            )
        chunk.instance_id[instance_hash] = instance_id
        if instance_hash in new:
            continue
        if expanded_at is not None:
            chunk.pre_expanded.add(instance_id)
        elif it.declared_upstreams is not None:
            chunk.to_expand.append(instance_id)


async def _check_sealed(session: AsyncSession, chunk: _Chunk) -> None:
    """A sealed plan takes no new static structure (409 ``plan_sealed``).

    Its request is fully stated, so after the seal this route accepts only:

    - **re-delivery**: an item the plan already holds under the same
      instance is a no-op for membership (its observation still applies —
      a resume re-observes a sealed plan's targets through here);
    - **a discovery job's result**: the first expansion of a member admitted
      unexpanded (a COMPLETED root later invalidated, a closure admission),
      with the items it reaches over ``declared_upstreams`` in this chunk.
      design.md ("The runnable rule") routes discovery through this route,
      and a sealed plan can have discovery jobs.

    Anything else — a member the plan does not hold and no discovery
    expansion reaches — is refused, as is new edges on an instance that was
    already expanded (checked in :func:`_insert_edges`). The other post-seal
    writers are closure admission and ``/yield``, by design.
    """
    plan = chunk.plan
    if plan.sealed_at is None:
        return
    held = dict(
        (
            await session.execute(
                select(PlanMember.task_pk, PlanMember.instance_id).where(
                    PlanMember.plan_id == plan.id,
                    PlanMember.task_pk.in_(set(chunk.task_pk.values())),
                )
            )
        )
        .tuples()
        .all()
    )
    to_expand = set(chunk.to_expand)
    by_hash = {it.instance_hash: it for it in chunk.items}

    def is_held(it: RegistrationItem) -> bool:
        return (
            held.get(chunk.task_pk[it.task_id]) == chunk.instance_id[it.instance_hash]
        )

    reached: set[str] = set()
    stack = [
        it.instance_hash
        for it in chunk.items
        if is_held(it) and chunk.instance_id[it.instance_hash] in to_expand
    ]
    while stack:
        h = stack.pop()
        if h in reached:
            continue
        reached.add(h)
        for up in by_hash[h].declared_upstreams or ():
            if up in by_hash:
                stack.append(up)
    refused = sorted(
        it.task_id
        for it in chunk.items
        # Another instance of a held completion is instance_conflict's.
        if chunk.task_pk[it.task_id] not in held and it.instance_hash not in reached
    )
    if refused:
        raise Conflict(
            "plan_sealed",
            "the plan is sealed: its request is fully stated, and only a"
            " re-delivery or a discovery job's result may still land",
            plan_id=str(plan.id),
            task_ids=refused,
        )


async def _admit_items(
    session: AsyncSession, environment_id: UUID, chunk: _Chunk, *, as_roots: bool
) -> int:
    """``plan_member`` insert-if-absent for the chunk's own items."""
    admitted_by = AdmittedBy.ROOT if as_roots else AdmittedBy.STATIC
    rows = [
        (
            it.task_id,
            chunk.task_pk[it.task_id],
            chunk.instance_id[it.instance_hash],
            it.body,
        )
        for it in chunk.items
    ]
    count, events = await admit_members(
        session,
        environment_id,
        chunk.plan,
        rows,
        admitted_by,
        clock=chunk.clock,
        tasks_created=chunk.tasks_created,
        is_root=as_roots,
    )
    chunk.events.extend(events)
    return count


async def admit_members(
    session: AsyncSession,
    environment_id: UUID,
    plan: Plan,
    rows: Sequence[tuple[str, UUID, UUID, Mapping[str, Any]]],
    admitted_by: AdmittedBy,
    *,
    clock: EventClock,
    tasks_created: Iterable[str] = (),
    is_root: bool = False,
) -> tuple[int, list[dict[str, Any]]]:
    """Insert members ``(task_id, task_pk, instance_id, body)``, in ``task_id``
    order; a completion the plan holds under another instance is 409
    ``instance_conflict``. Queues TASK_PENDING / TASK_REFERENCED for every
    member actually inserted. Returns how many were, and those events."""
    if not rows:
        return 0, []
    created = set(tasks_created)
    ordered = sorted(rows, key=lambda r: r[0])
    # Keyed by (task, instance): two instances of one completion in one
    # statement insert one row, and the other must be seen as refused.
    inserted = set(
        (
            await session.execute(
                pg_insert(PlanMember)
                .values(
                    [
                        {
                            "environment_id": environment_id,
                            "plan_id": plan.id,
                            "task_pk": task_pk,
                            "instance_id": instance_id,
                            "deployment_id": plan.deployment_id,
                            "settings_hash": plan.settings_hash,
                            "is_root": is_root,
                            "admitted_by": admitted_by,
                            "created_at": clock.now,
                        }
                        for _, task_pk, instance_id, _ in ordered
                    ]
                )
                .on_conflict_do_nothing(constraint="pk_plan_member")
                .returning(PlanMember.task_pk, PlanMember.instance_id)
            )
        )
        .tuples()
        .all()
    )
    # The rest were already members: under the same instance, or a conflict.
    held_rows = [r for r in ordered if (r[1], r[2]) not in inserted]
    if held_rows:
        held = {
            task_pk: (instance_id, body)
            for task_pk, instance_id, body in (
                await session.execute(
                    select(
                        PlanMember.task_pk, PlanMember.instance_id, TaskInstance.body
                    )
                    .join(TaskInstance, TaskInstance.id == PlanMember.instance_id)
                    .where(
                        PlanMember.plan_id == plan.id,
                        PlanMember.task_pk.in_([r[1] for r in held_rows]),
                    )
                )
            ).tuples()
        }
        for task_id, task_pk, instance_id, body in held_rows:
            member_instance, member_body = held[task_pk]
            if member_instance != instance_id:
                raise _instance_conflict(
                    task_id,
                    member_body,
                    body,
                    plan_id=str(plan.id),
                    member_instance_id=str(member_instance),
                    other_instance_id=str(instance_id),
                )
    events = []
    for task_id, task_pk, instance_id, _ in ordered:
        if (task_pk, instance_id) in inserted:
            event_type = (
                EventType.TASK_PENDING
                if task_id in created
                else EventType.TASK_REFERENCED
            )
            events.append(
                event_log.event_row(
                    environment_id,
                    event_type,
                    at=clock.tick(),
                    build_id=plan.build_id,
                    task_pk=task_pk,
                    plan_id=plan.id,
                    metadata={"admitted_by": admitted_by.value},
                )
            )
    return len(inserted), events


async def _insert_edges(
    session: AsyncSession, environment_id: UUID, chunk: _Chunk
) -> tuple[int, int, int]:
    """Edges for every ``declared_upstreams``; each upstream must exist in
    the scope and becomes a member (``admitted_by = closure``) if it is not
    one. Returns (edges created, instances diverged, upstreams admitted)."""
    plan = chunk.plan
    declared = [it for it in chunk.items if it.declared_upstreams]
    if not declared:
        return 0, 0, 0
    wanted = {h for it in declared for h in it.declared_upstreams or ()}
    upstream_rows = (
        await session.execute(
            select(
                TaskInstance.instance_hash,
                TaskInstance.id,
                TaskInstance.task_pk,
                TaskInstance.body,
                Task.task_id,
            )
            .join(Task, Task.id == TaskInstance.task_pk)
            .where(
                TaskInstance.deployment_id == plan.deployment_id,
                TaskInstance.settings_hash == plan.settings_hash,
                TaskInstance.instance_hash.in_(wanted),
            )
        )
    ).all()
    upstream = {row.instance_hash: row for row in upstream_rows}
    missing = sorted(wanted - upstream.keys())
    if missing:
        raise BadRequest(
            "unknown_upstream_instance",
            "declared upstreams must exist as instances in the plan's scope"
            " (registered in this or an earlier chunk)",
            instance_hashes=missing,
        )

    edges = sorted(
        {
            (chunk.instance_id[it.instance_hash], upstream[h].id)
            for it in declared
            for h in it.declared_upstreams or ()
        }
    )
    created = (
        await session.execute(
            pg_insert(TaskInstanceDependency)
            .values(
                [
                    {
                        "environment_id": environment_id,
                        "downstream_instance_id": down,
                        "upstream_instance_id": up,
                        "deployment_id": plan.deployment_id,
                        "settings_hash": plan.settings_hash,
                        "is_dynamic": False,
                        "created_at": chunk.clock.now,
                    }
                    for down, up in edges
                ]
            )
            .on_conflict_do_nothing(constraint="pk_task_instance_dependency")
            .returning(
                TaskInstanceDependency.downstream_instance_id,
                TaskInstanceDependency.upstream_instance_id,
            )
        )
    ).all()

    # Edges only grow: an instance expanded before this chunk that now
    # declares more upstreams diverged within its scope (a contract breach
    # that over-gates). Recorded once, when the new edges land.
    grown: dict[UUID, list[UUID]] = {}
    for down, up in created:
        if down in chunk.pre_expanded:
            grown.setdefault(down, []).append(up)
    by_instance = {chunk.instance_id[it.instance_hash]: it for it in declared}
    if grown and plan.sealed_at is not None:
        raise Conflict(
            "plan_sealed",
            "the plan is sealed: an already-expanded instance cannot gain"
            " edges through it",
            plan_id=str(plan.id),
            task_ids=sorted(by_instance[d].task_id for d in grown),
        )
    for down, ups in sorted(grown.items()):
        it = by_instance[down]
        chunk.events.append(
            event_log.event_row(
                environment_id,
                EventType.TASK_STRUCTURE_DIVERGED,
                at=chunk.clock.tick(),
                build_id=plan.build_id,
                task_pk=chunk.task_pk[it.task_id],
                plan_id=plan.id,
                metadata={
                    "instance_id": str(down),
                    "added_upstream_instance_ids": sorted(str(u) for u in ups),
                },
            )
        )

    in_chunk = {chunk.task_pk[it.task_id] for it in chunk.items}
    to_admit = [
        (row.task_id, row.task_pk, row.id, row.body)
        for row in upstream.values()
        if row.task_pk not in in_chunk
    ]
    # An upstream sharing a completion with a chunk item must be that item.
    for row in upstream.values():
        if row.task_pk in in_chunk and chunk.instance_id.get(row.instance_hash) is None:
            item = next(i for i in chunk.items if i.task_id == row.task_id)
            raise _instance_conflict(
                row.task_id,
                item.body,
                row.body,
                plan_id=str(plan.id),
                member_instance_id=str(chunk.instance_id[item.instance_hash]),
                other_instance_id=str(row.id),
            )
    closure_admitted, events = await admit_members(
        session,
        environment_id,
        plan,
        to_admit,
        AdmittedBy.CLOSURE,
        clock=chunk.clock,
    )
    chunk.events.extend(events)
    return len(created), len(grown), closure_admitted


async def _expand(session: AsyncSession, chunk: _Chunk) -> None:
    """Set the closure flag on existing instances this chunk expanded.

    New instances were inserted with it. The rows are locked in id order
    first, so two chunks expanding the same instances cannot deadlock.
    """
    if not chunk.to_expand:
        return
    ids = sorted(chunk.to_expand)
    await session.execute(
        select(TaskInstance.id)
        .where(TaskInstance.id.in_(ids))
        .order_by(TaskInstance.id)
        .with_for_update(key_share=True)
    )
    await session.execute(
        update(TaskInstance)
        .where(TaskInstance.id.in_(ids), TaskInstance.expanded_at.is_(None))
        .values(expanded_at=chunk.clock.now)
    )


async def _apply_observations(
    session: AsyncSession, environment_id: UUID, chunk: _Chunk
) -> tuple[int, int]:
    """The status rule, in this transaction, in ``task_id`` order.

    ``observed_complete = true`` → COMPLETED unless a live claim holds the
    task; ``false`` on a COMPLETED task → PENDING, if the completion on
    record precedes the observation. ``transition_task()`` re-reads the
    task under its row lock and decides; the read here only skips items
    that could not change anything.
    """
    completed = invalidated = 0
    for it in chunk.items:
        status = chunk.task_status[it.task_id]
        if it.observed_complete and status != TaskStatus.COMPLETED:
            kind = TransitionKind.OBSERVE_COMPLETE
        elif not it.observed_complete and status == TaskStatus.COMPLETED:
            kind = TransitionKind.INVALIDATE
        else:
            continue
        outcome = await transition_task(
            session,
            environment_id,
            task_pk=chunk.task_pk[it.task_id],
            plan_id=chunk.plan.id,
            transition=Transition(kind, observed_at=it.observed_at),
            now=chunk.clock.tick(),
        )
        if outcome.applied:
            if kind is TransitionKind.OBSERVE_COMPLETE:
                completed += 1
            else:
                invalidated += 1
    return completed, invalidated
