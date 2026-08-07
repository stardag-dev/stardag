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
    # Executor-descriptive metadata of the trigger (e.g. the Modal
    # app/workspace/environment of a build_trigger call).
    executor_metadata: dict | None = None


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
    # Executor-descriptive metadata of the trigger that created (or most
    # recently resumed-with-metadata) the build. None for builds without
    # a recorded trigger executor.
    executor_metadata: dict | None = None
    # Reactive-scheduling owner: the app whose ticks drive this build. None
    # for non-reactive builds — its presence is the reactive-scheduling
    # marker.
    reactive_app_name: str | None = None
    # Reactive-scheduler tick configuration (a TickConfig kwargs dict); None
    # for non-reactive builds (and treated as {} when the marker is set but
    # no kwargs were given).
    reactive_tick_kwargs: dict | None = None
    # ---- Liveness. Two different numbers; do not confuse them. ----
    #
    # ``last_active_at`` is the ``builds`` column that drives list ordering.
    # It is bumped by build-level LIFECYCLE transitions only (resume,
    # complete, fail, cancel, exit-early, roots appended) — never by task
    # events, deliberately, so the per-task hot path doesn't contend on the
    # build row. It is therefore **not** an activity signal: a build that has
    # been busily running tasks for three days still shows the timestamp of
    # its last lifecycle change.
    #
    # ``last_activity_at`` is that activity signal: the most recent moment
    # anything at all happened for this build — max over the build's whole
    # event stream (task events included), its ``last_active_at``, and any
    # pending scheduler wake-up. This is the number the stale-build reaper
    # measures idleness with, exposed so a UI can show operators exactly what
    # the reaper will act on.
    last_active_at: datetime | None = None
    last_activity_at: datetime | None = None


class BuildListResponse(BaseModel):
    """Schema for paginated build list."""

    builds: list[BuildResponse]
    total: int
    page: int
    page_size: int


class BuildCancelResponse(BuildResponse):
    """Response of ``POST /builds/{id}/cancel``.

    A superset of :class:`BuildResponse` — clients written against the plain
    build shape are unaffected. ``cascaded_task_ids`` is empty unless the
    call passed ``cascade=true``.
    """

    # Tasks this call moved to CANCELLED, releasing their execution claims
    # and any concurrency-limit slots they occupied.
    cascaded_task_ids: list[str] = []
    cascaded_task_count: int = 0


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
    # Executor identity of the most recent TASK_STARTED event (set/cleared
    # on every start — see the Task model). ``latest_executor_metadata``
    # carries descriptive backend details (e.g. Modal app/workspace/
    # environment/function) for UI deep links.
    latest_executor: str | None = None
    latest_executor_ref: str | None = None
    latest_executor_metadata: dict | None = None
    # Denormalised *global* status of the task — the environment-wide state,
    # not "the state within some build". A task row is unique per
    # (environment_id, task_id), so a task left RUNNING by any build denies
    # the execution claim to every other build until something moves it.
    # That makes these three the answer to "who is holding this claim, and
    # since when":
    #
    #   latest_status_build_id — the build whose event produced the current
    #     status, i.e. the claim holder. Null only for rows predating status
    #     denormalisation.
    #   latest_status_at — when it entered that status ("running since").
    #
    # Defaulted so the fields are purely additive for clients built against
    # the older shape. ``TaskWithStatusResponse`` also carries ``status`` /
    # ``status_build_id``; those are the same values under the names that
    # endpoint has always used, kept for compatibility.
    latest_status: TaskStatus | None = None
    latest_status_at: datetime | None = None
    latest_status_build_id: UUID | None = None


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
    latest_executor_metadata: dict | None = None


class FrontierTaskRef(BaseModel):
    """A task in a build's scheduling frontier (see BuildFrontierResponse)."""

    task_id: str
    latest_status: TaskStatus
    latest_executor: str | None = None
    latest_executor_ref: str | None = None
    latest_executor_metadata: dict | None = None
    # When the current status was recorded — lets schedulers apply
    # staleness bounds (e.g. fail a long-RUNNING task with no executor
    # ref, which would otherwise hold concurrency-limit slots forever).
    latest_status_at: datetime | None = None


class FrontierExternalBlocker(BaseModel):
    """An upstream outside this build that holds one of its tasks back.

    Task rows (and their dependency edges) are per *environment*, not per
    build, so an upstream left non-COMPLETED by some *other* build still
    gates this build's downstream tasks — silently, since such an upstream
    need not be part of this build's task set at all (a dynamic dependency
    registered under an earlier build is the common case). Each entry pairs
    one blocked task of this build with one such blocker.

    "Not this build's doing" is decided by ``blocking_status_build_id``:
    the build whose event produced the blocker's current status. If that is
    not this build, this build did not put the blocker in that state and
    cannot generally get it out of it.

    The blocked side carries only ``task_id`` (the caller registered it, and
    every other frontier field is task_id-keyed too); the *blocking* side
    carries name/namespace because it may be entirely unknown to the caller,
    and a diagnostic is useless without something human-readable.
    """

    # Blocked task — always a member of this build's task set.
    task_id: str
    # The blocking upstream. Identity is spelled out because the caller may
    # never have seen this task.
    blocking_task_id: str
    blocking_task_namespace: str
    blocking_task_name: str
    blocking_status: TaskStatus
    # When the blocker entered its current status, and the build whose event
    # put it there ("running under build Z since T"). The build id is null
    # only for rows predating status denormalisation.
    blocking_status_at: datetime | None = None
    blocking_status_build_id: UUID | None = None
    # Whether the blocker is also part of *this* build's task set. False is
    # the pathological case: this build will never schedule it, so it can
    # only wait for whoever owns it. True still blocks, but the blocker
    # shows up in this build's own ``actionable``/``running`` as well.
    blocking_in_build: bool


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

    ``blocked_by_external`` explains the gap between the two: dependency
    gating is environment-global while ``running``/``status_counts`` are
    scoped to this build, so a build can have nothing actionable and
    nothing running yet still be legitimately waiting. See
    :class:`FrontierExternalBlocker`.
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
    # Non-terminal tasks of this build held back by an upstream this build
    # doesn't own. Capped, in which case blocked_by_external_truncated is set
    # (it is a diagnostic, not a work queue — a truncated list still proves
    # "waiting, not stuck").
    #
    # Populated ONLY when the build has nothing actionable and nothing
    # running, i.e. when it looks stuck — which is the only state in which
    # the answer matters, and keeps a per-edge sort off the hot path of
    # every healthy build's linger polls. So an EMPTY list means "not
    # externally blocked, OR not stalled"; do not read it as proof that no
    # external blocker exists while the build is still progressing.
    blocked_by_external: list[FrontierExternalBlocker] = []
    blocked_by_external_truncated: bool = False
    # Reactive-scheduling owner: the app whose ticks drive this build. None
    # for non-reactive builds — its presence is the marker a tick reads to
    # decide whether to drive the build (and which app owns it).
    reactive_app_name: str | None = None
    # Reactive-scheduler tick configuration (a TickConfig kwargs dict); None
    # for non-reactive builds (treated as {} when the marker is set but no
    # kwargs were given). Read from the frontier by every tick.
    reactive_tick_kwargs: dict | None = None


class AddBuildRootsRequest(BaseModel):
    """Root task ids to append to a build (dedup/order handled server-side)."""

    root_task_ids: list[str]


class SetReactiveMetaRequest(BaseModel):
    """Reactive-scheduling metadata for PUT /builds/{id}/reactive-meta.

    ``tick_kwargs`` defaults to None meaning "leave the stored config
    untouched": a bare re-trigger (no explicit tick_kwargs) then preserves
    the existing ones, while a re-trigger that passes tick_kwargs updates
    them. ``app_name`` (the owner/marker) is always set.
    """

    app_name: str
    tick_kwargs: dict | None = None


class ConcurrencyLimitResponse(BaseModel):
    """A named environment concurrency limit."""

    key: str
    max_concurrent: int


class ConcurrencyLimitList(BaseModel):
    limits: list[ConcurrencyLimitResponse]


class ConcurrencyLimitUpsert(BaseModel):
    max_concurrent: int = Field(ge=1)


class ConcurrencyLimitHolder(BaseModel):
    """A RUNNING task currently occupying one slot of a concurrency-limit key."""

    task_id: str
    task_namespace: str
    task_name: str
    # When the task's RUNNING status was recorded ("running since").
    latest_status_at: datetime | None = None
    latest_executor: str | None = None
    latest_executor_ref: str | None = None
    latest_executor_metadata: dict | None = None


class ConcurrencyLimitHoldersResponse(BaseModel):
    """Current holders of a concurrency-limit key (admin drill-down).

    ``total`` is the full holder count; ``holders`` is capped by the
    ``limit`` query param (oldest running first).
    """

    key: str
    holders: list[ConcurrencyLimitHolder]
    total: int


class BulkCancelBuildsRequest(BaseModel):
    """Filter for ``POST /builds/bulk-cancel`` — bulk cleanup and the reaper.

    Bulk cancel and "reap builds idle beyond a threshold" are the same
    operation with different filters, so they are one endpoint rather than
    two near-identical code paths: pass ``build_ids`` for an explicit
    selection, ``idle_for_seconds`` for the reaper, or both.

    **Only RUNNING builds are ever eligible**, whatever the filter says —
    a build that already reached a terminal build-level event is skipped,
    which is what makes the call idempotent and safe to retry or run on a
    timer. At least one of ``build_ids`` / ``idle_for_seconds`` is required:
    an unqualified "cancel everything running in this environment" is not a
    cleanup operation and the endpoint refuses it (422).
    """

    # Explicit selection. Ids in another environment, unknown ids, and
    # already-terminal builds are silently skipped (reported via `skipped`)
    # rather than failing the batch — a cleanup that aborts halfway is worse
    # than one that reports what it did.
    build_ids: list[UUID] | None = None
    # The reaper filter: only builds with no activity of any kind for at
    # least this many seconds. A DURATION, not a timestamp (unlike
    # `GET /tasks?status_older_than=`), because this threshold is a
    # recurring policy — "anything quiet for a day is abandoned" — and a
    # fixed timestamp would go stale on the second invocation of a timer.
    # The unit is in the name so there is nothing to guess.
    #
    # Idleness is measured against `BuildResponse.last_activity_at`, NOT the
    # `last_active_at` column: see the field docs there and the endpoint
    # docstring. Minimum 60s — a threshold small enough to race a live build
    # is a foot-gun, not a feature.
    idle_for_seconds: int | None = Field(default=None, ge=60)
    # Restrict to builds driven by this reactive app (implies
    # include_reactive).
    reactive_app_name: str | None = None
    # Reactive builds are EXCLUDED by default. A reactive build is quiet
    # between ticks by design — the quiet is the feature — and it already
    # has a watchdog to notice when it wedges. Opt in when you know an app
    # is gone (e.g. undeployed) and its builds should be cleaned up.
    include_reactive: bool = False
    # Also cancel each build's claim-holding tasks. Defaults to TRUE here
    # (unlike the single-build cancel, where it defaults off to preserve
    # existing behaviour): releasing leaked claims and concurrency-limit
    # slots is the entire point of a cleanup pass, and a build cancelled
    # without it leaves exactly the problem behind that this endpoint exists
    # to fix.
    cascade: bool = True
    # Report what would happen and change nothing.
    dry_run: bool = False
    # Cap on builds handled per call. The scan itself is unbounded (the
    # reaper must be able to *find* every stale build — see the endpoint
    # docstring), only the write set is capped; `truncated` tells you to
    # call again.
    limit: int = Field(default=100, ge=1, le=500)
    # Free-text note recorded on each BUILD_CANCELLED event.
    reason: str | None = None


class CancelledBuildRef(BaseModel):
    """One build cancelled (or, in a dry run, selected) by bulk cancel."""

    build_id: UUID
    name: str
    # The idleness signal the selection was made on — the same value
    # ``BuildResponse.last_activity_at`` reports.
    last_activity_at: datetime | None = None
    reactive_app_name: str | None = None
    # Tasks cancelled along with the build (empty when cascade is off).
    cascaded_task_ids: list[str] = []


class BulkCancelBuildsResponse(BaseModel):
    """Result of ``POST /builds/bulk-cancel``.

    In a dry run, ``builds`` lists exactly what a real run would have
    cancelled and every count is the count it would have produced —
    nothing is written.
    """

    dry_run: bool
    builds: list[CancelledBuildRef]
    build_count: int
    task_count: int
    # Ids from ``build_ids`` that were not acted on, and why: "not_found"
    # (unknown, or another environment — the two are deliberately
    # indistinguishable so the endpoint can't be used to probe for build ids
    # in other environments), "not_running", "reactive" (excluded, pass
    # include_reactive), "not_idle".
    skipped: dict[str, str] = {}
    # More builds matched the filter than ``limit`` allowed; call again.
    truncated: bool = False


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
    # Executor identity + descriptive metadata of the most recent
    # TASK_STARTED event (see TaskResponse).
    latest_executor: str | None = None
    latest_executor_ref: str | None = None
    latest_executor_metadata: dict | None = None


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


# --- Build Tick Summary Schemas ---


class BuildTickSummaryCreate(BaseModel):
    """One reactive scheduler tick's summary, as reported by the SDK.

    Deliberately open (``extra="allow"``): the summary is an SDK-owned
    dataclass that keeps growing, and the server stores it verbatim so a
    new field needs no server release and no migration. ``outcome`` is
    the only required key — it is promoted to a typed column.
    """

    model_config = ConfigDict(extra="allow")

    outcome: str = Field(max_length=32)


class BuildTickSummaryResponse(BaseModel):
    """A persisted tick summary.

    ``summary`` is the reported dict verbatim (``outcome`` included), so
    a client can render fields this server has never heard of.
    """

    id: UUID
    build_id: UUID
    outcome: str
    summary: dict
    created_at: datetime


class BuildTickSummaryListResponse(BaseModel):
    """Retained tick summaries for a build, newest first."""

    build_id: UUID
    summaries: list[BuildTickSummaryResponse]
