"""Task artifacts: rich outputs of a completion (markdown reports, JSON).

Artifacts belong to the **promise** (``task``), not to an instance, a plan
or a build (design.md, "Peripheral tables, re-pointed"). The upload is
routed through the plan member whose execution produced them —
``POST /plans/{plan_id}/members/{task_id}/artifacts`` — which names the
task in the caller's environment; the artifacts are then the task's.

v1's semantics are kept: an artifact is unique per ``(task, type, name)``
and a re-upload replaces its body (an upsert, so a retried upload is a
no-op in effect). v1's guardrails too: the body-size limit and the
artifacts-per-task limit (429, as v1), both disabled unless configured.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.config import limits_settings
from stardag_api.limits import (
    ErrorCode,
    LimitExceededError,
    check_payload_size,
    check_structural_limit,
)
from stardag_api.models import Task, TaskArtifact
from stardag_api.models.base import generate_uuid7, utc_now
from stardag_api.services.errors import NotFound, TooManyRequests
from stardag_api.services.transitions import member_task_pk
from stardag_api.services.tx import transaction


@dataclass(frozen=True)
class ArtifactIn:
    artifact_type: str
    name: str
    body: Any


@dataclass(frozen=True)
class ArtifactView:
    id: UUID
    task_id: str
    artifact_type: str
    name: str
    body: Any
    created_at: datetime


def _guard(error: LimitExceededError | None) -> None:
    if error is not None:
        raise TooManyRequests(
            error.error_code.value.lower(),
            error.message,
            limit=error.limit,
            current=error.current,
        )


async def upload_artifacts(
    session: AsyncSession,
    environment_id: UUID,
    *,
    plan_id: UUID,
    task_id: str,
    artifacts: Sequence[ArtifactIn],
) -> list[ArtifactView]:
    """Upsert ``artifacts`` onto the member's task (404 ``not_a_member``
    unless ``task_id`` is a member of ``plan_id``). Within one request the
    last artifact of a ``(type, name)`` wins."""
    latest: dict[tuple[str, str], ArtifactIn] = {}
    for artifact in artifacts:
        _guard(
            check_payload_size(
                artifact.body,
                limits_settings.max_artifact_body_bytes,
                ErrorCode.ARTIFACT_BODY_SIZE_LIMIT,
                "artifact body",
            )
        )
        latest[(artifact.artifact_type, artifact.name)] = artifact
    async with transaction(session):
        task_pk = await member_task_pk(session, environment_id, plan_id, task_id)
        if not latest:
            return await _list(session, task_pk, task_id)
        if limits_settings.max_artifacts_per_task is not None:
            existing = await session.scalar(
                select(func.count())
                .select_from(TaskArtifact)
                .where(TaskArtifact.task_pk == task_pk)
            )
            replaced = await session.scalar(
                select(func.count())
                .select_from(TaskArtifact)
                .where(
                    TaskArtifact.task_pk == task_pk,
                    tuple_(TaskArtifact.artifact_type, TaskArtifact.name).in_(
                        list(latest)
                    ),
                )
            )
            _guard(
                check_structural_limit(
                    (existing or 0) + len(latest) - (replaced or 0),
                    limits_settings.max_artifacts_per_task,
                    ErrorCode.ARTIFACTS_PER_TASK_LIMIT,
                    "artifacts per task",
                )
            )
        now = utc_now()
        stmt = pg_insert(TaskArtifact).values(
            [
                {
                    "id": generate_uuid7(),
                    "environment_id": environment_id,
                    "task_pk": task_pk,
                    "artifact_type": a.artifact_type,
                    "name": a.name,
                    "body_json": a.body,
                    "created_at": now,
                }
                for a in latest.values()
            ]
        )
        await session.execute(
            stmt.on_conflict_do_update(
                constraint="uq_task_artifact_task_type_name",
                set_={"body_json": stmt.excluded.body_json},
            )
        )
        return await _list(session, task_pk, task_id)


async def list_artifacts(
    session: AsyncSession, environment_id: UUID, task_id: str
) -> list[ArtifactView]:
    """The task's artifacts, oldest first (v1's order)."""
    task_pk = await session.scalar(
        select(Task.id).where(
            Task.environment_id == environment_id, Task.task_id == task_id
        )
    )
    if task_pk is None:
        raise NotFound("unknown_task", f"no task {task_id}", task_id=task_id)
    return await _list(session, task_pk, task_id)


async def _list(
    session: AsyncSession, task_pk: UUID, task_id: str
) -> list[ArtifactView]:
    rows = (
        await session.scalars(
            select(TaskArtifact)
            .where(TaskArtifact.task_pk == task_pk)
            .order_by(TaskArtifact.created_at, TaskArtifact.id)
            .execution_options(populate_existing=True)
        )
    ).all()
    return [
        ArtifactView(
            id=r.id,
            task_id=task_id,
            artifact_type=r.artifact_type,
            name=r.name,
            body=r.body_json,
            created_at=r.created_at,
        )
        for r in rows
    ]


__all__ = ["ArtifactIn", "ArtifactView", "list_artifacts", "upload_artifacts"]
