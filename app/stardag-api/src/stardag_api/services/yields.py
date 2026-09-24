"""The dynamic phase: one ``/yield`` batch, in one transaction.

See design.md, "Registration" (the Dynamic phase block) and "Rollover" (a
yield into a superseded plan). In order, under the parent's row lock:

1. The parent's ``task`` row is locked ``FOR NO KEY UPDATE`` first — the
   lock every transition takes — so a batch serialises with the parent's
   reports and with a retry of itself.
2. ``deployment_id`` must be the plan's (409 ``deployment_mismatch``): a
   worker never yields into a scope that is not its own.
3. **Replay.** A batch already applied for this ``(execution_id,
   batch_id)`` — a ``TASK_YIELDED`` event, whose typed ``batch_id`` has a
   unique index with the execution — returns its stored result without
   re-checking anything: a ``suspend: true`` batch released the claim, so
   the plain execution check would refuse the worker's own retry after a
   lost response.
4. Otherwise the execution must be the task's current one and its claim
   unreleased (the authority rule every report follows, lapsed or not);
   else the batch is recorded ``report_applied = false`` and refused 409.
5. The items land through ``register_items`` exactly as a static chunk
   (the yielded ones admitted ``dynamic``), parent→child edges to every
   ``yielded`` instance are inserted with ``is_dynamic = true``, and the
   ``TASK_YIELDED`` event records the batch.
6. ``suspend: true`` applies ``TASK_SUSPENDED`` through
   ``transition_task()`` (``claim_outcome = suspended``); ``suspend:
   false`` leaves the claim (the resident engine's generator waits).

An ``instance_conflict`` while registering is non-retryable: the items are
rolled back (a savepoint), the parent is failed (``TASK_FAILED`` with the
conflict named) and the batch is refused 409 ``instance_conflict``. Any
other registration refusal rolls everything back; the worker then reports
``TASK_FAILED`` itself — a failure to register is never swallowed (S13).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.models import (
    Event,
    EventType,
    Execution,
    Plan,
    PlanMember,
    Task,
    TaskInstance,
    TaskInstanceDependency,
    TaskStatus,
)
from stardag_api.models.base import utc_now
from stardag_api.schemas_v2 import RegistrationItem
from stardag_api.services import event_log
from stardag_api.services.errors import BadRequest, Conflict, RecordedConflict
from stardag_api.services.event_log import EventClock
from stardag_api.services.registration import MAX_CHUNK_ITEMS, get_plan
from stardag_api.services.registration_chunk import MembersResult, register_items
from stardag_api.services.transitions import (
    Transition,
    lock_task,
    member_task_pk,
    transition_task,
)
from stardag_api.services.tx import transaction


@dataclass(frozen=True)
class YieldResult:
    members: MembersResult
    dynamic_edges_created: int
    status: TaskStatus
    execution_id: UUID | None
    claim_expires_at: datetime | None
    replayed: bool = False

    def stored(self) -> dict[str, Any]:
        """What the ``TASK_YIELDED`` event keeps, to replay the batch."""
        return {
            "members": asdict(self.members),
            "dynamic_edges_created": self.dynamic_edges_created,
            "status": self.status.value,
            "execution_id": str(self.execution_id) if self.execution_id else None,
            "claim_expires_at": (
                self.claim_expires_at.isoformat() if self.claim_expires_at else None
            ),
        }

    @classmethod
    def replay(cls, stored: dict[str, Any]) -> YieldResult:
        expires = stored["claim_expires_at"]
        execution_id = stored["execution_id"]
        return cls(
            members=MembersResult(**stored["members"]),
            dynamic_edges_created=stored["dynamic_edges_created"],
            status=TaskStatus(stored["status"]),
            execution_id=UUID(execution_id) if execution_id else None,
            claim_expires_at=datetime.fromisoformat(expires) if expires else None,
            replayed=True,
        )


async def yield_batch(
    session: AsyncSession,
    environment_id: UUID,
    *,
    plan_id: UUID,
    task_id: str,
    execution_id: UUID,
    deployment_id: UUID,
    batch_id: UUID,
    items: Sequence[RegistrationItem],
    yielded: Sequence[str],
    suspend: bool,
) -> YieldResult:
    """Apply one yield batch of the member ``task_id`` of ``plan_id``."""
    if len(items) > MAX_CHUNK_ITEMS:
        raise BadRequest(
            "chunk_too_large",
            f"at most {MAX_CHUNK_ITEMS} items per yield batch",
            items=len(items),
        )
    hashes = {it.instance_hash for it in items}
    unknown = sorted(set(yielded) - hashes)
    if unknown:
        raise BadRequest(
            "unknown_yielded_instance",
            "every yielded instance must be one of the batch's items",
            instance_hashes=unknown,
        )
    async with transaction(session):
        plan = await get_plan(session, environment_id, plan_id)
        task_pk = await member_task_pk(session, environment_id, plan_id, task_id)
        parent = await lock_task(session, environment_id, task_pk)
        if deployment_id != plan.deployment_id:
            raise Conflict(
                "deployment_mismatch",
                "the worker's deployment is not the plan's: a worker never"
                " yields into a scope that is not its own",
                deployment_id=str(deployment_id),
                plan_deployment_id=str(plan.deployment_id),
            )
        applied = await session.scalar(
            select(Event.event_metadata).where(
                Event.execution_id == execution_id,
                Event.batch_id == batch_id,
                Event.event_type == EventType.TASK_YIELDED,
            )
        )
        if applied is not None:
            return YieldResult.replay(applied)

        clock = EventClock(utc_now())
        await _check_current(
            session, environment_id, parent, plan_id, execution_id, batch_id, clock
        )
        parent_instance = await session.scalar(
            select(PlanMember.instance_id).where(
                PlanMember.plan_id == plan.id, PlanMember.task_pk == parent.id
            )
        )
        assert parent_instance is not None
        try:
            async with session.begin_nested():
                members = await register_items(
                    session,
                    environment_id,
                    plan,
                    items,
                    as_roots=False,
                    now=clock.now,
                    dynamic=set(yielded),
                    from_yield=True,
                )
                edges = await _dynamic_edges(
                    session, environment_id, plan, parent_instance, yielded
                )
        except Conflict as conflict:
            if conflict.code != "instance_conflict":
                raise
            await transition_task(
                session,
                environment_id,
                task_pk=parent.id,
                plan_id=plan.id,
                transition=Transition.fail(execution_id, conflict.message),
                now=utc_now(),
            )
            raise RecordedConflict(
                "instance_conflict",
                conflict.message + "; the yielding member is failed (a conflict"
                " at /yield is not retryable)",
                **conflict.detail,
            ) from conflict

        # After the chunk's own events (its clock started at clock.now).
        clock = EventClock(max(utc_now(), clock.now))
        # The batch's event precedes the suspend it caused in the log; it is
        # written once the result is known (events are never updated).
        yielded_at = clock.tick()
        if suspend:
            await transition_task(
                session,
                environment_id,
                task_pk=parent.id,
                plan_id=plan.id,
                transition=Transition.suspend(execution_id),
                now=clock.tick(),
            )
        result = YieldResult(
            members=members,
            dynamic_edges_created=edges,
            status=parent.status,
            execution_id=parent.execution_id,
            claim_expires_at=parent.claim_expires_at,
        )
        await event_log.append(
            session,
            [
                event_log.event_row(
                    environment_id,
                    EventType.TASK_YIELDED,
                    at=yielded_at,
                    build_id=plan.build_id,
                    task_pk=parent.id,
                    plan_id=plan.id,
                    execution_id=execution_id,
                    batch_id=batch_id,
                    metadata={
                        **result.stored(),
                        "yielded": sorted(yielded),
                        "suspend": suspend,
                    },
                )
            ],
        )
        return result


async def _check_current(
    session: AsyncSession,
    environment_id: UUID,
    parent: Task,
    plan_id: UUID,
    execution_id: UUID,
    batch_id: UUID,
    clock: EventClock,
) -> None:
    """The authority rule: the batch's execution is the task's current one
    and its claim has not been released (lapsed or not). Otherwise the
    batch is recorded ``report_applied = false`` and refused."""
    execution = await session.get(Execution, execution_id)
    known = execution is not None and execution.task_pk == parent.id
    if (
        known
        and execution is not None
        and parent.execution_id == execution_id
        and execution.claim_released_at is None
        and execution.ended_at is None
    ):
        # The authority rule's plan half: the current execution yields
        # through the plan holding its claim (409 ``not_claim_holder``
        # otherwise, with no trace, as for every report).
        if parent.claim_plan_id != plan_id:
            raise Conflict(
                "not_claim_holder",
                "the task's claim is held through another plan; its execution"
                " yields through that plan",
                task_id=parent.task_id,
                execution_id=str(execution_id),
                plan_id=str(plan_id),
                claim_plan_id=(
                    str(parent.claim_plan_id) if parent.claim_plan_id else None
                ),
            )
        return
    await event_log.append(
        session,
        [
            event_log.event_row(
                environment_id,
                EventType.TASK_YIELDED,
                at=clock.tick(),
                build_id=await _build_id(session, plan_id),
                task_pk=parent.id,
                plan_id=plan_id,
                # An unknown execution cannot be pointed at (FK); the id is
                # kept in the metadata either way. The refused record carries
                # no typed batch_id, so it can never be replayed as applied.
                execution_id=execution_id if known else None,
                report_applied=False,
                metadata={
                    "execution_id": str(execution_id),
                    "batch_id": str(batch_id),
                    "refused": "execution_not_current"
                    if known
                    else "unknown_execution",
                },
            )
        ],
    )
    if not known:
        raise RecordedConflict(
            "unknown_execution",
            "no execution with this id exists for the task",
            execution_id=str(execution_id),
        )
    raise RecordedConflict(
        "execution_not_current",
        "the execution does not hold the task's claim (taken over, closed,"
        " released or ended); the batch is recorded and not applied",
        execution_id=str(execution_id),
    )


async def _build_id(session: AsyncSession, plan_id: UUID) -> UUID | None:
    return await session.scalar(select(Plan.build_id).where(Plan.id == plan_id))


async def _dynamic_edges(
    session: AsyncSession,
    environment_id: UUID,
    plan: Plan,
    parent_instance: UUID,
    yielded: Sequence[str],
) -> int:
    """Parent→child edges (``is_dynamic = true``) to every yielded instance,
    insert-if-absent; returns how many were new. The children are the
    batch's items, so they exist in the scope and are members."""
    children = sorted(
        (
            await session.scalars(
                select(TaskInstance.id).where(
                    TaskInstance.deployment_id == plan.deployment_id,
                    TaskInstance.settings_hash == plan.settings_hash,
                    TaskInstance.instance_hash.in_(set(yielded)),
                )
            )
        ).all()
    )
    created = (
        await session.execute(
            pg_insert(TaskInstanceDependency)
            .values(
                [
                    {
                        "environment_id": environment_id,
                        "downstream_instance_id": parent_instance,
                        "upstream_instance_id": child,
                        "deployment_id": plan.deployment_id,
                        "settings_hash": plan.settings_hash,
                        "is_dynamic": True,
                        "created_at": utc_now(),
                    }
                    for child in children
                ]
            )
            .on_conflict_do_nothing(constraint="pk_task_instance_dependency")
            .returning(TaskInstanceDependency.upstream_instance_id)
        )
    ).all()
    return len(created)
