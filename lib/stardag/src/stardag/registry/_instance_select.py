"""Which instance of a task to use when none is pinned.

A task id may hold many instances: every build that registers the task
under a new deployment adds one, whose non-significant fields reflect that
deployment, whether or not it ever ran the task. So "the newest instance"
of a completed task is usually one that did not produce its output, and
the one that did may not even be among the newest a ``task_get`` lists.

The execution ledger records which instance an execution ran
(``ExecutionInfo.instance_id``) and how it ended. :func:`select_instance`
uses it:

- a task whose status is completed: the instance of the execution that
  produced its *current* completion (its outcome and claim outcome both
  completed, and its end the task's ``completed_at``), read by id
  (``task_instance_get``, which finds it even when it is not among the
  listed newest);
- otherwise (not completed; completed with no execution recorded, e.g. a
  completion observed at discovery; or a registry without the executions
  route): the newest instance.

Shared by ``from_registry``, unpinned ``TaskRef`` hydration and
``stardag tasks ref`` / ``tasks show``.
"""

from __future__ import annotations

import datetime
import typing
from dataclasses import dataclass

from stardag.exceptions import APIError, NotFoundError, StardagError

if typing.TYPE_CHECKING:
    from stardag.registry._base import RegistryABC
    from stardag.registry._models import ExecutionInfo, TaskInfo, TaskInstanceInfo

#: ``TaskInfo.status`` of a completed task, and ``ExecutionInfo.outcome`` of
#: an execution that completed it (the server's enum values).
COMPLETED = "completed"

#: Why an instance was chosen.
COMPLETED_EXECUTION = "completed_execution"
NEWEST = "newest"


class NoInstanceError(StardagError):
    """The task has no instance to choose from."""


@dataclass(frozen=True)
class SelectedInstance:
    """The chosen instance, and why."""

    instance: TaskInstanceInfo
    reason: str
    #: Human-readable, e.g. "instance 0193… (the execution that completed it)".
    explanation: str


#: How many executions are searched (the route's maximum page; it has no
#: cursor). A completing execution older than that is not found, and the
#: newest instance is used instead.
EXECUTION_SEARCH_LIMIT = 500
#: The completing report writes the execution's ``ended_at`` and the task's
#: ``completed_at`` from one clock reading; this only absorbs rounding.
_SAME_COMPLETION = datetime.timedelta(seconds=1)


def completed_execution(
    registry: RegistryABC,
    task_id: str,
    *,
    completed_at: datetime.datetime | None = None,
) -> ExecutionInfo | None:
    """The execution that produced the task's **current** completion, or
    ``None``.

    The newest execution whose outcome and claim outcome are both completed
    (a rejected late report also records ``outcome="completed"``, with
    another claim outcome) and which records its instance. When the task's
    ``completed_at`` is given, that execution must also have ended at that
    moment: the completing report writes both from one clock reading. A
    later ``completed_at`` means the current completion came from elsewhere
    (the task was invalidated, then observed complete at discovery), and no
    execution produced it. ``None`` too for a registry without
    ``GET /tasks/{id}/executions``, or when the completing execution is not
    among the newest ``EXECUTION_SEARCH_LIMIT``.
    """
    try:
        executions = registry.task_list_executions(
            task_id, include_ended=True, limit=EXECUTION_SEARCH_LIMIT
        )
    except NotImplementedError:
        return None
    except NotFoundError:
        return None  # an older server without the route
    except APIError as e:
        if e.status_code == 404:
            return None
        raise
    for execution in executions:  # newest first
        if (
            execution.outcome == COMPLETED
            and execution.claim_outcome == COMPLETED
            and execution.instance_id is not None
        ):
            if (
                completed_at is not None
                and execution.ended_at is not None
                and abs(_utc(completed_at) - _utc(execution.ended_at))
                > _SAME_COMPLETION
            ):
                return None
            return execution
    return None


def _utc(moment: datetime.datetime) -> datetime.datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=datetime.timezone.utc)
    return moment


def select_instance(
    registry: RegistryABC, task_id: str, *, info: TaskInfo | None = None
) -> SelectedInstance:
    """The instance to use for ``task_id`` when none is pinned (see the
    module docstring). ``info``: the task's ``task_get``, when already read.

    Raises:
        NoInstanceError: The task has no instance.
    """
    info = info if info is not None else registry.task_get(task_id)
    if (info.status or "").lower() == COMPLETED:
        execution = completed_execution(
            registry, info.task_id, completed_at=info.completed_at
        )
        if execution is not None and execution.instance_id is not None:
            instance = registry.task_instance_get(
                execution.instance_id, task_id=info.task_id
            )
            return SelectedInstance(
                instance=instance,
                reason=COMPLETED_EXECUTION,
                explanation=(
                    f"instance {instance.id} (the execution that completed it)"
                ),
            )
        why = "no execution recorded for its current completion"
    else:
        why = f"task status is {info.status or 'unknown'}, not completed"
    if not info.instances:
        raise NoInstanceError(f"Task {info.task_id} has no instance.")
    newest = info.instances[0]
    if newest.task_id is None:
        newest = newest.model_copy(update={"task_id": info.task_id})
    return SelectedInstance(
        instance=newest,
        reason=NEWEST,
        explanation=f"newest instance {newest.id} ({why})",
    )
