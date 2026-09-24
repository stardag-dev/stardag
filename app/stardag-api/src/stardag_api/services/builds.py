"""Build lifecycle: create, read, complete, fail, cancel, exit-early,
resume, delete.

See design.md, "The runnable rule" (the build-status paragraph and
resumability), "Registration" (lifecycle transitions are idempotent by
state) and the ``build`` entity.

- **Status is stored**, driven by build events; the server never flips it
  inside a task transition. Every lifecycle call locks the build row
  (``FOR NO KEY UPDATE``) first, then task rows in ``task_id`` order.
- **Idempotent by state**: a transition that finds the build already in the
  requested state returns it and writes no event and no timestamp.
- **Completion is verified**: ``complete`` recomputes ``plan_complete``
  (sealed, every non-excluded member COMPLETED) over the active plan's
  member task rows read ``FOR SHARE`` in ``task_id`` order, which
  serialises with an invalidation's ``FOR NO KEY UPDATE``. ``force``
  overrides outstanding members, never a missing seal and never an
  excluded root.
- **Claims**: ``complete``, ``fail`` and ``cancel`` release the claims held
  by any of the build's plans, through ``transition_task()`` (``claim_outcome
  = released``, task CANCELLED); ``exit-early`` releases nothing.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import delete, exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.models import (
    Build,
    BuildStatus,
    EventType,
    Execution,
    Plan,
    PlanMember,
    Task,
    TaskStatus,
)
from stardag_api.models.base import generate_uuid7, utc_now
from stardag_api.services import event_log
from stardag_api.services.deployments import (
    settings_hash,
    validate_settings,
    verify_deployment_current,
)
from stardag_api.services.errors import Conflict, NotFound
from stardag_api.services.event_log import EventClock
from stardag_api.services.registration import PlanState, lock_build
from stardag_api.services.slug import generate_build_slug
from stardag_api.services.transitions import Transition, transition_task
from stardag_api.services.tx import transaction

if TYPE_CHECKING:
    from stardag_api.services.plans import ClosureConflict

#: How many outstanding task ids a ``plan_incomplete`` refusal names.
_MAX_NAMED = 50


# ---------------------------------------------------------------------------
# Create and read
# ---------------------------------------------------------------------------


async def create_build(
    session: AsyncSession,
    environment_id: UUID,
    *,
    build_id: UUID | None = None,
    name: str | None = None,
    description: str | None = None,
    root_task_ids: Sequence[str] = (),
    user_id: UUID | None = None,
    executor_metadata: dict[str, Any] | None = None,
) -> Build:
    """Create a RUNNING build (``BUILD_STARTED``) requesting
    ``root_task_ids``. Idempotent on a client-minted id: a re-delivered
    create returns the build unchanged."""
    async with transaction(session):
        if build_id is not None:
            existing = await session.get(Build, build_id)
            if existing is not None:
                if existing.environment_id != environment_id:
                    raise Conflict("build_id_conflict", f"build id {build_id} is taken")
                return existing
        now = utc_now()
        build = Build(
            id=build_id or generate_uuid7(),
            environment_id=environment_id,
            user_id=user_id,
            name=name or generate_build_slug(),
            description=description,
            root_task_ids=sorted(set(root_task_ids)),
            executor_metadata=executor_metadata,
            status=BuildStatus.RUNNING,
            started_at=now,
            last_active_at=now,
            created_at=now,
        )
        session.add(build)
        await session.flush()
        await event_log.append(
            session,
            [
                event_log.event_row(
                    environment_id, EventType.BUILD_STARTED, at=now, build_id=build.id
                )
            ],
        )
        return build


async def get_build(
    session: AsyncSession, environment_id: UUID, build_id: UUID
) -> Build:
    build = await session.scalar(
        select(Build).where(
            Build.environment_id == environment_id, Build.id == build_id
        )
    )
    if build is None:
        raise NotFound("unknown_build", f"no build {build_id}", build_id=str(build_id))
    return build


async def active_plan(session: AsyncSession, build_id: UUID) -> Plan | None:
    return await session.scalar(
        select(Plan).where(
            Plan.build_id == build_id,
            Plan.activated_at.is_not(None),
            Plan.superseded_at.is_(None),
        )
    )


# ---------------------------------------------------------------------------
# Terminal transitions
# ---------------------------------------------------------------------------


async def complete_build(
    session: AsyncSession,
    environment_id: UUID,
    build_id: UUID,
    *,
    force: bool = False,
    triggered_by: str | None = None,
) -> Build:
    """``BUILD_COMPLETED``, only if the active plan is complete (409
    ``plan_incomplete`` otherwise, with a ``reason``); see the module
    docstring for ``force``."""
    async with transaction(session):
        build = await lock_build(session, environment_id, build_id)
        if build.status == BuildStatus.COMPLETED:
            return build
        outstanding = await _verify_plan_complete(session, build, force=force)
        now = utc_now()
        await _terminal(
            session,
            build,
            BuildStatus.COMPLETED,
            EventType.BUILD_COMPLETED,
            clock=EventClock(now),
            triggered_by=triggered_by,
            metadata={"force": True, "outstanding": outstanding} if force else None,
        )
        return build


async def _verify_plan_complete(
    session: AsyncSession, build: Build, *, force: bool
) -> int:
    """Recompute ``plan_complete`` in the caller's transaction; returns the
    number of outstanding members ``force`` overrode."""
    plan = await active_plan(session, build.id)
    if plan is None or plan.sealed_at is None:
        raise Conflict(
            "plan_incomplete",
            "the build's active plan is not sealed: the request is not yet"
            " fully stated (force does not override this; fail or cancel)",
            reason="not_sealed",
            plan_id=str(plan.id) if plan else None,
        )
    rows = (
        await session.execute(
            select(
                Task.task_id, Task.status, PlanMember.is_root, PlanMember.excluded_at
            )
            .select_from(PlanMember)
            .join(Task, Task.id == PlanMember.task_pk)
            .where(PlanMember.plan_id == plan.id)
            .order_by(Task.task_id)
            .with_for_update(read=True, of=Task)
        )
    ).all()
    excluded_roots = [r.task_id for r in rows if r.is_root and r.excluded_at]
    if excluded_roots:
        raise Conflict(
            "plan_incomplete",
            "a root is excluded: the request cannot be met (force does not"
            " override this; fail or cancel)",
            reason="root_excluded",
            task_ids=excluded_roots[:_MAX_NAMED],
        )
    outstanding = [
        r.task_id
        for r in rows
        if r.excluded_at is None and r.status != TaskStatus.COMPLETED
    ]
    if outstanding and not force:
        raise Conflict(
            "plan_incomplete",
            "members are not COMPLETED",
            reason="members_incomplete",
            count=len(outstanding),
            task_ids=outstanding[:_MAX_NAMED],
        )
    return len(outstanding)


async def fail_build(
    session: AsyncSession,
    environment_id: UUID,
    build_id: UUID,
    *,
    error_message: str | None = None,
    triggered_by: str | None = None,
) -> Build:
    """``BUILD_FAILED``; releases the build's claims."""
    async with transaction(session):
        build = await lock_build(session, environment_id, build_id)
        if build.status == BuildStatus.FAILED:
            return build
        await _terminal(
            session,
            build,
            BuildStatus.FAILED,
            EventType.BUILD_FAILED,
            clock=EventClock(utc_now()),
            triggered_by=triggered_by,
            error_message=error_message,
        )
        return build


async def cancel_build(
    session: AsyncSession,
    environment_id: UUID,
    build_id: UUID,
    *,
    triggered_by: str | None = None,
) -> Build:
    """``BUILD_CANCELLED``; releases the build's claims. Its workers find
    out at their own checkpoints (cancellation is cooperative)."""
    async with transaction(session):
        build = await lock_build(session, environment_id, build_id)
        if build.status == BuildStatus.CANCELLED:
            return build
        await _terminal(
            session,
            build,
            BuildStatus.CANCELLED,
            EventType.BUILD_CANCELLED,
            clock=EventClock(utc_now()),
            triggered_by=triggered_by,
        )
        return build


async def exit_early(
    session: AsyncSession, environment_id: UUID, build_id: UUID
) -> Build:
    """``BUILD_EXIT_EARLY`` (everything left runs in other builds). Releases
    nothing: a resident build's in-flight tasks keep reporting, and if the
    process is gone their claims lapse like any other worker's."""
    async with transaction(session):
        build = await lock_build(session, environment_id, build_id)
        if build.status == BuildStatus.EXIT_EARLY:
            return build
        now = utc_now()
        _set_status(build, BuildStatus.EXIT_EARLY, now=now, triggered_by=None)
        await event_log.append(
            session,
            [
                event_log.event_row(
                    environment_id,
                    EventType.BUILD_EXIT_EARLY,
                    at=now,
                    build_id=build.id,
                )
            ],
        )
        await session.flush()
        return build


def _set_status(
    build: Build, status: BuildStatus, *, now: datetime, triggered_by: str | None
) -> None:
    build.status = status
    build.completed_at = now
    build.last_active_at = now
    build.is_resumed = False
    build.status_triggered_by_user_id = triggered_by


async def _terminal(
    session: AsyncSession,
    build: Build,
    status: BuildStatus,
    event_type: EventType,
    *,
    clock: EventClock,
    triggered_by: str | None,
    error_message: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Move a locked build to a terminal status: release its claims (for
    COMPLETED, FAILED and CANCELLED), then record the build event."""
    released = await release_claims(session, build, reason=status.value, clock=clock)
    at = clock.tick()
    _set_status(build, status, now=at, triggered_by=triggered_by)
    meta = dict(metadata or {})
    if released:
        meta["released_claims"] = released
    await event_log.append(
        session,
        [
            event_log.event_row(
                build.environment_id,
                event_type,
                at=at,
                build_id=build.id,
                error_message=error_message,
                metadata=meta or None,
            )
        ],
    )
    await session.flush()


async def release_claims(
    session: AsyncSession, build: Build, *, reason: str, clock: EventClock
) -> int:
    """Release every claim (live or lapsed) held by any of the build's
    plans, in ``task_id`` order, through ``transition_task()``. Returns how
    many were released."""
    held = (
        await session.execute(
            select(Task.id, Task.claim_plan_id)
            .join(Plan, Plan.id == Task.claim_plan_id)
            .where(Plan.build_id == build.id, Task.status == TaskStatus.RUNNING)
            .order_by(Task.task_id)
        )
    ).all()
    released = 0
    for task_pk, plan_id in held:
        outcome = await transition_task(
            session,
            build.environment_id,
            task_pk=task_pk,
            plan_id=plan_id,
            transition=Transition.release(reason),
            now=clock.tick(),
        )
        released += outcome.applied
    return released


async def fail_locked_build(
    session: AsyncSession,
    build: Build,
    *,
    at: datetime,
    error_message: str,
    metadata: dict[str, Any],
) -> None:
    """``BUILD_FAILED`` for a build the caller has locked, inside its
    transaction (a no-op on a build already FAILED): releases the build's
    claims like any fail. For server-side failures — a closure conflict,
    an excluded root."""
    if build.status == BuildStatus.FAILED:
        return
    await _terminal(
        session,
        build,
        BuildStatus.FAILED,
        EventType.BUILD_FAILED,
        clock=EventClock(at),
        triggered_by=None,
        error_message=error_message,
        metadata=metadata,
    )


async def fail_build_for_conflicts(
    session: AsyncSession,
    environment_id: UUID,
    plan: Plan,
    conflicts: Sequence[ClosureConflict],
    *,
    at: datetime,
) -> bool:
    """Fail the build over closure conflicts (``BUILD_FAILED``), once,
    inside the caller's transaction. Returns True when the build is failed
    (now or already)."""
    build = await lock_build(session, environment_id, plan.build_id)
    if build.status == BuildStatus.FAILED:
        return True
    message = "instance_conflict: " + "; ".join(
        f"plan {plan.id} holds instance {c.member_instance_id} of task"
        f" {c.task_id}, and an edge reaches instance {c.other_instance_id}"
        f" (fields that differ: {', '.join(c.fields) or '-'})"
        for c in conflicts
    )
    await _terminal(
        session,
        build,
        BuildStatus.FAILED,
        EventType.BUILD_FAILED,
        clock=EventClock(at),
        triggered_by=None,
        error_message=message,
        metadata={
            "reason": "instance_conflict",
            "plan_id": str(plan.id),
            "conflicts": [
                {
                    "task_id": c.task_id,
                    "member_instance_id": str(c.member_instance_id),
                    "other_instance_id": str(c.other_instance_id),
                    "fields": c.fields,
                }
                for c in conflicts
            ],
        },
    )
    return True


# ---------------------------------------------------------------------------
# Resume
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResumeResult:
    build: Build
    #: The caller's plan, if one exists for its scope (now active unless it
    #: is an unsealed replacement, which its seal activates); None when the
    #: caller must create one (``POST /builds/{id}/plans``).
    plan: PlanState | None
    #: True when this call changed anything (status or active plan).
    changed: bool


async def resume_build(
    session: AsyncSession,
    environment_id: UUID,
    build_id: UUID,
    *,
    deployment_id: UUID | None = None,
    settings: Mapping[str, str] | None = None,
    executor_metadata: dict[str, Any] | None = None,
) -> ResumeResult:
    """Make the build RUNNING again (``BUILD_RESUMED``) and, when the caller
    names its scope, reuse or reactivate the plan for it.

    A plan that was active before (superseded since) is reactivated — the
    active one superseded — after the same deployment check a seal makes;
    an unsealed replacement is left to its seal; no plan means the caller
    runs discovery and creates one. The driver then re-sends its
    observations either way. Idempotent by state.
    """
    async with transaction(session):
        build = await lock_build(session, environment_id, build_id)
        now = utc_now()
        changed = False
        plan: Plan | None = None
        if deployment_id is not None:
            shash = settings_hash(validate_settings(settings or {}))
            plan = await session.scalar(
                select(Plan).where(
                    Plan.build_id == build.id,
                    Plan.deployment_id == deployment_id,
                    Plan.settings_hash == shash,
                )
            )
            if plan is not None and plan.superseded_at is not None:
                await verify_deployment_current(session, environment_id, deployment_id)
                await _refuse_pending_replacement(session, plan)
                current = await active_plan(session, build.id)
                if current is not None:
                    current.superseded_at = now
                    await session.flush()  # the one-active-plan index
                plan.superseded_at = None
                plan.activated_at = now
                changed = True
        if build.status != BuildStatus.RUNNING:
            build.status = BuildStatus.RUNNING
            build.completed_at = None
            build.status_triggered_by_user_id = None
            changed = True
        if executor_metadata is not None:
            build.executor_metadata = executor_metadata
        if changed:
            build.is_resumed = True
            build.last_active_at = now
            await event_log.append(
                session,
                [
                    event_log.event_row(
                        environment_id,
                        EventType.BUILD_RESUMED,
                        at=now,
                        build_id=build.id,
                        metadata={"plan_id": str(plan.id)} if plan else None,
                    )
                ],
            )
        await session.flush()
        return ResumeResult(
            build=build,
            plan=PlanState.of(plan) if plan is not None else None,
            changed=changed,
        )


async def _refuse_pending_replacement(session: AsyncSession, plan: Plan) -> None:
    """The seal's "no higher generation" rule, as it applies to a
    reactivation: a later request for the build that has not yet been
    activated (an unsealed replacement, registering) wins over the older
    plan a resume would bring back — 409 ``plan_superseded``. Plans that
    were active and have since been superseded or are active now do not
    count: moving between the build's recorded requests is what a resume
    is for."""
    pending = await session.scalar(
        select(Plan.id)
        .where(
            Plan.build_id == plan.build_id,
            Plan.generation > plan.generation,
            Plan.activated_at.is_(None),
        )
        .limit(1)
    )
    if pending is not None:
        raise Conflict(
            "plan_superseded",
            "a later request for this build is being registered; the latest"
            " request wins",
            plan_id=str(plan.id),
            pending_plan_id=str(pending),
        )


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


async def delete_build(
    session: AsyncSession, environment_id: UUID, build_id: UUID
) -> None:
    """Delete a build, refused (409 ``build_has_live_work``) while any of
    its plans holds a live claim or any of its executions has not reported
    its end — ``builds stop`` ends those first, so the ledger is never
    cascaded away under a worker that may still report. Plans, members and
    executions cascade; events keep their rows with the pointers NULL."""
    async with transaction(session):
        build = await lock_build(session, environment_id, build_id)
        now = utc_now()
        plans = select(Plan.id).where(Plan.build_id == build.id)
        live_claim = await session.scalar(
            select(
                exists().where(
                    Task.claim_plan_id.in_(plans),
                    Task.status == TaskStatus.RUNNING,
                    Task.claim_expires_at > now,
                )
            )
        )
        unended = await session.scalar(
            select(
                exists().where(
                    Execution.plan_id.in_(plans), Execution.ended_at.is_(None)
                )
            )
        )
        if live_claim or unended:
            raise Conflict(
                "build_has_live_work",
                "the build holds a live claim or has executions that have not"
                " reported their end; stop them first (stardag builds stop)",
                build_id=str(build.id),
                live_claim=bool(live_claim),
                unended_executions=bool(unended),
            )
        await session.execute(delete(Build).where(Build.id == build.id))
