"""Pydantic schemas for API request/response models."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from stardag_api.models.enums import BuildStatus, EventType, TaskStatus

# --- Workspace Schemas ---


class WorkspaceCreate(BaseModel):
    """Schema for creating a workspace."""

    name: str
    slug: str
    description: str | None = None


class WorkspaceResponse(BaseModel):
    """Schema for workspace response."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    slug: str
    description: str | None
    created_at: datetime


# --- User Schemas ---


class UserCreate(BaseModel):
    """Schema for creating a user."""

    username: str
    display_name: str | None = None
    email: str | None = None


class UserResponse(BaseModel):
    """Schema for user response."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    workspace_id: UUID
    username: str
    display_name: str | None
    email: str | None
    created_at: datetime


# --- Environment Schemas ---


class EnvironmentCreate(BaseModel):
    """Schema for creating an environment."""

    name: str
    slug: str
    description: str | None = None


class EnvironmentResponse(BaseModel):
    """Schema for environment response."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    workspace_id: UUID
    name: str
    slug: str
    description: str | None
    created_at: datetime


# --- Build Schemas ---


class BuildCreate(BaseModel):
    """Schema for creating a build."""

    environment_id: str = "default"
    user_id: str | None = None  # Optional until auth is implemented
    commit_hash: str | None = None
    root_task_ids: list[str] = []
    description: str | None = None


class StatusTriggeredByUser(BaseModel):
    """User info for who triggered a manual status change."""

    id: str  # external_id from auth provider (stored in event metadata)
    email: str
    display_name: str | None


class BuildResponse(BaseModel):
    """Schema for build response with derived status."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    environment_id: UUID
    user_id: UUID | None
    name: str
    description: str | None
    commit_hash: str | None
    root_task_ids: list[str]
    created_at: datetime
    # Derived fields (not from model directly)
    status: BuildStatus = BuildStatus.PENDING
    started_at: datetime | None = None
    completed_at: datetime | None = None
    # User who triggered the status change (for manual overrides)
    status_triggered_by_user: StatusTriggeredByUser | None = None
    # True if the most-recent build-level event is BUILD_RESUMED — i.e.
    # the build was picked up via sd.build(resume_build_id=...) after
    # finishing/failing. UI surfaces this as "running (resumed)".
    # Defaulted to False so older API responses (pre-resume support)
    # deserialize cleanly into clients that expect the field.
    is_resumed: bool = False


class BuildListResponse(BaseModel):
    """Schema for paginated build list."""

    builds: list[BuildResponse]
    total: int
    page: int
    page_size: int


# --- Task Schemas ---


class TaskCreate(BaseModel):
    """Schema for registering a task to a build."""

    task_id: str
    task_namespace: str = ""
    task_name: str
    task_data: dict
    version: str | None = None
    output_uri: str | None = None  # Path to task output (if FileSystemTarget)
    dependency_task_ids: list[str] = []  # task_ids of upstream dependencies


class TaskBulkCreate(BaseModel):
    """Schema for bulk-registering multiple tasks to a build.

    Tasks are processed in array order in a single transaction. Order
    matters: with the SDK's post-order discover, deps appear earlier in
    the array than parents, so when a parent's dependency_task_ids resolve
    they find existing rows (no phantom-creation in
    _reconcile_dependency_edges). The endpoint deduplicates by ``task_id``
    keeping the first occurrence, so callers don't pay event-emission
    cost for accidental duplicates within a single batch.
    """

    tasks: list[TaskCreate]


class TaskResponse(BaseModel):
    """Schema for task response."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    task_id: str
    environment_id: UUID
    task_namespace: str
    task_name: str
    task_data: dict
    version: str | None
    output_uri: str | None = None
    created_at: datetime
    # True for placeholder rows auto-created by ``_reconcile_dependency_edges``
    # when an edge points at a not-yet-registered task. With the SDK's
    # post-order discover walk this is rare; UI treats phantoms as
    # incomplete/registering rows.
    is_phantom: bool = False


class TaskBulkResponse(BaseModel):
    """Full response from a bulk task registration.

    Returned when ``id_only=false`` (the default) — each task in the
    response carries its complete state (task_data, namespace, etc.),
    matching the single-task ``register_task`` shape.
    """

    tasks: list[TaskResponse]


class BulkTaskIdRef(BaseModel):
    """Slim id-only reference to a registered task in a bulk response.

    Also carries the task's current (global) execution state so the SDK's
    build engine learns — with zero extra roundtrips — whether a task is
    already RUNNING with a re-attachable detached execution (see
    ``latest_executor_ref`` on the Task model). The executor fields are
    only meaningful when ``latest_status`` is RUNNING.
    """

    id: UUID
    task_id: str
    latest_status: TaskStatus | None = None
    latest_executor: str | None = None
    latest_executor_ref: str | None = None


class FrontierTaskRef(BaseModel):
    """A task in a build's scheduling frontier (see BuildFrontierResponse)."""

    task_id: str
    latest_status: TaskStatus
    latest_executor: str | None = None
    latest_executor_ref: str | None = None
    # When the current status was recorded — lets schedulers apply
    # staleness bounds (e.g. fail a long-RUNNING task with no executor
    # ref, which would otherwise hold concurrency-limit slots forever).
    latest_status_at: datetime | None = None


class BuildFrontierResponse(BaseModel):
    """Scheduling state of a build, for reactive scheduler ticks.

    ``actionable`` holds the tasks a scheduler can act on: global status
    PENDING / SUSPENDED / RUNNING with **no incomplete upstream dependency**
    (static or dynamic edges). The scheduler partitions them client-side:
    PENDING/SUSPENDED → spawn; RUNNING → verify the detached execution ref
    is still live, re-spawn or self-heal completion otherwise.

    ``status_counts`` covers *all* tasks referenced by the build, keyed by
    global status value — used for terminal detection (e.g. nothing running
    and nothing actionable).
    """

    build_id: UUID
    build_status: BuildStatus
    needs_tick: bool
    root_task_ids: list[str]
    roots: list[FrontierTaskRef]
    status_counts: dict[str, int]
    actionable: list[FrontierTaskRef]
    # All RUNNING tasks in the build, including non-actionable ones (e.g.
    # inside the dynamic-dep registration window) — cancellation targets.
    running: list[FrontierTaskRef] = []


class AddBuildRootsRequest(BaseModel):
    """Root task ids to append to a build (dedup/order handled server-side)."""

    root_task_ids: list[str]


class ConcurrencyLimitResponse(BaseModel):
    """A named environment concurrency limit."""

    key: str
    max_concurrent: int


class ConcurrencyLimitList(BaseModel):
    limits: list[ConcurrencyLimitResponse]


class ConcurrencyLimitUpsert(BaseModel):
    max_concurrent: int = Field(ge=1)


class SkipBlockedResponse(BaseModel):
    """Tasks skipped by POST /builds/{id}/skip-blocked."""

    build_id: UUID
    skipped_task_ids: list[str]


class BuildNotifyResponse(BaseModel):
    """Response of the build notify (scheduler wake-up flag) endpoints."""

    build_id: UUID
    needs_tick: bool


class TaskBulkIdOnlyResponse(BaseModel):
    """Lightweight response from a bulk task registration.

    Returned when ``?id_only=true``. Contains only the
    (database PK ↔ task_id) mapping — no task_data, no namespace, no
    timestamps. Saves bandwidth + serialisation cost when the caller
    (e.g. the SDK's build engine) doesn't need the full state echoed
    back. For a 50-task batch with rich task_data this is the
    difference between ~50 KB and ~3 KB on the wire.
    """

    tasks: list[BulkTaskIdRef]


class AddDependenciesRequest(BaseModel):
    """Schema for registering dependency edges on an existing task.

    Used by the SDK to record dynamically-yielded dependencies at runtime
    (dependencies that aren't known until a task's ``run()`` yields them).
    """

    upstream_task_ids: list[str]
    is_dynamic: bool = True


class AddDependenciesResponse(BaseModel):
    """Response for ``add_task_dependencies``."""

    added: int
    total: int


class TaskWithStatusResponse(TaskResponse):
    """Task response with status derived from events (global across all builds)."""

    status: TaskStatus = TaskStatus.PENDING
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error_message: str | None = None
    artifact_count: int = 0
    # Global status fields
    waiting_for_lock: bool = False
    # Build where the status-determining event occurred (for cross-build indicators)
    status_build_id: UUID | None = None
    # Git commit hash from the event that determined the current status
    commit_hash: str | None = None


class TaskEventResponse(BaseModel):
    """Slim response for task lifecycle events (start, complete, fail, etc.)."""

    task_id: str
    status: TaskStatus


class TaskListResponse(BaseModel):
    """Schema for paginated task list."""

    tasks: list[TaskResponse]
    total: int
    page: int
    page_size: int


# --- Event Schemas ---


class EventCreate(BaseModel):
    """Schema for creating an event (internal use)."""

    event_type: EventType
    task_id: UUID | None = None
    error_message: str | None = None
    event_metadata: dict | None = None


class EventResponse(BaseModel):
    """Schema for event response."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    build_id: UUID
    task_id: UUID | None
    event_type: EventType
    created_at: datetime
    error_message: str | None
    event_metadata: dict | None


class EventListResponse(BaseModel):
    """Schema for paginated event list."""

    events: list[EventResponse]
    total: int
    page: int
    page_size: int


# --- Graph/Dependency Schemas ---


class TaskDependencyResponse(BaseModel):
    """Schema for task dependency edge."""

    upstream_task_id: UUID
    downstream_task_id: UUID


class TaskNode(BaseModel):
    """Node in the task graph."""

    id: UUID
    task_id: str
    task_name: str
    task_namespace: str
    status: TaskStatus = TaskStatus.PENDING
    artifact_count: int = 0


class TaskEdge(BaseModel):
    """Edge in the task graph."""

    source: UUID  # upstream task id
    target: UUID  # downstream task id
    is_dynamic: bool = False  # True if edge was discovered at runtime via yield


class TaskGraphResponse(BaseModel):
    """DAG visualization data."""

    nodes: list[TaskNode]
    edges: list[TaskEdge]


class TaskNodeExtended(TaskNode):
    """Extended node with traversal metadata."""

    is_primary: bool = True
    traversal_depth: int = 0


class GroupSummary(BaseModel):
    """Summary for a batch of same-type tasks collapsed into one node."""

    group_id: str
    task_name: str
    task_namespace: str
    count: int
    sample_task_ids: list[str]
    depth: int
    status: TaskStatus = TaskStatus.PENDING
    downstream_task_pks: list[str]


class TaskEdgeExtended(BaseModel):
    """Edge that can reference both UUID task IDs and string group IDs."""

    source: str
    target: str
    is_dynamic: bool = False


class TaskGraphRequest(BaseModel):
    """Request body for the POST /tasks/graph endpoint."""

    task_ids: list[str] = Field(..., description="List of task_id hashes")
    upstream_depth: int = Field(0, ge=0, le=100)
    downstream_depth: int = Field(0, ge=0, le=100)
    max_per_type_per_level: int = Field(5, ge=1, le=200)
    max_total_nodes: int = Field(500, ge=1, le=5000)


class TaskGraphExtendedResponse(BaseModel):
    """Extended DAG visualization data with upstream traversal."""

    nodes: list[TaskNodeExtended]
    edges: list[TaskEdgeExtended]
    groups: list[GroupSummary] = []
    truncated: bool = False
    total_upstream_count: int = 0
    total_downstream_count: int = 0


# --- API Key Schemas ---


class ApiKeyCreate(BaseModel):
    """Schema for creating an API key."""

    name: str


class ApiKeyResponse(BaseModel):
    """Schema for API key response (without the actual key)."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    environment_id: UUID
    name: str
    key_prefix: str
    created_by_id: UUID | None
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None

    @property
    def is_active(self) -> bool:
        """Check if the API key is active."""
        return self.revoked_at is None


class ApiKeyCreateResponse(ApiKeyResponse):
    """Schema for API key creation response (includes the full key once)."""

    key: str  # The full key, only returned on creation


# --- Task Artifact Schemas ---


class TaskArtifactCreate(BaseModel):
    """Schema for creating a task artifact.

    Body format:
    - For markdown: {"content": "<markdown string>"}
    - For json: the actual JSON data dict
    """

    type: str  # "markdown" or "json"
    name: str
    body: dict  # Always a dict - markdown uses {"content": "..."}, json uses the data


class TaskArtifactResponse(BaseModel):
    """Schema for task artifact response."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    task_id: str  # The task_id hash (not the internal PK)
    artifact_type: str
    name: str
    body: dict  # Always a dict from body_json
    created_at: datetime


class TaskArtifactListResponse(BaseModel):
    """Schema for task artifacts list response."""

    artifacts: list[TaskArtifactResponse]


# --- Task Search Schemas ---


class TaskSearchResult(BaseModel):
    """Schema for a task in search results with build context."""

    model_config = ConfigDict(from_attributes=True)

    task_id: str
    environment_id: UUID
    task_namespace: str
    task_name: str
    task_data: dict
    version: str | None
    output_uri: str | None = None
    created_at: datetime
    # Build context (most recent build the task appeared in)
    build_id: UUID | None = None
    build_name: str | None = None
    status: TaskStatus = TaskStatus.PENDING
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error_message: str | None = None
    artifact_count: int = 0
    # Artifact data - mapping of artifact_name -> body_json (populated when artifact columns requested)
    artifact_data: dict[str, dict] = {}


class TaskSearchResponse(BaseModel):
    """Schema for task search results."""

    tasks: list[TaskSearchResult]
    total: int
    page: int
    page_size: int
    available_columns: list[str] = []


class KeySuggestion(BaseModel):
    """Schema for an autocomplete key suggestion."""

    key: str
    type: str = "string"  # string, number, boolean, object
    count: int = 0


class KeySuggestionsResponse(BaseModel):
    """Schema for key suggestions response."""

    keys: list[KeySuggestion]


class ValueSuggestion(BaseModel):
    """Schema for an autocomplete value suggestion."""

    value: str
    count: int = 0


class ValueSuggestionsResponse(BaseModel):
    """Schema for value suggestions response."""

    values: list[ValueSuggestion]


class AvailableColumnsResponse(BaseModel):
    """Schema for available columns response."""

    core: list[str]
    params: list[str]
    artifacts: list[str]


# --- Lock Schemas ---


class LockAcquireRequest(BaseModel):
    """Schema for acquiring a distributed lock."""

    owner_id: UUID  # UUID identifying the lock owner (stable across retries)
    ttl_seconds: int = 60  # Time-to-live in seconds
    check_task_completion: bool = True  # Check if task is already completed


class LockRenewRequest(BaseModel):
    """Schema for renewing a distributed lock."""

    owner_id: UUID  # The expected owner
    ttl_seconds: int = 60  # New TTL in seconds


class LockReleaseRequest(BaseModel):
    """Schema for releasing a distributed lock."""

    owner_id: UUID  # The expected owner
    task_completed: bool = False  # Whether the task completed successfully
    build_id: UUID | None = None  # Build ID if recording completion


class LockResponse(BaseModel):
    """Schema for lock details."""

    model_config = ConfigDict(from_attributes=True)

    name: str
    environment_id: UUID
    owner_id: UUID
    acquired_at: datetime
    expires_at: datetime
    version: int


class LockAcquireResponse(BaseModel):
    """Schema for lock acquisition response."""

    status: str  # acquired, already_completed, held_by_other, concurrency_limit_reached
    acquired: bool
    lock: LockResponse | None = None
    error_message: str | None = None


class LockRenewResponse(BaseModel):
    """Schema for lock renewal response."""

    renewed: bool


class LockReleaseResponse(BaseModel):
    """Schema for lock release response."""

    released: bool


class LockListResponse(BaseModel):
    """Schema for list of locks."""

    locks: list[LockResponse]
    count: int


class TaskCompletionStatusResponse(BaseModel):
    """Schema for task completion status check."""

    task_id: str
    is_completed: bool


# --- Task Metadata Schema (for SDK task_get_metadata) ---


class TaskMetadataResponse(BaseModel):
    """Schema for task metadata response (matches SDK TaskMetadata).

    This schema is used by the SDK's task_get_metadata method to retrieve
    task metadata for creating AliasTask instances.
    """

    model_config = ConfigDict(from_attributes=True)

    # Core Task fields
    id: str  # task_id (UUID string)
    body: dict  # Full task_data
    name: str  # task_name
    namespace: str  # task_namespace
    version: str
    output_uri: str | None  # Path to task output (if FileSystemTarget)
    # Registry Metadata fields
    status: TaskStatus
    registered_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    error_message: str | None
