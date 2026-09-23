"""The frontier of a build's active plan: what can run, what needs
discovery, what is running. See design.md, "The runnable rule".

Interface only in this commit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.models.enums import TaskStatus
from stardag_api.services.plans import ClosureResult


@dataclass(frozen=True)
class FrontierMember:
    task_id: str
    task_pk: UUID
    instance_id: UUID
    instance_hash: str
    status: TaskStatus
    is_root: bool
    body: dict[str, Any]


@dataclass(frozen=True)
class Frontier:
    build_id: UUID
    #: The active plan, or None when the build has none yet.
    plan_id: UUID | None
    deployment_id: UUID | None
    settings_hash: str | None
    sealed: bool
    runnable: list[FrontierMember] = field(default_factory=list)
    discovery_jobs: list[FrontierMember] = field(default_factory=list)
    running: list[FrontierMember] = field(default_factory=list)
    #: Diagnostic: sealed, and every non-excluded member COMPLETED.
    plan_complete: bool = False
    #: The closure step's outcome (a conflict fails the build).
    closure: ClosureResult | None = None


async def get_frontier(
    session: AsyncSession, environment_id: UUID, build_id: UUID
) -> Frontier:
    raise NotImplementedError
