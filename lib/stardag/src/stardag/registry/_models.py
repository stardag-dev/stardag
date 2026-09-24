"""Wire models of the v2 registry (``/api/v2``).

Two directions:

- :class:`RegistrationItem` is what the SDK *sends* — one task instance as a
  driver states it (design.md, "Registration"). Every registration route
  (``POST /builds/{id}/plans``, ``POST /plans/{id}/members`` and
  ``/yield``) carries this one shape.
- Everything else is what the registry *answers*, parsed leniently (unknown
  fields ignored), so a server that grows a response field does not break
  an SDK.

Vocabulary (design.md, D2): an **instance** is a registry row — a
construction of a task under a scope ``(deployment_id, settings_hash)``;
the Python object is a **task object**. ``instance_hash`` is never an
identifier on its own: routes address a member by its plan and ``task_id``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class _Response(BaseModel):
    """A registry response: unknown fields are ignored, never an error."""

    model_config = ConfigDict(extra="ignore")


# -----------------------------------------------------------------------------
# Registration (sent)
# -----------------------------------------------------------------------------


class RegistrationItem(BaseModel):
    """One task instance, as the static phase or a yield states it.

    ``declared_upstreams`` names upstream **instances** by ``instance_hash``
    (a scope may hold several instances of one completion); ``None`` means
    "not expanded" — the driver did not evaluate ``requires()``, because the
    target already existed. ``observed_complete`` / ``observed_at`` are what
    the driver saw when it checked the target, on its own clock: the
    registry marks the task COMPLETED on a positive observation and
    withdraws a completion on a negative one (the only path out of
    COMPLETED, design.md "Invalidation").
    """

    model_config = ConfigDict(extra="forbid")

    task_id: str
    task_namespace: str = ""
    task_name: str
    version: str | None = None
    output_uri: str | None = None
    instance_hash: str
    body: dict[str, Any]
    declared_upstreams: list[str] | None = None
    observed_complete: bool = False
    observed_at: datetime

    def wire(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


# -----------------------------------------------------------------------------
# Builds
# -----------------------------------------------------------------------------


BuildStatus = Literal["pending", "running", "completed", "failed", "cancelled"]


class BuildInfo(_Response):
    """A build (``GET /builds/{id}`` and every lifecycle transition).

    ``root_task_ids`` is the request at completion-id level, stable across
    rollover. ``reactive_app_name`` is the reactive marker: None means the
    build is not reactively scheduled, and a tick no-ops on it.
    """

    id: UUID
    name: str | None = None
    description: str | None = None
    status: str | None = None
    root_task_ids: list[str] = Field(default_factory=list)
    created_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    last_active_at: datetime | None = None
    is_resumed: bool = False
    executor_metadata: dict[str, Any] | None = None
    reactive_app_name: str | None = None
    reactive_tick_kwargs: dict[str, Any] | None = None
    #: Why the build is FAILED (its last ``BUILD_FAILED``'s message); None
    #: for any other status.
    error_message: str | None = None


class BuildListPage(_Response):
    """``GET /builds``: one page, most recently active first. ``total``
    counts every build the filters match; ``next_cursor`` is passed back as
    ``cursor`` for the next page (None on the last one)."""

    builds: list[BuildInfo] = Field(default_factory=list)
    total: int = 0
    next_cursor: str | None = None


class PlanInfo(_Response):
    """A plan: one build's request under one scope ``(deployment, settings)``.

    ``created`` is True only for the call that inserted the row. The first
    plan of a build is active from creation; a replacement activates at
    ``/seal`` and supersedes the previous one.
    """

    id: UUID
    build_id: UUID
    deployment_id: UUID
    settings_hash: str
    generation: int = 0
    activated_at: datetime | None = None
    sealed_at: datetime | None = None
    superseded_at: datetime | None = None
    created: bool = False

    @property
    def active(self) -> bool:
        return self.activated_at is not None and self.superseded_at is None


class PlanDetail(PlanInfo):
    """``GET /plans/{id}`` (and each item of ``GET /builds/{id}/plans``): a
    plan's lifecycle, its scope with the deployment resolved, and member
    counts. ``member_counts`` holds the non-excluded members by their
    task's global status; ``excluded_count`` counts the given-up ones
    apart. Served for a superseded plan too."""

    deployment: DeploymentInfo | None = None
    created_at: datetime | None = None
    is_active: bool = False
    member_count: int = 0
    root_count: int = 0
    excluded_count: int = 0
    member_counts: dict[str, int] = Field(default_factory=dict)


class ResumeResult(_Response):
    """``POST /builds/{id}/resume``: the build, and the plan for the caller's
    scope if one exists (None means discovery runs and the caller creates
    it)."""

    build: BuildInfo
    plan: PlanInfo | None = None
    changed: bool = False


class MembersResult(_Response):
    """What one registration chunk changed. All zero on a re-delivery."""

    tasks_created: int = 0
    instances_created: int = 0
    members_admitted: int = 0
    closure_admitted: int = 0
    edges_created: int = 0
    completed: int = 0
    invalidated: int = 0
    diverged: int = 0


# -----------------------------------------------------------------------------
# Frontier
# -----------------------------------------------------------------------------


class FrontierMember(_Response):
    """A member of the active plan as the frontier lists it, with the
    instance body a tick rehydrates the task object from.

    ``attempts`` and ``interruptions`` are served on ``runnable`` and
    ``running`` items only (zero elsewhere), counted from the execution
    ledger over **all** of the build's plans (design.md D9): ``attempts``
    is every execution of the task under the build, ``interruptions`` those
    whose claim was released ``interrupted`` or which ended ``interrupted``
    or ``preempted``. The tick applies ``TickConfig.max_interruptions`` to
    ``interruptions``.
    """

    task_id: str
    instance_id: UUID | None = None
    instance_hash: str
    status: str
    is_root: bool = False
    body: dict[str, Any] = Field(default_factory=dict)
    attempts: int = 0
    interruptions: int = 0


class PlanRoots(_Response):
    """``GET /plans/{id}/roots``: a plan's scope and its root members, with
    the instance bodies a rollover rehydrates."""

    plan_id: UUID
    build_id: UUID
    deployment_id: UUID
    settings_hash: str
    roots: list[FrontierMember] = Field(default_factory=list)


class ClosureConflict(_Response):
    task_id: str
    fields: list[str] = Field(default_factory=list)


class ClosureOutcome(_Response):
    admitted: int = 0
    conflicts: list[ClosureConflict] = Field(default_factory=list)
    build_failed: bool = False


class BuildFrontier(_Response):
    """The scheduling state of a build's active plan (design.md, "The
    runnable rule"), after the closure step.

    - ``runnable``: members whose instance is expanded, whose status is
      actionable (a lapsed claim included) and whose upstreams are all
      COMPLETED.
    - ``discovery_jobs``: non-COMPLETED members whose instance was never
      expanded — a tick rehydrates, evaluates ``requires()`` and registers.
    - ``running``: members under a live claim, whoever holds it.

    ``plan_id`` is None when the build has no active plan yet (the static
    phase has not started). ``plan_complete`` is a diagnostic: ``/complete``
    recomputes it.
    """

    build_id: UUID
    plan_id: UUID | None = None
    deployment_id: UUID | None = None
    settings_hash: str | None = None
    sealed: bool = False
    plan_complete: bool = False
    build_status: str | None = None
    reactive_app_name: str | None = None
    reactive_tick_kwargs: dict[str, Any] | None = None
    runnable: list[FrontierMember] = Field(default_factory=list)
    discovery_jobs: list[FrontierMember] = Field(default_factory=list)
    running: list[FrontierMember] = Field(default_factory=list)
    closure: ClosureOutcome | None = None


# -----------------------------------------------------------------------------
# Transitions
# -----------------------------------------------------------------------------


class TransitionResult(_Response):
    """The task's state after a transition. ``applied`` is False for a no-op
    (a retried start that already holds the claim, a retry of a PENDING
    task)."""

    applied: bool = True
    status: str
    execution_id: UUID | None = None
    claim_expires_at: datetime | None = None


class YieldResult(_Response):
    """``/yield``: what the batch registered, and the parent's state after
    it (or when it first applied, for a replay)."""

    members: MembersResult = Field(default_factory=MembersResult)
    dynamic_edges_created: int = 0
    status: str
    execution_id: UUID | None = None
    claim_expires_at: datetime | None = None
    replayed: bool = False


class ExclusionResult(_Response):
    """What one exclusion call did: the task ids it took out of the plan
    (the member and its downstream closure; empty on a re-delivery), the
    plan's roots among them, and whether this call failed the build (it
    excluded a root of a build not already terminal)."""

    plan_id: UUID
    excluded: list[str] = Field(default_factory=list)
    roots_excluded: list[str] = Field(default_factory=list)
    build_failed: bool = False


class ExecutionInfo(_Response):
    """One execution ledger row (the two ends, written by two hands)."""

    id: UUID
    task_id: str | None = None
    build_id: UUID | None = None
    plan_id: UUID | None = None
    instance_id: UUID | None = None
    executor: str | None = None
    executor_ref: str | None = None
    executor_metadata: dict[str, Any] | None = None
    in_current_plan: bool = True
    started_at: datetime | None = None
    claim_released_at: datetime | None = None
    claim_outcome: str | None = None
    ended_at: datetime | None = None
    outcome: str | None = None

    @property
    def still_wanted(self) -> bool:
        """Whether this execution may still produce the task's result: its
        claim has not been released (taken over, closed or released by a
        build) and no end has been reported."""
        return self.claim_released_at is None and self.ended_at is None


class TaskInstanceInfo(_Response):
    """One instance of a completion: its body under one scope
    (``deployment_id`` and ``settings_hash``) — a task id may hold several."""

    id: UUID
    deployment_id: UUID
    settings_hash: str
    instance_hash: str
    body: dict[str, Any]
    expanded_at: datetime | None = None
    created_at: datetime | None = None


class TaskInfo(_Response):
    """A completion (``task`` row): identity and global state only; it holds
    no parameters. ``instances`` holds each instance the read found, newest
    first."""

    task_id: str
    task_namespace: str = ""
    task_name: str = ""
    version: str | None = None
    output_uri: str | None = None
    status: str | None = None
    status_at: datetime | None = None
    #: The latest start (the current execution's).
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error_message: str | None = None
    claim_expires_at: datetime | None = None
    #: The claim's holder while RUNNING (live or lapsed): the plan it was
    #: granted through, and that plan's build. None otherwise.
    claim_plan_id: UUID | None = None
    claim_build_id: UUID | None = None
    #: The execution the task's claim names (live or lapsed), if any.
    execution_id: UUID | None = None
    #: Filled by ``task_get`` only; ``task_list`` items carry none.
    instances: list["TaskInstanceInfo"] = Field(default_factory=list)

    @property
    def body(self) -> dict[str, Any] | None:
        """The newest instance's body, or ``None`` without one. A task has
        no parameters — an instance does — so a caller not asking for a
        specific scope (``from_registry``) takes the newest as its default."""
        return self.instances[0].body if self.instances else None


class TaskListPage(_Response):
    """``GET /tasks``: one page of completions, most recent status change
    first, without instances."""

    tasks: list[TaskInfo] = Field(default_factory=list)
    next_cursor: str | None = None


class EventInfo(_Response):
    """One row of the append-only event log (``GET /tasks/{id}/events``).
    ``report_applied`` is False for a report that was recorded but refused:
    history, not state."""

    id: UUID | None = None
    event_type: str
    created_at: datetime | None = None
    build_id: UUID | None = None
    plan_id: UUID | None = None
    execution_id: UUID | None = None
    task_id: str | None = None
    report_applied: bool = True
    error_message: str | None = None
    event_metadata: dict[str, Any] | None = None


class TaskArtifactInfo(_Response):
    """One artifact of a task (``GET /tasks/{task_id}/artifacts``);
    artifacts belong to the completion, not to a plan or execution."""

    id: UUID | None = None
    task_id: str
    artifact_type: str
    name: str
    body: Any = None
    created_at: datetime | None = None


# -----------------------------------------------------------------------------
# Deployments and settings
# -----------------------------------------------------------------------------


DeploymentKind = Literal["modal", "local"]

#: What an operator reports on ``POST /executions/{id}/stopped``: ``stopped``
#: (the CLI cancelled the call) or ``lost`` (it could not, and gives up on
#: it: no report of that execution will ever be applied).
StopOutcome = Literal["stopped", "lost"]


class DeploymentInfo(_Response):
    """One deployment: a ``stardag modal deploy`` (``kind="modal"``,
    activated after the deploy succeeded) or a local code id
    (``kind="local"``, born activated). "Current" for an app is the
    activated row with the highest generation. ``created`` is set only by
    the create call that inserted the row; a listing does not carry it, so
    it is left out of dumps (a listing row never shows ``created: false``)."""

    id: UUID
    kind: str
    app_name: str
    code_id: str
    image_id: str | None = None
    modal_app_id: str | None = None
    generation: int = 0
    deployed_at: datetime | None = None
    activated_at: datetime | None = None
    is_current: bool = False
    created: bool = Field(default=False, exclude=True)


class SettingsInfo(_Response):
    hash: str
    body: dict[str, str] = Field(default_factory=dict)


# -----------------------------------------------------------------------------
# Concurrency limits
# -----------------------------------------------------------------------------


class ConcurrencyLimitHolderInfo(_Response):
    """A task currently occupying a slot of a limit key (a live claim)."""

    task_id: str
    task_name: str
    build_id: UUID
    plan_id: UUID
    execution_id: UUID | None = None
    started_at: datetime | None = None


class ConcurrencyLimitInfo(_Response):
    """One named limit: the cap, how many slots are occupied, and — with
    ``include_holders``, from ``concurrency_limit_list`` — by what."""

    key: str
    max_concurrent: int
    in_use: int = 0
    holders: list[ConcurrencyLimitHolderInfo] | None = None


# -----------------------------------------------------------------------------
# Reactive scheduling
# -----------------------------------------------------------------------------


class BuildNotifyResult(_Response):
    """Outcome of ``build_notify``: whether the build wants a tick, and
    whether a scheduler held the lease once the flag was durable (POST
    only; None on a read). ``scheduler_live=True`` is what lets a worker
    skip spawning a tick: the holder re-reads the flag after releasing the
    lease (the exit handshake)."""

    build_id: UUID | None = None
    needs_tick: bool = True
    scheduler_live: bool | None = None


class WakeCandidate(_Response):
    """A flagged build nobody is serving, handed out once per window."""

    build_id: UUID
    reactive_app_name: str


class SchedulerLeaseResult(_Response):
    held: bool
    expires_at: datetime | None = None


class TickSummaryRecord(_Response):
    """A reported tick summary (``GET /builds/{id}/tick-summaries``)."""

    id: UUID | None = None
    outcome: str
    summary: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None


__all__ = [
    "BuildFrontier",
    "BuildInfo",
    "BuildListPage",
    "BuildNotifyResult",
    "ClosureConflict",
    "ClosureOutcome",
    "DeploymentInfo",
    "DeploymentKind",
    "EventInfo",
    "ExecutionInfo",
    "FrontierMember",
    "MembersResult",
    "PlanDetail",
    "PlanInfo",
    "PlanRoots",
    "RegistrationItem",
    "ResumeResult",
    "SchedulerLeaseResult",
    "SettingsInfo",
    "StopOutcome",
    "TaskArtifactInfo",
    "TaskInfo",
    "TaskInstanceInfo",
    "TaskListPage",
    "TickSummaryRecord",
    "TransitionResult",
    "WakeCandidate",
    "YieldResult",
]


PlanDetail.model_rebuild()
