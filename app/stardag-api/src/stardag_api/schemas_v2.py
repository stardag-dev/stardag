"""Wire schemas of the ``/api/v2`` registry routes.

The registration item is shared by the services and the routes: it is the
one shape every registration route carries (design.md, "Registration").
Responses mirror the services' result types (``from_attributes``), so a
route converts and does nothing else.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from stardag_api.models.enums import (
    BuildStatus,
    ClaimOutcome,
    DeploymentKind,
    ExecutionOutcome,
    TaskStatus,
)


class RegistrationItem(BaseModel):
    """One task instance, as the static phase (or a yield) states it.

    ``declared_upstreams`` names upstream **instances** by ``instance_hash``
    (a scope may hold several instances of one completion); ``None`` means
    "not expanded" — the driver did not evaluate ``requires()``, typically
    because the target already existed. ``observed_complete`` and
    ``observed_at`` are what the driver saw when it checked the target, on
    its own clock.
    """

    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1, max_length=64)
    task_namespace: str = Field(default="", max_length=255)
    task_name: str = Field(min_length=1, max_length=255)
    version: str | None = Field(default=None, max_length=64)
    output_uri: str | None = Field(default=None, max_length=2048)
    instance_hash: str = Field(min_length=1, max_length=64)
    body: dict[str, Any]
    declared_upstreams: list[str] | None = None
    observed_complete: bool = False
    observed_at: datetime


# ---------------------------------------------------------------------------
# Deployments and settings
# ---------------------------------------------------------------------------


class DeploymentCreate(BaseModel):
    """``POST /deployments``. A Modal deployment names its client-minted
    ``id`` and its ``app_name``; a local one is looked up by ``code_id``
    (``id`` optional, ``app_name`` defaults to ``"local"``)."""

    model_config = ConfigDict(extra="forbid")

    id: UUID | None = None
    kind: DeploymentKind
    app_name: str | None = Field(default=None, min_length=1, max_length=64)
    code_id: str = Field(min_length=1, max_length=64)
    image_id: str | None = Field(default=None, max_length=128)
    modal_app_id: str | None = Field(default=None, max_length=64)


class DeploymentActivate(BaseModel):
    """``POST /deployments/{id}/activate``: what only the finished deploy
    knows. Optional; a given value fills a NULL or must match."""

    model_config = ConfigDict(extra="forbid")

    modal_app_id: str | None = Field(default=None, max_length=64)
    image_id: str | None = Field(default=None, max_length=128)


class DeploymentInfo(BaseModel):
    """A deployment as read (listings, a plan's deployment)."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    kind: DeploymentKind
    app_name: str
    code_id: str
    image_id: str | None
    modal_app_id: str | None
    generation: int
    deployed_at: datetime
    activated_at: datetime | None
    is_current: bool


class DeploymentResponse(DeploymentInfo):
    """``POST /deployments`` and ``/activate``: the deployment, and whether
    this call created it (a lookup-or-create answers either way)."""

    created: bool


class DeploymentListResponse(BaseModel):
    deployments: list[DeploymentInfo]


class SettingsResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    hash: str
    body: dict[str, str]


# ---------------------------------------------------------------------------
# Builds
# ---------------------------------------------------------------------------


class BuildCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Client-minted for an idempotent create; the server mints one if absent.
    id: UUID | None = None
    name: str | None = Field(default=None, max_length=64)
    description: str | None = None
    #: The request at completion-id level; every plan's roots must match.
    root_task_ids: list[Annotated[str, Field(min_length=1, max_length=64)]] = Field(
        min_length=1
    )
    executor_metadata: dict[str, Any] | None = None


class BuildResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    description: str | None
    status: BuildStatus
    root_task_ids: list[str]
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    last_active_at: datetime
    is_resumed: bool
    status_triggered_by_user_id: str | None
    executor_metadata: dict[str, Any] | None
    reactive_app_name: str | None
    reactive_tick_kwargs: dict[str, Any] | None
    #: Why the build is FAILED (its last ``BUILD_FAILED``'s message); None
    #: for any other status.
    error_message: str | None


class BuildCompleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Override outstanding members — never a missing seal or an excluded
    #: root.
    force: bool = False


class BuildFailRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    error_message: str | None = None


class BuildResumeRequest(BaseModel):
    """The caller's scope, to reuse or reactivate its plan; omitted, the
    resume only makes the build RUNNING again."""

    model_config = ConfigDict(extra="forbid")

    deployment_id: UUID | None = None
    settings: dict[str, str] = Field(default_factory=dict)
    executor_metadata: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Plans and registration
# ---------------------------------------------------------------------------


class PlanCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: UUID
    deployment_id: UUID
    settings: dict[str, str] = Field(default_factory=dict)
    roots: list[RegistrationItem] = Field(min_length=1)


class PlanResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    build_id: UUID
    deployment_id: UUID
    settings_hash: str
    generation: int
    activated_at: datetime | None
    sealed_at: datetime | None
    superseded_at: datetime | None
    created: bool


# ---------------------------------------------------------------------------
# Wake-ups, the scheduler lease, reactive meta, tick summaries
# ---------------------------------------------------------------------------


class NotifyResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    build_id: UUID
    needs_tick: bool
    #: POST only: a scheduler held the lease once the flag was durable.
    scheduler_live: bool | None = None


class WakeCandidateResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    build_id: UUID
    reactive_app_name: str


class WakeCandidatesResponse(BaseModel):
    builds: list[WakeCandidateResponse]


class LeaseResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    build_id: UUID
    held: bool
    expires_at: datetime | None = None


class ReactiveMetaRequest(BaseModel):
    """``PUT /builds/{id}/reactive-meta``; ``tick_kwargs`` omitted keeps the
    stored configuration."""

    model_config = ConfigDict(extra="forbid")

    app_name: str = Field(min_length=1, max_length=64)
    tick_kwargs: dict[str, Any] | None = None


class TickSummaryCreate(BaseModel):
    """One tick's summary, stored verbatim: SDK-owned and growing, so
    unknown keys are kept, not rejected."""

    model_config = ConfigDict(extra="allow")

    outcome: str = Field(min_length=1, max_length=32)


class TickSummaryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    build_id: UUID
    outcome: str
    summary: dict[str, Any]
    created_at: datetime


class TickSummaryListResponse(BaseModel):
    build_id: UUID
    summaries: list[TickSummaryResponse]


class ResumeResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    build: BuildResponse
    #: The plan for the caller's scope, if one exists; None means discovery
    #: runs and the caller creates it (``POST /builds/{id}/plans``).
    plan: PlanResponse | None
    changed: bool


class MembersRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[RegistrationItem]


class MembersResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    tasks_created: int
    instances_created: int
    members_admitted: int
    closure_admitted: int
    edges_created: int
    completed: int
    invalidated: int
    diverged: int


# ---------------------------------------------------------------------------
# Frontier
# ---------------------------------------------------------------------------


class FrontierMemberResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    task_id: str
    instance_id: UUID
    instance_hash: str
    status: TaskStatus
    is_root: bool
    body: dict[str, Any]


class FrontierItemResponse(FrontierMemberResponse):
    """A runnable or running member, with the counts the tick applies its
    retry and interruption budgets to: executions of the task under any of
    the build's plans, and those that ended interrupted or preempted
    (counted from the ledger, D9)."""

    attempts: int
    interruptions: int


class ClosureConflictResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    task_id: str
    member_instance_id: UUID
    other_instance_id: UUID
    fields: list[str]


class ClosureResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    admitted: int
    conflicts: list[ClosureConflictResponse]
    build_failed: bool


class FrontierResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    build_id: UUID
    plan_id: UUID | None
    deployment_id: UUID | None
    settings_hash: str | None
    sealed: bool
    plan_complete: bool
    build_status: BuildStatus | None
    #: Read by every tick, so wake-ups spawned with only the build id share
    #: the trigger-time configuration.
    reactive_app_name: str | None
    reactive_tick_kwargs: dict[str, Any] | None
    runnable: list[FrontierItemResponse]
    discovery_jobs: list[FrontierMemberResponse]
    running: list[FrontierItemResponse]
    closure: ClosureResponse | None


# ---------------------------------------------------------------------------
# Task transitions
# ---------------------------------------------------------------------------


class StartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    execution_id: UUID
    #: A claiming start (the tick, or an in-process engine) takes the claim;
    #: a non-claiming one is the holder's own "I am running" report.
    claim: bool = True
    claim_ttl_seconds: int | None = Field(default=None, gt=0)
    executor: str | None = Field(default=None, max_length=32)
    executor_ref: str | None = Field(default=None, max_length=255)
    executor_metadata: dict[str, Any] | None = None
    #: A claiming start's concurrency-limit keys, computed by the tick from
    #: the instance body it runs (limit-key selection may read
    #: non-significant fields, so they are per instance). Written to
    #: ``task_limit_key`` at claim and replaced on every claim.
    limit_keys: list[Annotated[str, Field(min_length=1, max_length=255)]] = Field(
        default_factory=list, max_length=64
    )


class ReportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    execution_id: UUID


class FailRequest(ReportRequest):
    error_message: str | None = None


class RenewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    execution_id: UUID
    claim_ttl_seconds: int | None = Field(default=None, gt=0)


class TransitionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    applied: bool
    status: TaskStatus
    execution_id: UUID | None
    claim_expires_at: datetime | None


# ---------------------------------------------------------------------------
# The dynamic phase
# ---------------------------------------------------------------------------


class YieldRequest(BaseModel):
    """``POST /plans/{plan_id}/members/{task_id}/yield``: one yield batch.

    ``items`` are the yielded children and their static closure, in
    post-order, exactly as a static chunk; ``yielded`` names the children
    (by instance hash, each one of ``items``) the parent gets a dynamic
    edge to. ``batch_id`` is client-minted: a retried batch is replayed.
    """

    model_config = ConfigDict(extra="forbid")

    execution_id: UUID
    deployment_id: UUID
    batch_id: UUID
    items: list[RegistrationItem] = Field(min_length=1, max_length=1000)
    yielded: list[Annotated[str, Field(min_length=1, max_length=64)]] = Field(
        min_length=1
    )
    #: The reactive worker suspends (its container exits); the resident
    #: engine keeps the claim while its generator waits.
    suspend: bool


class YieldResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    members: MembersResponse
    dynamic_edges_created: int
    #: The parent's state once the batch applied (or when it first applied,
    #: for a replay).
    status: TaskStatus
    execution_id: UUID | None
    claim_expires_at: datetime | None
    #: True when this delivery found the batch already applied.
    replayed: bool


# ---------------------------------------------------------------------------
# Skip-blocked and exclusion
# ---------------------------------------------------------------------------


class SkipBlockedResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    plan_id: UUID | None
    skipped: list[str]


class ExcludeRequest(BaseModel):
    """An operator gives up on a member (STA-104)."""

    model_config = ConfigDict(extra="forbid")

    #: Why, in the operator's words; recorded on the ``TASK_EXCLUDED`` event.
    reason: str | None = Field(default=None, max_length=2000)


class DiscoveryFailedRequest(BaseModel):
    """A discovery job failed: the class could not be imported, or
    ``requires()`` raised."""

    model_config = ConfigDict(extra="forbid")

    error: str = Field(min_length=1)


class ExclusionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    plan_id: UUID
    #: Task ids this call excluded (the member, then its cascade).
    excluded: list[str]
    #: The plan's roots among ``excluded``: this call cascaded to them.
    roots_excluded: list[str]
    #: This call failed the build (an excluded root, build not terminal).
    build_failed: bool


# ---------------------------------------------------------------------------
# Executions: builds stop and orphans
# ---------------------------------------------------------------------------


class ExecutionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

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
    #: False for an orphan: its plan is not the build's active plan.
    in_current_plan: bool


class ExecutionListResponse(BaseModel):
    build_id: UUID
    executions: list[ExecutionResponse]


class TaskExecutionListResponse(BaseModel):
    """``GET /tasks/{task_id}/executions``: across builds, newest first."""

    task_id: str
    executions: list[ExecutionResponse]


class StoppedRequest(BaseModel):
    """An operator end: ``stopped`` (the CLI stopped the execution) or
    ``lost`` (it cannot be stopped, and the operator gives up on it)."""

    model_config = ConfigDict(extra="forbid")

    outcome: Literal["stopped", "lost"] = "stopped"


# ---------------------------------------------------------------------------
# Concurrency limits
# ---------------------------------------------------------------------------


class ConcurrencyLimitSet(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_concurrent: int = Field(ge=0)


class ConcurrencyLimitResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    key: str
    max_concurrent: int


class ConcurrencyLimitHolder(BaseModel):
    """A task currently occupying a slot of a limit key (a live claim)."""

    model_config = ConfigDict(from_attributes=True)

    task_id: str
    task_name: str
    build_id: UUID
    plan_id: UUID
    execution_id: UUID | None
    started_at: datetime | None


class ConcurrencyLimitInfo(ConcurrencyLimitResponse):
    """A limit as ``GET /concurrency-limits`` lists it: the cap, how many
    slots are occupied, and — with ``include_holders=true`` — by what."""

    in_use: int
    holders: list[ConcurrencyLimitHolder] | None = None


class ConcurrencyLimitListResponse(BaseModel):
    limits: list[ConcurrencyLimitInfo]
