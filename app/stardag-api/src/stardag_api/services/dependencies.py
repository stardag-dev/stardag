"""A task's declared static dependencies are immutable.

A task id promises the world state its completion establishes, and that
state includes the upstream set it was built from. So changing what a task
requires changes what it promises, and its id has to change with it — via
``__version__`` or a hash-significant parameter.

That is a user obligation, like "same id, same output", and unverifiable in
general. But once a task has been registered the registry holds its
declaration, and from then on it can check: a later declaration that differs
says the obligation was not met, by this code version or an earlier one.

The check does not arbitrate between builds. It asks nothing about who else
is running, when they started, or whether they are still alive — it compares
two sets. Everything that made the earlier, retraction-based design hard to
reason about was a question about *time*; there are none here.

See ``docs/design/immutable-dependency-declarations.md``.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.models import Task, TaskDependency, TaskStatus

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DeclarationChanged:
    """A task's declared static upstreams differ from what was recorded.

    Carries both sets rather than just the difference, because the message
    built from this is the entire user experience of the refusal and a
    reader needs to see what the task used to require as well as what it
    requires now.
    """

    task_id: str
    # ``namespace.name`` where the registry knows it. A task id is an opaque
    # content hash, and a refusal that offers only hashes tells the reader
    # nothing about which *class* to go and look at.
    task_label: str | None
    declared: list[str]
    recorded: list[str]

    @property
    def dropped(self) -> list[str]:
        return sorted(set(self.recorded) - set(self.declared))

    @property
    def added(self) -> list[str]:
        return sorted(set(self.declared) - set(self.recorded))


async def find_changed_declaration(
    db: AsyncSession,
    *,
    declarations: Sequence[tuple[Task, Sequence[str]]],
    environment_id: UUID,
) -> DeclarationChanged | None:
    """The first declaration in ``declarations`` that contradicts the record.

    ``declarations`` pairs a task row with the upstream **task ids** it
    declares. Only tasks the caller is actually declaring for belong here:
    a registration that says nothing about dependencies is not a
    declaration and must not be compared (see ``TaskCreate``), and neither
    is one for a task discovery pruned at because it was already complete.

    Upstreams are compared by ``task_id``, not by primary key, so an
    upstream that has no row yet — an out-of-band caller naming one ahead of
    its registration — still counts as declared, and therefore appears in
    the refusal. It cannot be in the *recorded* set by construction, which
    is exactly what makes it a difference.

    A task **nobody has declared for** never conflicts: there is no
    previous declaration to contradict, so the first one is simply
    recorded. That covers every task the environment has not seen before,
    which is the overwhelmingly common case and the one this must stay
    cheap for — it costs the single indexed read below and nothing else.

    "Nobody has declared for it" is ``Task.static_deps_declared``, not "it
    has no recorded edges". The two differ for exactly one shape and it is
    a common one: a task that was declared to require *nothing* writes no
    edges, so without the flag a leaf that later gains an upstream would
    read as a first declaration and be accepted.

    Returns None when every declaration agrees.
    """
    if not declarations:
        return None

    by_pk = {task.id: task for task, _ in declarations}
    rows = (
        await db.execute(
            select(TaskDependency.downstream_task_id, Task.task_id)
            .join(Task, Task.id == TaskDependency.upstream_task_id)
            .where(
                TaskDependency.downstream_task_id.in_(list(by_pk)),
                TaskDependency.is_dynamic.is_(False),
            )
        )
    ).all()
    recorded_by_downstream: dict[UUID, set[str]] = {pk: set() for pk in by_pk}
    for downstream_pk, upstream_task_id in rows:
        recorded_by_downstream[downstream_pk].add(upstream_task_id)

    for task, declared in declarations:
        # A completed task is never compared, and this is a *server-side*
        # rule rather than something the caller can be trusted to observe.
        #
        # Every engine here already declines to declare for a task it
        # pruned at, so in principle this is unreachable. In practice the
        # compatibility case that actually occurs is an **old SDK against a
        # new API** (``sdk_compat``): the hosted service upgrades first, and
        # an older SDK re-derives ``requires()`` for every task in the
        # chunk, complete ones included. Without this skip, upgrading the
        # server would start refusing those callers' builds over tasks
        # nobody is going to build — with a remedy (bump the version) that
        # rebuilds the whole downstream cone.
        #
        # It is also right on its own terms. A complete task gates nothing,
        # plan closure prunes at one, and nothing will schedule it; there is
        # no work for a refusal to protect.
        if task.latest_status == TaskStatus.COMPLETED:
            continue
        recorded = recorded_by_downstream[task.id]
        # A task nobody has declared for is being declared for the first
        # time, which contradicts nothing. The flag is what makes that
        # different from "declared to require nothing": an empty
        # declaration writes no edges, so without it a leaf that later
        # gains an upstream would read as a first declaration and slip
        # past — and leaves gaining a dependency is a common shape.
        if not recorded and not task.static_deps_declared:
            continue
        if set(declared) != recorded:
            logger.info(
                "Task %s declares static upstreams %s but %s were recorded.",
                task.task_id,
                sorted(set(declared)),
                sorted(recorded),
            )
            return DeclarationChanged(
                task_id=task.task_id,
                task_label=_label(task),
                declared=sorted(set(declared)),
                recorded=sorted(recorded),
            )
    return None


def _label(task: Task) -> str | None:
    """``namespace.name``, or None for a row that carries neither."""
    parts = [p for p in (task.task_namespace, task.task_name) if p]
    return ".".join(parts) or None


def declaration_changed_message(changed: DeclarationChanged) -> str:
    """The refusal, as the person who triggered the build will read it.

    This message is the whole feature from the outside: a build refused for
    this reason looks exactly like stardag declining to run. So it says what
    changed, why that is not allowed, and what to do — in that order, and
    without assuming the reader has met this rule before.
    """
    changes = []
    if changed.dropped:
        changes.append(f"no longer requires {', '.join(changed.dropped)}")
    if changed.added:
        changes.append(f"now requires {', '.join(changed.added)}")
    named = (
        f"Task {changed.task_label} ({changed.task_id})"
        if changed.task_label
        else f"Task {changed.task_id}"
    )
    return (
        f"{named} declares different static dependencies than "
        f"the ones recorded for it: it {' and '.join(changes)}.\n"
        f"\n"
        f"  recorded: {', '.join(changed.recorded) or '(none)'}\n"
        f"  declared: {', '.join(changed.declared) or '(none)'}\n"
        f"\n"
        f"A task's id promises the state its completion establishes, and "
        f"that includes what it was built from — so changing what it "
        f"requires has to change its id. Bump the task's __version__, or "
        f"make the difference a parameter that counts towards the hash, and "
        f"trigger again.\n"
    )
