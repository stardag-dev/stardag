"""``/api/v2`` routes of a task (a completion): one with its instances,
the listing (paged; by status, name, namespace, tag, id prefix, free text),
its annotations, its executions across builds, its event log, its
artifacts, the claim renewal — and one instance by id.

Thin by rule: parse, resolve the environment from the credentials, call
one service, convert its result.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query

from stardag_api.models import TaskStatus
from stardag_api.services.errors import BadRequest
from stardag_api.routes.registry_v2._common import (
    Auth,
    Cursor,
    Db,
    Limit,
    artifact_list,
    event_list,
)
from stardag_api.schemas_v2 import (
    ExecutionResponse,
    RenewRequest,
    TaskExecutionListResponse,
    TransitionResponse,
)
from stardag_api.schemas_v2_reads import (
    EventListResponse,
    InstanceResponse,
    TaskAnnotateRequest,
    TaskArtifactListResponse,
    TaskListResponse,
    TaskResponse,
    TaskSummaryResponse,
)
from stardag_api.services import (
    annotations,
    artifacts,
    executions,
    reads,
    transitions,
)

router = APIRouter(tags=["registry-v2"])


@router.post("/tasks/{task_id}/claim/renew", response_model=TransitionResponse)
async def renew_claim(task_id: str, body: RenewRequest, db: Db, auth: Auth):
    return await transitions.renew_claim(
        db,
        auth.environment_id,
        task_id=task_id,
        execution_id=body.execution_id,
        claim_ttl_seconds=body.claim_ttl_seconds,
    )


@router.get("/tasks/{task_id}/executions", response_model=TaskExecutionListResponse)
async def list_task_executions(
    task_id: str,
    db: Db,
    auth: Auth,
    include_ended: bool = True,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
):
    """Every execution of the task, across builds, newest first."""
    rows = await executions.list_task_executions(
        db, auth.environment_id, task_id, include_ended=include_ended, limit=limit
    )
    return TaskExecutionListResponse(
        task_id=task_id,
        executions=[ExecutionResponse.model_validate(r) for r in rows],
    )


@router.get("/tasks", response_model=TaskListResponse)
async def list_tasks(
    db: Db,
    auth: Auth,
    status: TaskStatus | None = None,
    namespace: Annotated[str | None, Query(max_length=255)] = None,
    name: Annotated[str | None, Query(max_length=255)] = None,
    tag: Annotated[list[str] | None, Query()] = None,
    id_prefix: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
    q: Annotated[str | None, Query(min_length=1, max_length=255)] = None,
    limit: Limit = 100,
    cursor: Cursor = None,
):
    """Completions, most recent status change first, a page at a time.
    Filters AND together: ``namespace`` and ``name`` match exactly
    (``namespace=`` is the root namespace); ``tag`` is repeatable and a task
    must carry **all** of them; ``id_prefix`` matches the start of the task
    id; ``q`` is a case-insensitive substring of the name, the namespace or
    the description. Pass the same filters with each ``cursor``."""
    try:
        tags = annotations.normalise_tags(tag) if tag else None
    except ValueError as exc:
        raise BadRequest("invalid_tag", str(exc)) from None
    page = await reads.list_tasks(
        db,
        auth.environment_id,
        status=status,
        namespace=namespace,
        name=name,
        tags=tags,
        id_prefix=id_prefix,
        q=q,
        limit=limit,
        cursor=cursor,
    )
    return TaskListResponse(
        tasks=[TaskSummaryResponse.model_validate(t) for t in page.tasks],
        next_cursor=page.next_cursor,
    )


@router.patch("/tasks/{task_id}", response_model=TaskSummaryResponse)
async def annotate_task(task_id: str, body: TaskAnnotateRequest, db: Db, auth: Auth):
    """Set a completion's annotations (tags, description); omitted fields
    are left as they are. Counted against the workspace's write rate limit
    like any write that is not finishing running work."""
    return TaskSummaryResponse.model_validate(
        await annotations.annotate_task(
            db, auth.environment_id, task_id, body.changes()
        )
    )


@router.get("/instances/{instance_id}", response_model=InstanceResponse)
async def get_instance(instance_id: UUID, db: Db, auth: Auth):
    """One instance of the caller's environment by its row id (404
    ``unknown_instance`` otherwise)."""
    return InstanceResponse.model_validate(
        await reads.get_instance(db, auth.environment_id, instance_id)
    )


@router.get("/tasks/{task_id}", response_model=TaskResponse)
async def get_task(task_id: str, db: Db, auth: Auth, limit: Limit = 50):
    return await reads.get_task(db, auth.environment_id, task_id, limit=limit)


@router.get("/tasks/{task_id}/artifacts", response_model=TaskArtifactListResponse)
async def list_artifacts(task_id: str, db: Db, auth: Auth):
    return artifact_list(
        await artifacts.list_artifacts(db, auth.environment_id, task_id)
    )


@router.get("/tasks/{task_id}/events", response_model=EventListResponse)
async def task_events(task_id: str, db: Db, auth: Auth, limit: Limit = 500):
    return event_list(
        await reads.list_events(db, auth.environment_id, task_id=task_id, limit=limit)
    )
