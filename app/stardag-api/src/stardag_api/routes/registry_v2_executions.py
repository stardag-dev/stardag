"""``/api/v2`` routes for the execution ledger: ``builds stop``, orphans, and
a task's executions across builds.

Thin by rule: parse, resolve the environment from the credentials, call
one service in ``services/executions.py``.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.auth import SdkAuth, require_sdk_auth
from stardag_api.db import get_db
from stardag_api.schemas_v2 import (
    ExecutionListResponse,
    ExecutionResponse,
    StoppedRequest,
    TaskExecutionListResponse,
    TransitionResponse,
)
from stardag_api.services import executions

router = APIRouter(tags=["registry-v2"])

Db = Annotated[AsyncSession, Depends(get_db)]
Auth = Annotated[SdkAuth, Depends(require_sdk_auth)]


@router.get("/builds/{build_id}/executions", response_model=ExecutionListResponse)
async def list_executions(
    build_id: UUID,
    db: Db,
    auth: Auth,
    not_in_current_plan: bool = False,
    include_ended: bool = False,
):
    """The build's executions with no end reported (what ``builds stop``
    lists); ``not_in_current_plan`` keeps the orphans, ``include_ended``
    lists the whole ledger."""
    rows = await executions.list_executions(
        db,
        auth.environment_id,
        build_id,
        not_in_current_plan=not_in_current_plan,
        include_ended=include_ended,
    )
    return ExecutionListResponse(
        build_id=build_id,
        executions=[ExecutionResponse.model_validate(r) for r in rows],
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


@router.post("/executions/{execution_id}/stopped", response_model=TransitionResponse)
async def report_stopped(
    execution_id: UUID, db: Db, auth: Auth, body: StoppedRequest | None = None
):
    """The CLI reports an execution it stopped (``outcome = stopped``)."""
    del body  # the one outcome there is
    return await executions.report_stopped(db, auth.environment_id, execution_id)
