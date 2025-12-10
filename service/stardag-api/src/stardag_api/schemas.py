from datetime import datetime

from pydantic import BaseModel

from stardag_api.models.task import TaskStatus


class TaskCreate(BaseModel):
    task_id: str
    task_family: str
    task_data: dict
    user: str
    commit_hash: str
    dependency_ids: list[str] = []


class TaskUpdate(BaseModel):
    status: TaskStatus | None = None
    error_message: str | None = None


class TaskResponse(BaseModel):
    task_id: str
    task_family: str
    task_data: dict
    status: TaskStatus
    user: str
    commit_hash: str
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    error_message: str | None
    dependency_ids: list[str]

    model_config = {"from_attributes": True}


class TaskListResponse(BaseModel):
    tasks: list[TaskResponse]
    total: int
    page: int
    page_size: int
