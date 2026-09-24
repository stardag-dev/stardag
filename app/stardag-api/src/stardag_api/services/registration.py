"""Registration of the static phase: plans, member chunks, seal, closure.

The one registration path (engineering rule 2) for root, static and
closure admission — and, from step 3, the dynamic one. See
``docs/design/registry-v2/design.md``, "Registration". Every public function
here is one transaction and commits it; a refusal rolls all of it back. The
per-item steps are in ``registration_chunk.py``; seal and closure in
``plans.py``.

Locking, which is what makes concurrent registration safe (S16):

- Rows are inserted with ``INSERT … ON CONFLICT DO NOTHING RETURNING`` and
  read back with a plain ``SELECT``. A second transaction inserting the same
  brand-new key waits on the unique index until the first commits, then
  finds the row: no 500, and no lock on a row that did not exist.
- Every multi-row insert is ordered by ``(task_id, instance_hash)``, so two
  chunks sharing keys meet them in the same order and cannot deadlock on
  unique-index waits.
- ``task`` is never locked here except through ``transition_task()`` when
  an observation changes a status, which takes ``FOR NO KEY UPDATE`` in
  ``task_id`` order. Plan creation and sealing serialise per build on the
  ``build`` row (``FOR NO KEY UPDATE``, which does not block the FK key-share
  locks of event inserts).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.models import (
    Build,
    Deployment,
    Plan,
    PlanMember,
    SettingsRecord,
    TaskInstance,
)
from stardag_api.models.base import utc_now
from stardag_api.schemas_v2 import RegistrationItem
from stardag_api.services.errors import BadRequest, Conflict, NotFound
from stardag_api.services.registration_chunk import MembersResult, register_items
from stardag_api.services.tx import transaction

__all__ = [
    "MAX_CHUNK_ITEMS",
    "MembersResult",
    "PlanState",
    "canonical_json",
    "create_plan",
    "get_plan",
    "lock_build",
    "register_members",
    "settings_hash",
]

#: The largest chunk ``register_members`` accepts.
MAX_CHUNK_ITEMS = 1000


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


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

    @classmethod
    def of(cls, plan: Plan, *, created: bool = False) -> PlanState:
        return cls(
            id=plan.id,
            build_id=plan.build_id,
            deployment_id=plan.deployment_id,
            settings_hash=plan.settings_hash,
            generation=plan.generation,
            activated_at=plan.activated_at,
            sealed_at=plan.sealed_at,
            superseded_at=plan.superseded_at,
            created=created,
        )


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------


def canonical_json(value: object) -> bytes:
    """Sorted keys, compact separators, UTF-8."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def settings_hash(body: Mapping[str, str]) -> str:
    """sha256 hex of the canonical JSON of a settings body."""
    return hashlib.sha256(canonical_json(dict(body))).hexdigest()


# ---------------------------------------------------------------------------
# Transactions and shared reads
# ---------------------------------------------------------------------------


async def get_plan(session: AsyncSession, environment_id: UUID, plan_id: UUID) -> Plan:
    plan = await session.scalar(
        select(Plan).where(Plan.environment_id == environment_id, Plan.id == plan_id)
    )
    if plan is None:
        raise NotFound("unknown_plan", f"no plan {plan_id}", plan_id=str(plan_id))
    return plan


async def lock_build(
    session: AsyncSession, environment_id: UUID, build_id: UUID
) -> Build:
    """The build row, locked ``FOR NO KEY UPDATE``: serialises plan creation
    and sealing per build without blocking event inserts' FK checks."""
    build = await session.scalar(
        select(Build)
        .where(Build.environment_id == environment_id, Build.id == build_id)
        .with_for_update(key_share=True)
    )
    if build is None:
        raise NotFound("unknown_build", f"no build {build_id}", build_id=str(build_id))
    return build


# ---------------------------------------------------------------------------
# create_plan
# ---------------------------------------------------------------------------


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
    """Look up or create the build's plan for ``(deployment, settings)``.

    The roots are admitted first and unexpanded (``is_root``, ``admitted_by
    = root``), so a crash at any later point leaves them as discovery jobs.
    A re-trigger with other root instances under the same scope is 409
    ``root_instance_conflict``; a re-send of the same instances goes through
    the item path again, so a changed body or task identity is refused as
    on any other registration. The first plan of a build is activated here.
    """
    if not roots:
        raise BadRequest("no_roots", "a plan needs at least one root")
    if any(r.declared_upstreams is not None for r in roots):
        raise BadRequest(
            "root_declared_upstreams",
            "roots are registered unexpanded; send their expansion as members",
        )
    async with transaction(session):
        now = utc_now()
        await lock_build(session, environment_id, build_id)
        deployment = await session.scalar(
            select(Deployment).where(
                Deployment.environment_id == environment_id,
                Deployment.id == deployment_id,
            )
        )
        if deployment is None:
            raise BadRequest(
                "unknown_deployment",
                f"no deployment {deployment_id}",
                deployment_id=str(deployment_id),
            )
        if deployment.activated_at is None:
            raise BadRequest(
                "deployment_not_activated",
                "a deployment that has not been activated cannot host a plan",
                deployment_id=str(deployment_id),
            )

        body = dict(settings_body)
        shash = settings_hash(body)
        await session.execute(
            pg_insert(SettingsRecord)
            .values(environment_id=environment_id, hash=shash, body=body)
            .on_conflict_do_nothing(constraint="pk_settings")
        )

        existing = await session.scalar(
            select(Plan).where(
                Plan.environment_id == environment_id,
                Plan.build_id == build_id,
                Plan.deployment_id == deployment_id,
                Plan.settings_hash == shash,
            )
        )
        if existing is not None:
            # The hash sets first, so another root instance is
            # root_instance_conflict rather than an instance_conflict on its
            # member; then the one item path, which is a no-op for what
            # landed and checks each root's body and task identity against
            # the recorded rows (instance_body_conflict,
            # task_identity_conflict) — a matching hash is not a matching
            # item.
            await _check_roots(session, existing, roots)
            await register_items(
                session, environment_id, existing, roots, as_roots=True, now=now
            )
            return PlanState.of(existing)

        if await session.get(Plan, plan_id) is not None:
            raise Conflict(
                "plan_id_conflict",
                f"plan id {plan_id} names another request",
                plan_id=str(plan_id),
            )
        previous = await session.scalar(
            select(func.max(Plan.generation)).where(Plan.build_id == build_id)
        )
        generation = (previous or 0) + 1
        plan = Plan(
            id=plan_id,
            environment_id=environment_id,
            build_id=build_id,
            deployment_id=deployment_id,
            settings_hash=shash,
            generation=generation,
            activated_at=now if generation == 1 else None,
        )
        session.add(plan)
        await session.flush()
        await register_items(
            session, environment_id, plan, roots, as_roots=True, now=now
        )
        return PlanState.of(plan, created=True)


async def _check_roots(
    session: AsyncSession, plan: Plan, roots: Sequence[RegistrationItem]
) -> None:
    recorded = set(
        (
            await session.scalars(
                select(TaskInstance.instance_hash)
                .join(PlanMember, PlanMember.instance_id == TaskInstance.id)
                .where(PlanMember.plan_id == plan.id, PlanMember.is_root)
            )
        ).all()
    )
    requested = {r.instance_hash for r in roots}
    if recorded != requested:
        raise Conflict(
            "root_instance_conflict",
            "this build already has a plan under this scope with other root"
            " instances; a build is one request — start a new build",
            plan_id=str(plan.id),
            unexpected=sorted(requested - recorded),
            missing=sorted(recorded - requested),
        )


# ---------------------------------------------------------------------------
# register_members: the one registration path
# ---------------------------------------------------------------------------


async def register_members(
    session: AsyncSession,
    environment_id: UUID,
    plan_id: UUID,
    items: Sequence[RegistrationItem],
) -> MembersResult:
    """Register one chunk (≤ :data:`MAX_CHUNK_ITEMS`) into a plan, atomically.

    Each chunk is self-consistent: an instance lands with its declared
    edges, and every upstream it names exists in the scope and is a member
    once the chunk commits. Re-delivery is a no-op.
    """
    if len(items) > MAX_CHUNK_ITEMS:
        raise BadRequest(
            "chunk_too_large",
            f"at most {MAX_CHUNK_ITEMS} items per chunk",
            items=len(items),
        )
    async with transaction(session):
        plan = await get_plan(session, environment_id, plan_id)
        return await register_items(
            session, environment_id, plan, items, as_roots=False, now=utc_now()
        )
