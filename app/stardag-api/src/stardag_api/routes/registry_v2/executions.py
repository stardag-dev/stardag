"""``/api/v2`` routes of one execution: an operator end (``stopped`` or
``lost``) reported by ``builds stop``.

Thin by rule: parse, resolve the environment from the credentials, call
one service, convert its result.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter

from stardag_api.models import ExecutionOutcome
from stardag_api.routes.registry_v2._common import Auth, Db
from stardag_api.schemas_v2 import (
    StoppedRequest,
    TransitionResponse,
)
from stardag_api.services import executions

router = APIRouter(tags=["registry-v2"])


@router.post("/executions/{execution_id}/stopped", response_model=TransitionResponse)
async def report_stopped(
    execution_id: UUID, db: Db, auth: Auth, body: StoppedRequest | None = None
):
    """The CLI reports an execution it stopped, or gave up on (``lost``)."""
    outcome = ExecutionOutcome((body or StoppedRequest()).outcome)
    return await executions.report_stopped(
        db, auth.environment_id, execution_id, outcome=outcome
    )
