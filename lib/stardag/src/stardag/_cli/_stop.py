"""Selecting and stopping a build's unended executions, for ``builds stop``.

The command lives in :mod:`stardag._cli.builds_stop`; this is the part
worth testing without a CLI runner: which executions can be stopped from
here, what the filters mean, and what cancelling one Modal call does.

**The list is the execution ledger.** ``GET /builds/{id}/executions``
returns the build's executions with ``ended_at IS NULL`` — no report of
their end has arrived — whatever has happened to their claim since (an
execution ref is not a claim; design.md, "``execution`` — the ledger"). So
the list is exact at any moment, before or after a cancel, and a task
another build took over never shows up here under this build: its new
execution belongs to the other build's plan. v1 had to read the list while
the build still held its claims; v2 does not.

**An orphan** is an unended execution whose plan is not its build's active
plan (design.md, "Rollover"): it was started under a plan a rollover or a
re-trigger under new settings superseded. ``--not-in-current-plan`` keeps
only those; the server filters (``in_current_plan``).

**What can be stopped**: an execution on the ``modal`` executor with a
recorded call id (``executor_ref``). Everything else is listed with the
reason and left alone: an in-process execution of a resident driver, an
execution on another executor, and a claim whose spawn has not reported
its call id yet (re-run a few seconds later to catch it).

Each stopped call is reported with ``POST /executions/{id}/stopped``
(outcome ``stopped``), which ends it on the ledger and releases its claim
if it still holds one. A call that could not be cancelled is **not**
reported: nothing is known about it, and a stop written for a container
that is still running would make its eventual completion a late report.
``--mark-lost`` is the operator's explicit end for an execution with no call
id (outcome ``lost``): no report of it will ever be applied.

The UI's ``utils/stoppable.ts`` mirrors these rules; this module is the
reference.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from stardag._cli._output import as_utc
from stardag.registry import ExecutionInfo

# The executor this command can stop. Everything else is listed and left
# alone, with a reason.
MODAL_EXECUTOR = "modal"

NO_REF_YET = "no call id recorded yet — it was claimed but not yet spawned"
NO_EXECUTOR = (
    "no executor recorded — it runs in its driver's own process, or its "
    "spawn has not reported yet; re-run to see whether a call id appears"
)


def executor_of(execution: ExecutionInfo) -> str | None:
    """The recorded executor, or the ``kind`` its metadata declares (a
    claim written before its spawn reported)."""
    kind = (execution.executor_metadata or {}).get("kind")
    return execution.executor or (kind if isinstance(kind, str) else None)


def not_stoppable_reason(execution: ExecutionInfo) -> str | None:
    """Why ``execution`` can only be listed, or None if it can be stopped.

    Order matters: an execution naming no executor must be caught before
    the Modal comparison, or it is promised a call id that may never exist.
    """
    executor = executor_of(execution)
    if not executor:
        return NO_EXECUTOR
    if executor != MODAL_EXECUTOR:
        return f"stardag cannot stop a {executor!r} execution"
    if not execution.executor_ref:
        return NO_REF_YET
    return None


def is_stoppable(execution: ExecutionInfo) -> bool:
    return not_stoppable_reason(execution) is None


def worker_of(execution: ExecutionInfo) -> str | None:
    """The worker name the app declares (Modal's ``worker_`` prefix
    stripped from the recorded function name)."""
    name = (execution.executor_metadata or {}).get("function_name")
    if not isinstance(name, str) or not name:
        return None
    return name.removeprefix("worker_")


@dataclass(frozen=True)
class Filters:
    """What the operator narrowed the list to (conjunctive). Empty means
    everything the server returned."""

    executor: str | None = None
    worker: str | None = None
    older_than_seconds: int | None = None
    task_ids: tuple[str, ...] = ()

    @property
    def any_set(self) -> bool:
        return any((self.executor, self.worker, self.older_than_seconds, self.task_ids))

    def matches(self, execution: ExecutionInfo, *, now: datetime) -> bool:
        if self.task_ids and execution.task_id not in self.task_ids:
            return False
        if self.executor is not None and executor_of(execution) != self.executor:
            return False
        if self.worker is not None and worker_of(execution) != self.worker:
            return False
        if self.older_than_seconds is not None:
            # An age that cannot be established is not evidence of age.
            if execution.started_at is None:
                return False
            running = (now - as_utc(execution.started_at)).total_seconds()
            if running < self.older_than_seconds:
                return False
        return True


def split(
    executions: Sequence[ExecutionInfo],
    filters: Filters,
    *,
    now: datetime | None = None,
) -> tuple[list[ExecutionInfo], list[ExecutionInfo]]:
    """``(selected, excluded_by_filter)``, one evaluation per execution."""
    reference = now or datetime.now(timezone.utc)
    selected: list[ExecutionInfo] = []
    excluded: list[ExecutionInfo] = []
    for execution in executions:
        (selected if filters.matches(execution, now=reference) else excluded).append(
            execution
        )
    return selected, excluded


class ModalUnavailable(RuntimeError):
    """The ``modal`` package is not importable, so nothing can be stopped."""


@dataclass(frozen=True)
class CancelOutcome:
    execution: ExecutionInfo
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def cancel_modal_calls(executions: Iterable[ExecutionInfo]) -> list[CancelOutcome]:
    """Cancel each execution's Modal function call; one failure never
    aborts the rest. The import is deferred so the CLI works without the
    ``modal`` extra until something is actually about to be stopped."""
    try:
        import modal
    except ImportError as error:  # pragma: no cover - environment-dependent
        raise ModalUnavailable(
            "Stopping a Modal execution needs the modal package "
            f"({error}); install stardag with its modal extra, or stop the "
            "calls from the Modal dashboard."
        ) from error

    outcomes: list[CancelOutcome] = []
    for execution in executions:
        if not execution.executor_ref:
            outcomes.append(CancelOutcome(execution, error=NO_REF_YET))
            continue
        try:
            modal.FunctionCall.from_id(execution.executor_ref).cancel()
        except Exception as error:  # noqa: BLE001 - reported, never fatal
            outcomes.append(
                CancelOutcome(execution, error=f"{type(error).__name__}: {error}")
            )
        else:
            outcomes.append(CancelOutcome(execution))
    return outcomes


def modal_workspaces(executions: Iterable[ExecutionInfo]) -> set[str]:
    """Modal workspaces the executions were started in: ``from_id`` resolves
    against the ambient Modal profile, so a call in another workspace is
    simply not found."""
    found: set[str] = set()
    for execution in executions:
        workspace = (execution.executor_metadata or {}).get("workspace")
        if isinstance(workspace, str) and workspace:
            found.add(workspace)
    return found


def execution_json(execution: ExecutionInfo) -> dict[str, Any]:
    """One execution as a JSON object, with the stop verdict."""
    data = execution.model_dump(mode="json")
    data["worker"] = worker_of(execution)
    data["stoppable"] = is_stoppable(execution)
    data["not_stoppable_reason"] = not_stoppable_reason(execution)
    return data
