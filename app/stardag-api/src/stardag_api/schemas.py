"""Pydantic schemas for API request/response models."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from stardag_api.models.enums import BuildStatus, EventType, TaskStatus


# --- Organization Schemas ---


class OrganizationCreate(BaseModel):
    """Schema for creating an organization."""

    name: str
    slug: str
    description: str | None = None


class OrganizationResponse(BaseModel):
    """Schema for organization response."""

    model_config = ConfigDict(from_attributes=True)

    id: str
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

    id: str
    organization_id: str
    username: str
    display_name: str | None
    email: str | None
    created_at: datetime


# --- Workspace Schemas ---


class WorkspaceCreate(BaseModel):
    """Schema for creating a workspace."""

    name: str
    slug: str
    description: str | None = None


class WorkspaceResponse(BaseModel):
    """Schema for workspace response."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    organization_id: str
    name: str
    slug: str
    description: str | None
    created_at: datetime


# --- Build Schemas ---


class BuildCreate(BaseModel):
    """Schema for creating a build."""

    workspace_id: str = "default"
    user_id: str | None = None  # Optional until auth is implemented
    commit_hash: str | None = None
    root_task_ids: list[str] = []
    description: str | None = None


class BuildResponse(BaseModel):
    """Schema for build response with derived status."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    workspace_id: str
    user_id: str | None
    name: str
    description: str | None
    commit_hash: str | None
    root_task_ids: list[str]
    created_at: datetime
    # Derived fields (not from model directly)
    status: BuildStatus = BuildStatus.PENDING
    started_at: datetime | None = None
    completed_at: datetime | None = None


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
    dependency_task_ids: list[str] = []  # task_ids of upstream dependencies


class TaskResponse(BaseModel):
    """Schema for task response."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    task_id: str
    workspace_id: str
    task_namespace: str
    task_name: str
    task_data: dict
    version: str | None
    created_at: datetime


class TaskWithStatusResponse(TaskResponse):
    """Task response with status derived from events for a specific build."""

    status: TaskStatus = TaskStatus.PENDING
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error_message: str | None = None


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
    task_id: int | None = None
    error_message: str | None = None
    event_metadata: dict | None = None


class EventResponse(BaseModel):
    """Schema for event response."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    build_id: str
    task_id: int | None
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

    upstream_task_id: int
    downstream_task_id: int


class TaskNode(BaseModel):
    """Node in the task graph."""

    id: int
    task_id: str
    task_name: str
    task_namespace: str
    status: TaskStatus = TaskStatus.PENDING


class TaskEdge(BaseModel):
    """Edge in the task graph."""

    source: int  # upstream task id
    target: int  # downstream task id


class TaskGraphResponse(BaseModel):
    """DAG visualization data."""

    nodes: list[TaskNode]
    edges: list[TaskEdge]


# --- API Key Schemas ---


class ApiKeyCreate(BaseModel):
    """Schema for creating an API key."""

    name: str


class ApiKeyResponse(BaseModel):
    """Schema for API key response (without the actual key)."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    workspace_id: str
    name: str
    key_prefix: str
    created_by_id: str | None
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
