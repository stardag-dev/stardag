"""Wire schemas of the ``/api/v2`` registry routes.

The registration item is shared by the services and the routes: it is the
one shape every registration route carries (design.md, "Registration").
Responses mirror the services' result types (``from_attributes``), so a
route converts and does nothing else.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from stardag_api.models.enums import BuildStatus, TaskStatus


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
# Builds (minimal: lifecycle routes arrive in step 3)
# ---------------------------------------------------------------------------


class BuildCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Client-minted for an idempotent create; the server mints one if absent.
    id: UUID | None = None
    name: str | None = Field(default=None, max_length=64)
    description: str | None = None
    #: The request at completion-id level.
    root_task_ids: list[str] = Field(default_factory=list)


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
    runnable: list[FrontierMemberResponse]
    discovery_jobs: list[FrontierMemberResponse]
    running: list[FrontierMemberResponse]
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
