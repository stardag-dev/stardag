"""Wire schemas of the ``/api/v2`` read routes: builds, plans, tasks,
artifacts and the event log.

Split from ``schemas_v2.py`` by concern (module-size rule). Responses mirror
the read services' result types (``from_attributes``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from stardag_api.models.enums import (
    AdmittedBy,
    EventType,
    ExclusionReason,
    TaskStatus,
)
from stardag_api.services.annotations import (
    MAX_DESCRIPTION_LENGTH,
    MAX_TAGS,
    normalise_description,
    normalise_tags,
)
from stardag_api.schemas_v2 import (
    BuildResponse,
    DeploymentInfo,
    FrontierMemberResponse,
)

# ---------------------------------------------------------------------------
# Builds and plans
# ---------------------------------------------------------------------------


class BuildListResponse(BaseModel):
    """Most recently active first, a page at a time."""

    builds: list[BuildResponse]
    #: Builds matching the filters, over every page.
    total: int
    #: Pass back as ``cursor`` for the next page; None on the last one.
    next_cursor: str | None


class PlanRootsResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    plan_id: UUID
    build_id: UUID
    deployment_id: UUID
    settings_hash: UUID
    roots: list[FrontierMemberResponse]


class PlanDetailResponse(BaseModel):
    """``GET /plans/{id}``: a plan's lifecycle, scope and member counts."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    build_id: UUID
    deployment_id: UUID
    deployment: DeploymentInfo
    settings_hash: UUID
    generation: int
    created_at: datetime
    activated_at: datetime | None
    sealed_at: datetime | None
    superseded_at: datetime | None
    is_active: bool
    member_count: int
    root_count: int
    #: Given-up members; counted apart from ``member_counts``.
    excluded_count: int
    #: Non-excluded members by their task's global status.
    member_counts: dict[TaskStatus, int]


class PlanListResponse(BaseModel):
    """``GET /builds/{id}/plans``: newest generation first."""

    build_id: UUID
    plans: list[PlanDetailResponse]


class PlanGraphMemberResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    task_id: str
    instance_id: UUID
    instance_hash: str
    task_namespace: str
    task_name: str
    status: TaskStatus
    is_root: bool
    admitted_by: AdmittedBy
    excluded_at: datetime | None
    excluded_reason: ExclusionReason | None
    attempts: int
    interruptions: int


class PlanGraphEdgeResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    upstream_instance_id: UUID
    downstream_instance_id: UUID
    is_dynamic: bool


class PlanGraphResponse(BaseModel):
    """``GET /plans/{id}/graph``: every member and the instance edges
    between member instances."""

    model_config = ConfigDict(from_attributes=True)

    plan_id: UUID
    build_id: UUID
    deployment_id: UUID
    settings_hash: UUID
    members: list[PlanGraphMemberResponse]
    edges: list[PlanGraphEdgeResponse]


# ---------------------------------------------------------------------------
# Tasks, artifacts and the event log
# ---------------------------------------------------------------------------


class TaskInstanceResponse(BaseModel):
    """One instance of a completion: its body under one scope."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    deployment_id: UUID
    settings_hash: UUID
    instance_hash: str
    body: dict[str, Any]
    expanded_at: datetime | None
    created_at: datetime


class TaskSummaryResponse(BaseModel):
    """A completion (``task``: identity and global state, no parameters)."""

    model_config = ConfigDict(from_attributes=True)

    task_id: str
    task_namespace: str
    task_name: str
    version: str | None
    output_uri: str | None
    status: TaskStatus
    status_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    error_message: str | None
    claim_expires_at: datetime | None
    #: The claim's holder while RUNNING (live or lapsed): the plan it was
    #: granted through, and that plan's build.
    claim_plan_id: UUID | None
    claim_build_id: UUID | None
    #: The current execution (the claim's, while RUNNING).
    execution_id: UUID | None
    #: Annotations: normalised tags (sorted) and a description. Mutable,
    #: set by ``PATCH /tasks/{task_id}``, outside every hash.
    tags: list[str] = Field(default_factory=list)
    description: str | None = None


class TaskResponse(TaskSummaryResponse):
    """A completion with its instances in the caller's environment, newest
    first."""

    instances: list[TaskInstanceResponse]


class InstanceResponse(BaseModel):
    """``GET /instances/{instance_id}``: one instance and the completion it
    realises. The body is immutable."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    task_id: str
    deployment_id: UUID
    settings_hash: UUID
    instance_hash: str
    body: dict[str, Any]
    created_at: datetime
    expanded_at: datetime | None


class TaskAnnotateRequest(BaseModel):
    """``PATCH /tasks/{task_id}``: the annotations to change. An omitted
    field is left as it is; ``tags`` replaces the whole set (``[]`` clears
    it, ``null`` is refused) — last writer wins: two clients adding a tag
    each by read-modify-write can lose one of the additions, there is no
    server-side add/remove; ``description: null`` or ``""`` clears it.
    Tags are normalised — trimmed, deduplicated, sorted — and refused (422)
    if empty, longer than 64 characters, carrying a control character, or
    more than 32 distinct."""

    model_config = ConfigDict(extra="forbid")

    # Bounded before normalisation too, so an oversized body is refused
    # without being walked; duplicates may make up the difference.
    tags: list[str] | None = Field(default=None, max_length=4 * MAX_TAGS)
    description: str | None = Field(default=None, max_length=4 * MAX_DESCRIPTION_LENGTH)

    @field_validator("tags", mode="before")
    @classmethod
    def _tags_not_null(cls, value: Any) -> Any:
        # Runs only for a value that was sent (defaults are not validated).
        if value is None:
            raise ValueError("tags may be omitted or a list, not null")
        return value

    @field_validator("tags")
    @classmethod
    def _normalise_tags(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else normalise_tags(value)

    @field_validator("description")
    @classmethod
    def _normalise_description(cls, value: str | None) -> str | None:
        return normalise_description(value)

    def changes(self) -> dict[str, Any]:
        """Only the fields the request named, as columns to set."""
        return {
            f: getattr(self, f)
            for f in ("tags", "description")
            if f in self.model_fields_set
        }


class TaskListResponse(BaseModel):
    """``GET /tasks``: most recent status change first, a page at a time."""

    tasks: list[TaskSummaryResponse]
    next_cursor: str | None


class ArtifactItem(BaseModel):
    """One artifact, as the SDK dumps it. Body format (v1's): markdown as
    ``{"content": "<markdown>"}``, json as the data dict."""

    model_config = ConfigDict(extra="forbid")

    type: str = Field(min_length=1, max_length=50)
    name: str = Field(min_length=1, max_length=255)
    body: dict[str, Any]


class ArtifactUploadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: The execution that produced them, when known. Informational:
    #: artifacts belong to the promise, whichever execution wrote them.
    execution_id: UUID | None = None
    artifacts: list[ArtifactItem] = Field(max_length=100)


class TaskArtifactResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    task_id: str
    artifact_type: str
    name: str
    body: Any
    created_at: datetime


class TaskArtifactListResponse(BaseModel):
    artifacts: list[TaskArtifactResponse]


class EventResponse(BaseModel):
    """One row of the append-only log. ``report_applied`` is False for a
    report that was recorded but refused: history, not state."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    event_type: EventType
    created_at: datetime
    build_id: UUID | None
    plan_id: UUID | None
    execution_id: UUID | None
    task_id: str | None
    report_applied: bool
    error_message: str | None
    event_metadata: dict[str, Any] | None


class EventListResponse(BaseModel):
    """Oldest first."""

    events: list[EventResponse]
