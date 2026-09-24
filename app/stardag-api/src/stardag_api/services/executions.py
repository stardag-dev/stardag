"""The execution ledger as ``builds stop`` reads and closes it.

design.md, "Rollover" (orphaned executions) and the ``execution`` entity.
``ended_at IS NULL`` means "no report of this execution ending has
arrived" — independent of whether the claim has since moved (an execution
ref is not a claim) — which is exactly what ``stardag builds stop`` lists.
An **orphan** is such an execution whose plan is not the build's active
plan (``not_in_current_plan``). Nothing ends one automatically: the CLI
stops the container through the backend and reports it here
(``outcome = stopped``), which is also how a build's unended executions
are cleared before it can be deleted.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Select, and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.models import (
    ClaimOutcome,
    Execution,
    ExecutionOutcome,
    Plan,
    Task,
)
from stardag_api.models.base import utc_now
from stardag_api.services.builds import active_plan, get_build
from stardag_api.services.errors import NotFound
from stardag_api.services.transitions import (
    Transition,
    TransitionOutcome,
    transition_task,
)
from stardag_api.services.tx import transaction


@dataclass(frozen=True)
class ExecutionState:
    id: UUID
    task_id: str
    build_id: UUID
    plan_id: UUID
    instance_id: UUID
    executor: str | None
    executor_ref: str | None
    executor_metadata: dict[str, Any] | None
    started_at: datetime
    claim_released_at: datetime | None
    claim_outcome: ClaimOutcome | None
    ended_at: datetime | None
    outcome: ExecutionOutcome | None
    #: The execution's plan is its build's active plan (False = an orphan).
    in_current_plan: bool


def _ledger_query() -> Select[tuple[Execution, str, UUID, bool]]:
    """The ledger rows with the task id, the build, and whether the plan is
    its build's active one."""
    return (
        select(
            Execution,
            Task.task_id,
            Plan.build_id,
            and_(Plan.activated_at.is_not(None), Plan.superseded_at.is_(None)),
        )
        .join(Task, Task.id == Execution.task_pk)
        .join(Plan, Plan.id == Execution.plan_id)
    )


async def _states(session: AsyncSession, query: Select) -> list[ExecutionState]:
    rows = (await session.execute(query)).tuples().all()
    return [
        ExecutionState(
            id=e.id,
            task_id=task_id,
            build_id=build_id,
            plan_id=e.plan_id,
            instance_id=e.instance_id,
            executor=e.executor,
            executor_ref=e.executor_ref,
            executor_metadata=e.executor_metadata,
            started_at=e.started_at,
            claim_released_at=e.claim_released_at,
            claim_outcome=e.claim_outcome,
            ended_at=e.ended_at,
            outcome=e.outcome,
            in_current_plan=bool(active),
        )
        for e, task_id, build_id, active in rows
    ]


async def list_executions(
    session: AsyncSession,
    environment_id: UUID,
    build_id: UUID,
    *,
    not_in_current_plan: bool = False,
    include_ended: bool = False,
) -> list[ExecutionState]:
    """The build's executions with no end reported, over all its plans,
    oldest first; ``not_in_current_plan`` keeps the orphans only.

    ``include_ended`` lists the whole ledger instead: every execution the
    build's plans ever granted, ended or not — the durable record of what
    was spawned, which a tick's own summary cannot be (a tick can die
    before it reports)."""
    build = await get_build(session, environment_id, build_id)
    current = await active_plan(session, build.id)
    query = (
        _ledger_query()
        .where(Plan.build_id == build.id)
        .order_by(Execution.started_at, Execution.id)
    )
    if not include_ended:
        query = query.where(Execution.ended_at.is_(None))
    if not_in_current_plan and current is not None:
        query = query.where(Execution.plan_id != current.id)
    return await _states(session, query)


async def list_task_executions(
    session: AsyncSession,
    environment_id: UUID,
    task_id: str,
    *,
    include_ended: bool = True,
    limit: int = 100,
) -> list[ExecutionState]:
    """Every execution of one completion, across builds, newest first (at
    most ``limit``; ``ix_execution_task_started``). ``include_ended=False``
    keeps those with no end reported."""
    task_pk = await session.scalar(
        select(Task.id).where(
            Task.environment_id == environment_id, Task.task_id == task_id
        )
    )
    if task_pk is None:
        raise NotFound("unknown_task", f"no task {task_id}", task_id=task_id)
    query = (
        _ledger_query()
        .where(Execution.task_pk == task_pk)
        .order_by(Execution.started_at.desc(), Execution.id.desc())
        .limit(limit)
    )
    if not include_ended:
        query = query.where(Execution.ended_at.is_(None))
    return await _states(session, query)


async def report_stopped(
    session: AsyncSession,
    environment_id: UUID,
    execution_id: UUID,
    *,
    outcome: ExecutionOutcome = ExecutionOutcome.STOPPED,
) -> TransitionOutcome:
    """Record an operator end of the execution — ``stopped`` (the CLI
    stopped it) or ``lost`` (it cannot be stopped; the operator gives up on
    it) — through ``transition_task()``: the ledger end, and — if it still
    holds the task's claim — the claim's release (the task CANCELLED).
    Idempotent: an ended execution is left as it ended.

    Only the execution's task and plan keys — which never change — are read
    before ``transition_task()`` locks the task row; the execution itself
    is loaded under that lock, so a stop or an end report that won the lock
    first is seen, not a stale copy."""
    async with transaction(session):
        keys = (
            await session.execute(
                select(Execution.task_pk, Execution.plan_id).where(
                    Execution.environment_id == environment_id,
                    Execution.id == execution_id,
                )
            )
        ).one_or_none()
        if keys is None:
            raise NotFound(
                "unknown_execution",
                f"no execution {execution_id}",
                execution_id=str(execution_id),
            )
        return await transition_task(
            session,
            environment_id,
            task_pk=keys.task_pk,
            plan_id=keys.plan_id,
            transition=Transition.stop(execution_id, outcome),
            now=utc_now(),
        )
