"""Registration of the static phase: plans, member chunks, seal, closure.

The one registration path (engineering rule 2) for static, closure and —
from step 3 — dynamic admission. See ``docs/design/registry-v2/design.md``,
"Registration". Every public function here is one transaction and commits
it; a refusal rolls it back.

Interface only in this commit: the invariant tests are written against it
before the implementation lands.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.schemas_v2 import RegistrationItem

#: The largest chunk ``register_members`` accepts.
MAX_CHUNK_ITEMS = 1000


@dataclass(frozen=True)
class PlanState:
    """A plan's identity and lifecycle, as a registration call left it."""

    id: UUID
    build_id: UUID
    deployment_id: UUID
    settings_hash: str
    generation: int
    activated_at: datetime | None
    sealed_at: datetime | None
    superseded_at: datetime | None
    #: True only for the call that inserted the plan.
    created: bool = False


@dataclass(frozen=True)
class MembersResult:
    """What one chunk changed. Every count is zero on a re-delivery."""

    tasks_created: int = 0
    instances_created: int = 0
    members_admitted: int = 0
    closure_admitted: int = 0
    edges_created: int = 0
    completed: int = 0
    invalidated: int = 0
    diverged: int = 0


@dataclass(frozen=True)
class ClosureConflict:
    """Two instances of one completion that one plan cannot both hold."""

    task_id: str
    member_instance_id: UUID
    other_instance_id: UUID
    fields: list[str]


@dataclass(frozen=True)
class ClosureResult:
    admitted: int
    conflicts: list[ClosureConflict]
    #: The closure found a conflict and failed the build (BUILD_FAILED).
    build_failed: bool


def canonical_json(value: object) -> bytes:
    """Sorted keys, compact separators, UTF-8."""
    raise NotImplementedError


def settings_hash(body: Mapping[str, str]) -> str:
    """sha256 hex of the canonical JSON of a settings body."""
    raise NotImplementedError


async def create_plan(
    session: AsyncSession,
    environment_id: UUID,
    *,
    build_id: UUID,
    plan_id: UUID,
    deployment_id: UUID,
    settings_body: Mapping[str, str],
    roots: Sequence[RegistrationItem],
) -> PlanState:
    raise NotImplementedError


async def register_members(
    session: AsyncSession,
    environment_id: UUID,
    plan_id: UUID,
    items: Sequence[RegistrationItem],
) -> MembersResult:
    raise NotImplementedError


async def seal_plan(
    session: AsyncSession, environment_id: UUID, plan_id: UUID
) -> PlanState:
    raise NotImplementedError


async def closure(
    session: AsyncSession, environment_id: UUID, plan_id: UUID
) -> ClosureResult:
    raise NotImplementedError
