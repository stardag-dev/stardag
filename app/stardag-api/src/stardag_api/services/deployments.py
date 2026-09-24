"""Deployments and settings: the two halves of the deterministic scope.

See design.md, "The deterministic scope" and the ``deployment`` /
``settings`` entities.

- A **Modal** deployment is registered in two steps: ``create`` **before**
  the deploy (client-minted id; the server assigns ``generation``,
  monotonic per ``(environment, kind, app_name)``), ``activate`` after it
  succeeded. Order is fixed when a deploy starts, so a record that lands
  late cannot roll an app back to older code (S37).
- A **local** deployment has no deploy step: lookup-or-create by
  ``(environment, code_id)``, born activated, ``app_name`` ``"local"``
  unless the driver names one. ``kind`` is part of every lookup, so a local
  code id never collides with a Modal one (S9).
- **Current** for an app is the activated **Modal** row with the highest
  generation. A local deployment is never current and never superseded: it
  is authoritative for its own plans, so the seal's currency check, resume's
  reactivation check and rollover apply to ``kind = modal`` only.

Settings are a flat ``str → str`` body stored under the sha256 of its
canonical JSON; keys starting ``STARDAG_`` or ``MODAL_`` are reserved for
the framework and refused here as well as at the trigger.

Every write is idempotent by state: a re-sent create finds its row, a
re-sent activation finds it activated, and neither writes anything.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.models import Deployment, DeploymentKind, SettingsRecord
from stardag_api.models.base import generate_uuid7, utc_now
from stardag_api.services.errors import BadRequest, Conflict, NotFound
from stardag_api.services.tx import transaction

#: The ``app_name`` of a local deployment whose driver names no app.
LOCAL_APP_NAME = "local"

#: Prefixes of the framework's own environment variables; never settings.
RESERVED_SETTINGS_PREFIXES = ("STARDAG_", "MODAL_")


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def canonical_json(value: object) -> bytes:
    """Sorted keys, compact separators, UTF-8."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def settings_hash(body: Mapping[str, str]) -> str:
    """sha256 hex of the canonical JSON of a settings body."""
    return hashlib.sha256(canonical_json(dict(body))).hexdigest()


def validate_settings(body: Mapping[str, object]) -> dict[str, str]:
    """A flat ``str → str`` body with no reserved key, or 400."""
    checked: dict[str, str] = {}
    for key, value in body.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise BadRequest(
                "invalid_settings",
                "settings are a flat mapping of strings to strings",
                key=str(key),
            )
        if key.startswith(RESERVED_SETTINGS_PREFIXES):
            raise BadRequest(
                "reserved_settings_key",
                f"settings keys starting {' or '.join(RESERVED_SETTINGS_PREFIXES)}"
                " are reserved for the framework",
                key=key,
            )
        checked[key] = value
    return checked


async def ensure_settings(
    session: AsyncSession, environment_id: UUID, body: Mapping[str, object]
) -> str:
    """Validate, then insert-if-absent; returns the hash. No commit."""
    checked = validate_settings(body)
    shash = settings_hash(checked)
    await session.execute(
        pg_insert(SettingsRecord)
        .values(environment_id=environment_id, hash=shash, body=checked)
        .on_conflict_do_nothing(constraint="pk_settings")
    )
    return shash


async def get_settings(
    session: AsyncSession, environment_id: UUID, shash: str
) -> SettingsRecord:
    record = await session.scalar(
        select(SettingsRecord).where(
            SettingsRecord.environment_id == environment_id,
            SettingsRecord.hash == shash,
        )
    )
    if record is None:
        raise NotFound("unknown_settings", f"no settings {shash}", hash=shash)
    return record


# ---------------------------------------------------------------------------
# Deployments
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeploymentState:
    id: UUID
    kind: DeploymentKind
    app_name: str
    code_id: str
    image_id: str | None
    modal_app_id: str | None
    generation: int
    deployed_at: datetime
    activated_at: datetime | None
    #: Whether this is the app's current deployment (highest activated
    #: generation) as of the read.
    is_current: bool = False
    #: True only for the call that inserted the row.
    created: bool = False

    @classmethod
    def of(
        cls, row: Deployment, *, is_current: bool = False, created: bool = False
    ) -> DeploymentState:
        return cls(
            id=row.id,
            kind=row.kind,
            app_name=row.app_name,
            code_id=row.code_id,
            image_id=row.image_id,
            modal_app_id=row.modal_app_id,
            generation=row.generation,
            deployed_at=row.deployed_at,
            activated_at=row.activated_at,
            is_current=is_current,
            created=created,
        )


async def current_deployment_id(
    session: AsyncSession, environment_id: UUID, kind: DeploymentKind, app_name: str
) -> UUID | None:
    """The activated deployment with the highest generation for the app;
    None for a local one, which is never current."""
    if kind is DeploymentKind.LOCAL:
        return None
    return await session.scalar(
        select(Deployment.id)
        .where(
            Deployment.environment_id == environment_id,
            Deployment.kind == kind,
            Deployment.app_name == app_name,
            Deployment.activated_at.is_not(None),
        )
        .order_by(Deployment.generation.desc())
        .limit(1)
    )


async def verify_deployment_current(
    session: AsyncSession, environment_id: UUID, deployment_id: UUID
) -> None:
    """409 ``deployment_not_current`` unless the deployment is its app's
    current one: rollover only moves forward. A no-op for a local
    deployment, which is authoritative for its own plans."""
    deployment = await session.get(Deployment, deployment_id)
    assert deployment is not None  # FK
    if deployment.kind is DeploymentKind.LOCAL:
        return
    # The app lock, shared, held to the caller's commit: an activation (which
    # takes it exclusively) either committed before this read, and the check
    # sees it, or waits until the seal or reactivation has committed. So a
    # plan under D2 cannot be sealed after D3's activation committed.
    await _lock_app(
        session, environment_id, deployment.kind, deployment.app_name, shared=True
    )
    current = await current_deployment_id(
        session, environment_id, deployment.kind, deployment.app_name
    )
    if current != deployment_id:
        raise Conflict(
            "deployment_not_current",
            "the plan's deployment is no longer the app's current one;"
            " rollover only moves forward",
            deployment_id=str(deployment_id),
            current_deployment_id=str(current) if current else None,
        )


async def _lock_app(
    session: AsyncSession,
    environment_id: UUID,
    kind: DeploymentKind,
    app_name: str,
    *,
    shared: bool = False,
) -> None:
    """The app lock per ``(environment, kind, app)``: a transaction-scoped
    advisory lock, since the row that would carry it may not exist yet.
    Exclusive for what changes which deployment is current (create assigns
    the generation, activate makes it eligible); shared for the checks that
    read it (seal, resume's reactivation), which do not wait for each
    other."""
    await _advisory_lock(
        session, f"deployment:{environment_id}:{kind.value}:{app_name}", shared=shared
    )


async def _advisory_lock(
    session: AsyncSession, key: str, *, shared: bool = False
) -> None:
    fn = "pg_advisory_xact_lock_shared" if shared else "pg_advisory_xact_lock"
    await session.execute(text(f"SELECT {fn}(hashtextextended(:key, 0))"), {"key": key})


async def _next_generation(
    session: AsyncSession, environment_id: UUID, kind: DeploymentKind, app_name: str
) -> int:
    previous = await session.scalar(
        select(func.max(Deployment.generation)).where(
            Deployment.environment_id == environment_id,
            Deployment.kind == kind,
            Deployment.app_name == app_name,
        )
    )
    return (previous or 0) + 1


async def create_deployment(
    session: AsyncSession,
    environment_id: UUID,
    *,
    deployment_id: UUID | None,
    kind: DeploymentKind,
    app_name: str | None,
    code_id: str,
    image_id: str | None = None,
    modal_app_id: str | None = None,
) -> DeploymentState:
    """Create a Modal deployment (not yet activated), or look up or create
    a local one (born activated). Idempotent: see the module docstring."""
    async with transaction(session):
        if kind is DeploymentKind.LOCAL:
            return await _local(
                session,
                environment_id,
                deployment_id=deployment_id,
                app_name=app_name or LOCAL_APP_NAME,
                code_id=code_id,
            )
        if deployment_id is None:
            raise BadRequest(
                "deployment_id_required",
                "a Modal deployment's id is minted by the CLI before the deploy",
            )
        if not app_name:
            raise BadRequest("app_name_required", "a Modal deployment names its app")
        existing = await session.get(Deployment, deployment_id)
        if existing is None:
            await _lock_app(session, environment_id, kind, app_name)
            now = utc_now()
            # Insert-on-conflict by id: a concurrent create of the same
            # client-minted id (a retry, possibly naming another app, so
            # under another app lock) is waited for on the primary key and
            # then found, rather than raising a unique-key error. The
            # generation is computed but only takes effect if this call
            # inserts.
            inserted = await session.scalar(
                pg_insert(Deployment)
                .values(
                    id=deployment_id,
                    environment_id=environment_id,
                    kind=kind,
                    app_name=app_name,
                    code_id=code_id,
                    image_id=image_id,
                    modal_app_id=modal_app_id,
                    # The registry's clock, not the CLI's (I4).
                    deployed_at=now,
                    generation=await _next_generation(
                        session, environment_id, kind, app_name
                    ),
                    created_at=now,
                )
                .on_conflict_do_nothing(index_elements=[Deployment.id])
                .returning(Deployment.id)
            )
            row = await session.scalar(
                select(Deployment)
                .where(Deployment.id == deployment_id)
                .execution_options(populate_existing=True)
            )
            assert row is not None
            if inserted is not None:
                return DeploymentState.of(row, created=True)
            existing = row
        _check_same(existing, environment_id, kind, app_name, code_id)
        current = await current_deployment_id(
            session, environment_id, existing.kind, existing.app_name
        )
        return DeploymentState.of(existing, is_current=current == existing.id)


def _check_same(
    row: Deployment,
    environment_id: UUID,
    kind: DeploymentKind,
    app_name: str,
    code_id: str,
) -> None:
    if row.environment_id != environment_id:
        raise Conflict("deployment_id_conflict", f"deployment id {row.id} is taken")
    sent = (kind, app_name, code_id)
    recorded = (row.kind, row.app_name, row.code_id)
    if sent != recorded:
        fields = [
            f
            for f, a, b in zip(("kind", "app_name", "code_id"), recorded, sent)
            if a != b
        ]
        raise Conflict(
            "deployment_id_conflict",
            f"deployment {row.id} is recorded with another {', '.join(fields)}",
            deployment_id=str(row.id),
            fields=fields,
        )


async def _local(
    session: AsyncSession,
    environment_id: UUID,
    *,
    deployment_id: UUID | None,
    app_name: str,
    code_id: str,
) -> DeploymentState:
    """Lookup-or-create by ``(environment, code_id)``, born activated."""
    kind = DeploymentKind.LOCAL
    # The code id first (it is the unique key), then the app (generation);
    # always in this order, so two lookups cannot deadlock.
    await _advisory_lock(session, f"deployment:{environment_id}:local-code:{code_id}")
    await _lock_app(session, environment_id, kind, app_name)
    existing = await session.scalar(
        select(Deployment).where(
            Deployment.environment_id == environment_id,
            Deployment.kind == kind,
            Deployment.code_id == code_id,
        )
    )
    created = False
    if existing is None:
        if deployment_id is not None and await session.get(Deployment, deployment_id):
            raise Conflict(
                "deployment_id_conflict", f"deployment id {deployment_id} is taken"
            )
        now = utc_now()
        existing = Deployment(
            id=deployment_id or generate_uuid7(),
            environment_id=environment_id,
            kind=kind,
            app_name=app_name,
            code_id=code_id,
            deployed_at=now,
            generation=await _next_generation(session, environment_id, kind, app_name),
            activated_at=now,
            created_at=now,
        )
        session.add(existing)
        await session.flush()
        created = True
    elif existing.app_name != app_name:
        raise Conflict(
            "local_deployment_conflict",
            f"code id {code_id} is recorded as a local deployment of app"
            f" {existing.app_name!r}",
            deployment_id=str(existing.id),
            app_name=existing.app_name,
        )
    current = await current_deployment_id(session, environment_id, kind, app_name)
    return DeploymentState.of(
        existing, is_current=current == existing.id, created=created
    )


async def activate_deployment(
    session: AsyncSession,
    environment_id: UUID,
    deployment_id: UUID,
    *,
    modal_app_id: str | None = None,
    image_id: str | None = None,
) -> DeploymentState:
    """Mark a deployment live after its deploy succeeded, recording what
    only the finished deploy knows (``modal_app_id``, ``image_id``).
    Idempotent by state: an activated row is returned unchanged. A given
    value fills a NULL column or must equal the recorded one (409
    ``deployment_activation_conflict`` otherwise, nothing written): a
    deployment's identity does not change after the fact.

    Takes the app lock exclusively (``kind`` and ``app_name`` never change,
    so they are read first): a seal or reactivation checking currency holds
    it shared to its commit, so the two serialise."""
    async with transaction(session):
        where = (
            Deployment.environment_id == environment_id,
            Deployment.id == deployment_id,
        )
        unknown = NotFound(
            "unknown_deployment",
            f"no deployment {deployment_id}",
            deployment_id=str(deployment_id),
        )
        app = (
            await session.execute(
                select(Deployment.kind, Deployment.app_name).where(*where)
            )
        ).first()
        if app is None:
            raise unknown
        await _lock_app(session, environment_id, app.kind, app.app_name)
        row = await session.scalar(
            select(Deployment)
            .where(*where)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row is None:
            raise unknown
        given = {"modal_app_id": modal_app_id, "image_id": image_id}
        clashing = sorted(
            column
            for column, value in given.items()
            if value is not None and getattr(row, column) not in (None, value)
        )
        if clashing:
            raise Conflict(
                "deployment_activation_conflict",
                "the activation names values other than those recorded",
                deployment_id=str(deployment_id),
                fields=clashing,
            )
        for column, value in given.items():
            if value is not None:
                setattr(row, column, value)
        if row.activated_at is None:
            row.activated_at = utc_now()
            await session.flush()
        current = await current_deployment_id(
            session, environment_id, row.kind, row.app_name
        )
        return DeploymentState.of(row, is_current=current == row.id)


async def get_deployment(
    session: AsyncSession, environment_id: UUID, deployment_id: UUID
) -> DeploymentState:
    """One deployment of the caller's environment, marked current or not."""
    row = await session.scalar(
        select(Deployment).where(
            Deployment.environment_id == environment_id,
            Deployment.id == deployment_id,
        )
    )
    if row is None:
        raise NotFound(
            "unknown_deployment",
            f"no deployment {deployment_id}",
            deployment_id=str(deployment_id),
        )
    current = await current_deployment_id(
        session, environment_id, row.kind, row.app_name
    )
    return DeploymentState.of(row, is_current=current == row.id)


async def get_deployments(
    session: AsyncSession, environment_id: UUID, deployment_ids: list[UUID]
) -> dict[UUID, DeploymentState]:
    """The named deployments, each marked current or not, in two queries
    total regardless of how many distinct deployments or apps are named —
    a caller resolving several deployments (a build's plans) should call
    this once rather than :func:`get_deployment` per id."""
    if not deployment_ids:
        return {}
    rows = (
        await session.scalars(
            select(Deployment).where(
                Deployment.environment_id == environment_id,
                Deployment.id.in_(set(deployment_ids)),
            )
        )
    ).all()
    found = {row.id: row for row in rows}
    for deployment_id in deployment_ids:
        if deployment_id not in found:
            raise NotFound(
                "unknown_deployment",
                f"no deployment {deployment_id}",
                deployment_id=str(deployment_id),
            )
    modal_app_names = {row.app_name for row in rows if row.kind is DeploymentKind.MODAL}
    current_ids: set[UUID] = set()
    if modal_app_names:
        current_ids = set(
            (
                await session.scalars(
                    select(Deployment.id)
                    .distinct(Deployment.kind, Deployment.app_name)
                    .where(
                        Deployment.environment_id == environment_id,
                        Deployment.kind == DeploymentKind.MODAL,
                        Deployment.app_name.in_(modal_app_names),
                        Deployment.activated_at.is_not(None),
                    )
                    .order_by(
                        Deployment.kind,
                        Deployment.app_name,
                        Deployment.generation.desc(),
                    )
                )
            ).all()
        )
    return {
        deployment_id: DeploymentState.of(
            found[deployment_id], is_current=deployment_id in current_ids
        )
        for deployment_id in deployment_ids
    }


async def list_deployments(
    session: AsyncSession,
    environment_id: UUID,
    *,
    kind: DeploymentKind | None = None,
    app_name: str | None = None,
    current_only: bool = False,
    limit: int = 100,
) -> list[DeploymentState]:
    """Deployments newest generation first per app, each marked current or
    not; ``current_only`` keeps one row per app."""
    current = (
        select(Deployment.id)
        .distinct(Deployment.kind, Deployment.app_name)
        .where(
            Deployment.environment_id == environment_id,
            Deployment.kind == DeploymentKind.MODAL,
            Deployment.activated_at.is_not(None),
        )
        .order_by(Deployment.kind, Deployment.app_name, Deployment.generation.desc())
    )
    if kind is not None:
        current = current.where(Deployment.kind == kind)
    if app_name is not None:
        current = current.where(Deployment.app_name == app_name)
    current_ids = set((await session.scalars(current)).all())
    if current_only:
        query = select(Deployment).where(Deployment.id.in_(current_ids))
    else:
        query = select(Deployment).where(Deployment.environment_id == environment_id)
        if kind is not None:
            query = query.where(Deployment.kind == kind)
        if app_name is not None:
            query = query.where(Deployment.app_name == app_name)
    rows = (
        await session.scalars(
            query.order_by(
                Deployment.kind, Deployment.app_name, Deployment.generation.desc()
            ).limit(limit)
        )
    ).all()
    return [DeploymentState.of(r, is_current=r.id in current_ids) for r in rows]
