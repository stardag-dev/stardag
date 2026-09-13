"""Retracting dependency edges that no longer describe how a task is built.

**A dependency edge is evidence asserted by an act.** A static edge is
declared, in full, by every build that registers the task. A dynamic edge is
*discovered*: one execution attempt of the downstream task yielded this
upstream and suspended. The two therefore go stale for different reasons and
need different rules, and this module implements the second one — an edge
stops counting when the attempt that produced it is abandoned.

A static edge is not abandoned by an execution, so retraction never
touches one. It goes stale a different way — a later registration of the
same task declares a different set — and that is what
:func:`reconcile_static_declaration` below handles, on the rule that the
declaration is authoritative and a *disagreement between live builds* is a
conflict rather than something to reconcile silently.

**Scope of the "one attempt at a time" assumption.** A retraction is keyed
by the task, not by the attempt that recorded the edges, which is sound as
far as the execution claim reaches: a claimed task has one executing build
at a time, so its attempts are sequential. Under ``claim=None`` a local
executor takes no claim, and two such builds can run one task at once — in
which case either one's reset retracts the other's edges. That configuration
is already outside what the claim promises (both runs write the same
target); binding an edge to its attempt is the general fix, and the same
missing identity would close the late-write race a worker of an abandoned
attempt can still win.

Why it is needed at all: dynamic edges are written ``ON CONFLICT DO
NOTHING``, so a task's set only ever grew across attempts. Usually invisible,
because a previous generation's children are COMPLETED and completed
upstreams do not gate. It bites when an attempt is abandoned with its
children incomplete — a cancelled build is the ordinary way — and then the
children gate *their own parent* forever: the task cannot be scheduled until
they complete, so the next build that wants it resets and re-runs a whole
generation of work the task is no longer going to ask for, before it ever
gets to re-yield.

**All of the attempt's edges are retracted, not only the incomplete ones.**
The completed children are just as much a statement about that attempt, and
retracting only the stragglers would leave a task whose recorded generation
is half one attempt and half another — which is not a state any rule can
reason about afterwards. Nothing is lost by retracting a completed child:
its target still exists, so if the next attempt yields it again it is
complete on arrival, and the edge comes back with the next yield.
"""

from __future__ import annotations

import logging
from datetime import datetime
from uuid import UUID

from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.models import (
    Build,
    BuildStatus,
    Event,
    EventType,
    Task,
    TaskDependency,
    TaskStatus,
)
from stardag_api.models.base import utc_now

logger = logging.getLogger(__name__)


async def retract_dynamic_edges_if_new_attempt(
    db: AsyncSession,
    task: Task,
    *,
    previous_status: TaskStatus,
    previous_status_build_id: UUID | None,
    event: Event,
    now: datetime | None = None,
) -> None:
    """Supersede ``task``'s dynamic edges when a fresh attempt begins.

    Two transitions mean that, and only these two:

    **A reset to PENDING.** The task will run again from the top, so
    whatever the last attempt yielded is not what the next one will need.
    This is also the transition that *has* to carry the retraction rather
    than the start that follows it: the stale children gate the task
    itself, so it can never reach a start while they are recorded. Reaching
    for the start instead would be a rule that cannot fire.

    **A start of a SUSPENDED task by a different build.** Taking over an
    abandoned suspension is a new attempt of the same shape — the worker
    re-runs the generator from the top — so the previous attempt's
    generation goes with it. Takeover itself stays unrestricted: handing a
    suspension between builds is pure benefit when the code is identical,
    which is the normal case, and retracting makes the cost of a takeover
    proportional to how much the code actually diverges.

    A build resuming *its own* suspension is the ordinary multi-round walk,
    not a new attempt: its generator advances past the batches it has
    already completed and yields the next one, which is exactly what the
    accumulated edges describe. Retracting there would throw away the
    record of a walk still in progress.

    No-ops for every other transition, and cheap when it does run: one
    UPDATE that matches nothing for the overwhelming majority of tasks,
    which have no dynamic edges at all.
    """
    if not _begins_new_attempt(
        task,
        previous_status=previous_status,
        previous_status_build_id=previous_status_build_id,
        event=event,
    ):
        return

    result = await db.execute(
        update(TaskDependency)
        .where(
            TaskDependency.downstream_task_id == task.id,
            TaskDependency.is_dynamic.is_(True),
            TaskDependency.superseded_at.is_(None),
        )
        .values(superseded_at=now or utc_now())
    )
    retracted = getattr(result, "rowcount", 0) or 0
    if retracted:
        logger.info(
            "Task %s begins a new attempt; retracted %d dynamic dependency "
            "edge(s) from the abandoned one.",
            task.task_id,
            retracted,
        )


def _begins_new_attempt(
    task: Task,
    *,
    previous_status: TaskStatus,
    previous_status_build_id: UUID | None,
    event: Event,
) -> bool:
    if event.event_type == EventType.TASK_RETRIED:
        # Gated on the task actually having moved: TASK_RETRIED is recorded
        # whether or not the status was retryable (which is what makes
        # concurrent trigger/retry races benign), and a retry that changed
        # nothing has abandoned nothing.
        return (
            task.latest_status == TaskStatus.PENDING
            and previous_status != TaskStatus.PENDING
        )
    if event.event_type == EventType.TASK_STARTED:
        # A NULL previous owner counts as a takeover, not as "mine". The
        # column is ``ON DELETE SET NULL``, so a suspension whose build has
        # been deleted lands here, as does a row predating the owner
        # backfill — and in both cases whoever is starting it now is not the
        # build that suspended it. Plan closure already reads a missing
        # owner as abandoned; treating it as present here would keep an
        # abandoned generation current while the other half of the rule
        # assumes it is gone.
        return (
            previous_status == TaskStatus.SUSPENDED
            and previous_status_build_id != event.build_id
        )
    return False


@dataclass(frozen=True)
class StaticDeclarationConflict:
    """Two live builds disagree about how a task is built.

    A task id promises a world state, not a provenance, so changing how a
    task is produced — a different upstream, a different partitioning — and
    keeping the id is correct. What is not correct is two builds
    materialising it over different upstream DAGs at the same time: both
    are legitimate, both write the same target, and one of them is wasted
    by construction.

    Unlike a dynamic fan-out, this really can happen concurrently. `U1` and
    `U2` are different tasks with no claim between them, so nothing
    serialises the two builds the way the execution claim serialises two
    attempts at one task.
    """

    task_id: str
    declared: list[str]
    recorded: list[str]
    build_ids: list[UUID]


async def reconcile_static_declaration(
    db: AsyncSession,
    *,
    downstream: Task,
    declared_upstream_pks: set[UUID],
    build_id: UUID,
    now: datetime | None = None,
) -> StaticDeclarationConflict | None:
    """Make the declared static upstream set authoritative for ``downstream``.

    A static edge is *declared*, in full, by every build that registers the
    task — so unlike a dynamic edge the server knows the current set exactly,
    and an edge the latest declaration omits has stopped describing how the
    task is built. Superseding it is what lets a task be re-pointed at a new
    upstream without minting a new id for a downstream whose promise has not
    changed.

    **Except when somebody else is still building it that way.** If a live
    build holds the task, superseding would rewrite the gates under a build
    that is running right now — so the difference is reported instead, and
    the caller refuses the registration. Returns the conflict and writes
    nothing; returns None (having superseded) when the change is uncontested.

    Nothing to drop is the overwhelmingly common case — the same code
    registering the same task — and costs one indexed read.
    """
    recorded = (
        await db.execute(
            select(TaskDependency.upstream_task_id, Task.task_id)
            .join(Task, Task.id == TaskDependency.upstream_task_id)
            .where(
                TaskDependency.downstream_task_id == downstream.id,
                TaskDependency.is_dynamic.is_(False),
                TaskDependency.superseded_at.is_(None),
            )
        )
    ).all()
    dropped = {
        pk: task_id for pk, task_id in recorded if pk not in declared_upstream_pks
    }
    if not dropped:
        return None

    # A completed task is nobody's to build, so two declarations about it
    # cannot both be materialised and there is nothing to refuse. Worth
    # skipping explicitly rather than leaving to chance: discovery prunes
    # *below* a complete task but still registers it, so every build sends a
    # declaration for every complete task in its closure — and those are the
    # ones most likely to have been registered long ago, under older code.
    # Checking them would refuse triggers over a disagreement about work
    # that is already done.
    #
    # The edges are still brought up to date. Nothing reads them for
    # scheduling — a complete task gates nothing, and plan closure prunes at
    # one — so this is bookkeeping, and bookkeeping that keeps the recorded
    # graph matching the code that last described it.
    holders = (
        []
        if downstream.latest_status == TaskStatus.COMPLETED
        else await _live_builds_holding(db, downstream, exclude=build_id)
    )
    if holders:
        declared_ids = (
            (
                await db.execute(
                    select(Task.task_id).where(Task.id.in_(declared_upstream_pks))
                )
            )
            .scalars()
            .all()
            if declared_upstream_pks
            else []
        )
        return StaticDeclarationConflict(
            task_id=downstream.task_id,
            declared=sorted(declared_ids),
            recorded=sorted(task_id for _, task_id in recorded),
            build_ids=holders,
        )

    await db.execute(
        update(TaskDependency)
        .where(
            TaskDependency.downstream_task_id == downstream.id,
            TaskDependency.upstream_task_id.in_(list(dropped)),
            TaskDependency.is_dynamic.is_(False),
            TaskDependency.superseded_at.is_(None),
        )
        .values(superseded_at=now or utc_now())
    )
    logger.info(
        "Task %s no longer declares %d static upstream(s); superseded.",
        downstream.task_id,
        len(dropped),
    )
    return None


async def _live_builds_holding(
    db: AsyncSession, task: Task, *, exclude: UUID
) -> list[UUID]:
    """Builds other than ``exclude`` that are RUNNING and hold ``task``.

    "Holds" is plan membership — an event for the task — which is the same
    relation the wake-up flag uses. That reads as wider than "declared these
    edges", and is *nearly* the same thing in practice, which is worth
    knowing before anyone tightens it: discovery walks ``requires()`` and
    prunes only at **complete** tasks, never at already-registered ones, so
    every build registers every task in its own closure with its own
    declaration. Holding a task without having declared anything about it
    therefore needs plan closure to have admitted it — which, for static
    edges, is the very thing this function has just made current. What is
    left is inheritance over a *dynamic* edge: another build's fan-out
    children, which this build may still run.

    So the residual imprecision is one shape, it is a build that can run the
    task, and the cost of being wrong there is a refused trigger with an
    actionable message — against two builds crunching the same data over
    different upstream DAGs if it were wrong the other way.
    """
    holders = (
        (
            await db.execute(
                select(Build.id)
                .where(
                    Build.id.in_(
                        select(Event.build_id)
                        .where(Event.task_id == task.id)
                        .distinct()
                        .scalar_subquery()
                    ),
                    Build.id != exclude,
                    Build.environment_id == task.environment_id,
                    Build.latest_status == BuildStatus.RUNNING,
                )
                .order_by(Build.id)
            )
        )
        .scalars()
        .all()
    )
    return list(holders)
