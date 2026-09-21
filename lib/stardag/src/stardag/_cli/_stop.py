"""Selecting and stopping a build's live executions, for ``builds stop``.

The command itself lives in :mod:`stardag._cli.builds`; everything here is
the part worth testing without a CLI runner — which of a build's tasks are
stoppable, what the filters mean, and what cancelling one Modal call does.

**Why the list is read while the claims are still held.** Cancelling a
build releases its claims, and from that instant another build may take a
task over and start its own execution on it. The task row then names
*somebody else's* call, so anything that reads it afterwards either misses
the execution it wanted or kills one it does not own; both happened, and
reconstructing the difference from the event log is the machinery this
command exists to replace. Listing first makes the row exact: it is read
at a moment when only this build can be on it.

That is also why the selection rule is as small as it is. A task is this
build's to stop when its row says so:

    latest_status_build_id == build
    latest_status in (running, interrupted)
    latest_executor_ref is set

No ranking, no event walk, no authority rules. If a neighbour had taken
the task over the row would carry the neighbour's build id and the task
would not be selected — the same single check that makes the list exact
makes it safe.

The two statuses, and the two it leaves out:

- **RUNNING** is the claim. It also covers preemption on its own: a
  preemption leaves the status and the executor ref alone and only pulls
  the claim's expiry forward to a restart-sized grace, so there is no
  separate status to select for. ``latest_preempted_at`` later than
  ``latest_status_at`` means the restart has not landed yet, which the
  table flags — the call id is the same one the restart will reuse, so it
  is still the right thing to cancel.
- **INTERRUPTED** has released its claim, but ``services.status`` leaves
  the executor ref in place deliberately: an interruption does not always
  mean the execution is gone (a backend with its own retries may be
  restarting the very same call). A ref that may still be live is exactly
  what this command is for.
- **SUSPENDED** keeps a ref too, but that execution yielded and *returned*
  — there is nothing running to stop.
- **PENDING** and every terminal status hold no execution at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Iterable, Sequence
from uuid import UUID

if TYPE_CHECKING:  # pragma: no cover - typing only
    from stardag.registry import RegistryABC, TaskSummary

# The executor this command can actually stop. Everything else is listed
# and left alone — with a reason, rather than silently dropped, because an
# execution nobody told you about is the thing the ordering is protecting.
MODAL_EXECUTOR = "modal"

# Statuses whose row may still name a live execution (see the module
# docstring for why these two and not the others).
STOPPABLE_STATUSES: tuple[str, ...] = ("running", "interrupted")

# Rows per page when scanning the environment's claim holders. The
# server's maximum; the population is "tasks holding a claim", so one page
# covers all but the largest environments.
_PAGE_SIZE = 100

# A hard stop on the scan, so a pathological environment fails loudly
# instead of paging forever. 200 pages is 20 000 simultaneous claim
# holders — far past anything real, and far past anything a human is going
# to review in a table.
_MAX_PAGES = 200


class TooManyClaimHolders(RuntimeError):
    """The environment has more claim holders than the scan will page.

    Raised rather than truncated on purpose. A truncated list would let
    ``builds stop`` release the build's claims having stopped only some of
    its containers — the precise failure the command is built to prevent —
    and it would do it without saying so.
    """


@dataclass(frozen=True)
class Execution:
    """One of a build's live executions, as the task row describes it."""

    task_id: str
    task_namespace: str
    task_name: str
    status: str
    executor: str
    executor_ref: str
    executor_metadata: dict[str, Any]
    # When the task entered its current status — "running since", and the
    # column the ``--older-than`` filter and the "running for" cell read.
    status_at: datetime | None
    # The platform said it was restarting this execution and the restart
    # has not landed. See ``restart_due``.
    preempted_at: datetime | None

    @property
    def qualified_name(self) -> str:
        """``namespace.Name``, or just ``Name`` in the default namespace."""
        if self.task_namespace:
            return f"{self.task_namespace}.{self.task_name}"
        return self.task_name

    @property
    def worker(self) -> str | None:
        """The worker this execution runs on, as the metadata records it.

        Modal names the function ``worker_<name>``; the bare name is what
        the app declares and what ``--worker`` takes, so the prefix is
        stripped here rather than at every comparison.
        """
        function_name = self.executor_metadata.get("function_name")
        if not isinstance(function_name, str) or not function_name:
            return None
        prefix = "worker_"
        return (
            function_name[len(prefix) :]
            if function_name.startswith(prefix)
            else function_name
        )

    @property
    def stoppable(self) -> bool:
        """Whether this command can stop it, as opposed to only list it."""
        return self.executor == MODAL_EXECUTOR

    @property
    def restart_due(self) -> bool:
        """A preemption was reported and its restart has not arrived yet.

        Derived rather than stored, which is what keeps it honest: the
        restart records its own start, moving ``status_at`` past
        ``preempted_at``, and this goes false with nothing to clear.
        """
        if self.preempted_at is None:
            return False
        if self.status_at is None:
            return True
        return _as_utc(self.preempted_at) > _as_utc(self.status_at)


@dataclass(frozen=True)
class Filters:
    """What the user narrowed the list to. Empty means everything."""

    worker: str | None = None
    executor: str | None = None
    namespace: str | None = None
    older_than_seconds: int | None = None
    task_ids: tuple[str, ...] = ()

    @property
    def any_set(self) -> bool:
        return any(
            (
                self.worker,
                self.executor,
                self.namespace,
                self.older_than_seconds,
                self.task_ids,
            )
        )

    def matches(self, execution: Execution, *, now: datetime | None = None) -> bool:
        """Whether ``execution`` survives every filter that is set.

        Conjunctive: each flag narrows. ``--namespace`` is a *prefix*
        match, so ``--namespace acme`` covers ``acme.features`` as well as
        ``acme`` itself; the others are exact.
        """
        if self.task_ids and execution.task_id not in self.task_ids:
            return False
        if self.executor is not None and execution.executor != self.executor:
            return False
        if self.namespace is not None and not execution.task_namespace.startswith(
            self.namespace
        ):
            return False
        if self.worker is not None and execution.worker != self.worker:
            return False
        if self.older_than_seconds is not None:
            # A row with no status timestamp never matches a staleness
            # filter: an age that cannot be established is not evidence of
            # age. Same rule the server applies to ``status_older_than``.
            if execution.status_at is None:
                return False
            reference = now or datetime.now(timezone.utc)
            age = (reference - _as_utc(execution.status_at)).total_seconds()
            if age < self.older_than_seconds:
                return False
        return True


def _as_utc(value: datetime) -> datetime:
    """Interpret a naive timestamp as UTC (the API's own convention)."""
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def execution_from_task(task: "TaskSummary") -> Execution | None:
    """Build an :class:`Execution` from a task row, or None if it holds none.

    The whole selection rule for one row, minus the build check the caller
    makes: a status that may still have a container behind it, and a ref
    naming that container.
    """
    if task.latest_status not in STOPPABLE_STATUSES:
        return None
    if not task.latest_executor_ref:
        return None
    return Execution(
        task_id=task.task_id,
        task_namespace=task.task_namespace,
        task_name=task.task_name,
        status=task.latest_status or "",
        # A ref with no executor named is pre-``latest_executor`` data.
        # Treat it as Modal — the only executor that has ever recorded a
        # ref — rather than dropping a live container from the list.
        executor=task.latest_executor or MODAL_EXECUTOR,
        executor_ref=task.latest_executor_ref,
        executor_metadata=task.latest_executor_metadata or {},
        status_at=task.latest_status_at,
        preempted_at=task.latest_preempted_at,
    )


def collect_executions(registry: "RegistryABC", build_id: UUID) -> list[Execution]:
    """Every live execution the build currently holds, oldest claim first.

    Read from ``GET /tasks`` — the task row itself, denormalised columns
    and all — rather than from the frontier or the build's task list. The
    frontier carries no task name, and the build-scoped listing reports
    ``latest_started_at`` (first start ever, across builds) rather than
    when the current status began, which is the one number "running for"
    means. Ordering is the server's: with a status filter it returns
    oldest claim first, which is the order to review and to stop in.

    The scan is environment-wide and filtered to the build here, because
    the endpoint has no build filter. That is affordable: the population
    is the environment's claim holders, not its tasks.
    """
    executions: list[Execution] = []
    page = 1
    while True:
        result = registry.task_list(
            page=page,
            page_size=_PAGE_SIZE,
            status=STOPPABLE_STATUSES,
        )
        for task in result.tasks:
            if task.latest_status_build_id != build_id:
                continue
            execution = execution_from_task(task)
            if execution is not None:
                executions.append(execution)
        seen = (page - 1) * _PAGE_SIZE + len(result.tasks)
        if not result.tasks or seen >= result.total:
            break
        page += 1
        if page > _MAX_PAGES:
            raise TooManyClaimHolders(
                f"More than {_MAX_PAGES * _PAGE_SIZE} tasks in this "
                "environment are holding an execution claim, so the list "
                "of this build's executions cannot be completed. Stopping "
                "a build on a partial list would release its claims with "
                "containers still running, so this refuses rather than "
                "truncating."
            )
    return executions


@dataclass(frozen=True)
class CancelOutcome:
    """What happened to one execution's cancel request."""

    execution: Execution
    # None when the call was cancelled (or was already gone, which is the
    # same outcome); the error text otherwise.
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


class ModalUnavailable(RuntimeError):
    """``modal`` is not importable, so no Modal call can be cancelled."""


def cancel_modal_calls(executions: Sequence[Execution]) -> list[CancelOutcome]:
    """Cancel each execution's Modal function call, one at a time.

    Idempotent by construction: Modal's ``cancel`` on a call that has
    already finished, already been cancelled, or never existed is not an
    error worth stopping for, and a run of this command is expected to
    overlap with executions ending on their own. Each call is reported
    individually and a failure never aborts the rest — the alternative is
    a partial stop followed by a claim release, which is the ordering this
    command exists to get right.

    Import is deferred so the SDK's CLI keeps working without ``modal``
    installed: only this one command needs it, and only when it is about
    to cancel something.
    """
    try:
        import modal
    except ImportError as error:  # pragma: no cover - environment-dependent
        raise ModalUnavailable(
            "Stopping a Modal execution needs the modal package: "
            f"{error}. Install stardag[modal], or stop the calls from the "
            "Modal dashboard and then run 'stardag builds cancel'."
        ) from error

    outcomes: list[CancelOutcome] = []
    for execution in executions:
        try:
            modal.FunctionCall.from_id(execution.executor_ref).cancel()
        except Exception as error:  # noqa: BLE001 - reported, never fatal
            outcomes.append(
                CancelOutcome(execution, error=f"{type(error).__name__}: {error}")
            )
        else:
            outcomes.append(CancelOutcome(execution))
    return outcomes


def modal_workspaces(executions: Iterable[Execution]) -> set[str]:
    """Modal workspaces the selected executions were started in.

    ``FunctionCall.from_id`` resolves against whatever credentials the
    ambient Modal profile provides, so a call started in a workspace the
    operator is not currently authenticated to simply will not be found —
    a per-call failure that reads as "already gone". Surfacing the
    workspaces up front turns that into something the operator can check
    before it matters.
    """
    workspaces = set()
    for execution in executions:
        workspace = execution.executor_metadata.get("workspace")
        if isinstance(workspace, str) and workspace:
            workspaces.add(workspace)
    return workspaces
