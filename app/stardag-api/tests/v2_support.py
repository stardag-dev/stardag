"""Helpers for the v2 registry tests: scope rows, items, and row reads.

Deployments and builds are inserted directly: their lifecycle routes are
not part of the static path. Everything the tests assert *about* goes
through the services under test (``stardag_api.services.registration``,
``.frontier``, ``.transitions``).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from stardag_api.schemas_v2 import RegistrationItem
from stardag_api.services import frontier as frontier_service
from stardag_api.services import registration, transitions
from stardag_api.services.transitions import Transition
from tests.conftest import DEFAULT_ENVIRONMENT_ID

ENV = DEFAULT_ENVIRONMENT_ID


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _sha(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def item(
    name: str,
    *,
    params: Mapping[str, Any] | None = None,
    extra: Mapping[str, Any] | None = None,
    upstreams: Sequence[RegistrationItem] | None = (),
    observed_complete: bool = False,
    observed_at: datetime | None = None,
    version: str = "1",
    output_uri: str | None = None,
) -> RegistrationItem:
    """A registration item for task class ``name``.

    ``params`` are the completion-significant parameters (they go into
    ``task_id``); ``extra`` the non-significant ones (instance body only).
    ``upstreams=()`` is "expanded, no upstreams"; ``None`` is "not expanded".
    """
    identity = {"__namespace": "", "__name": name, "__version": version}
    identity.update(params or {})
    task_id = _sha(identity)
    body = {**identity, **(extra or {})}
    return RegistrationItem(
        task_id=task_id,
        task_name=name,
        version=version,
        output_uri=output_uri or f"memory://{task_id}",
        instance_hash=_sha(body),
        body=body,
        declared_upstreams=(
            None if upstreams is None else [u.instance_hash for u in upstreams]
        ),
        observed_complete=observed_complete,
        observed_at=observed_at or utcnow() - timedelta(seconds=1),
    )


def unexpanded(it: RegistrationItem, **update: Any) -> RegistrationItem:
    return it.model_copy(update={"declared_upstreams": None, **update})


def observed(
    it: RegistrationItem, complete: bool, at: datetime | None = None
) -> RegistrationItem:
    return it.model_copy(
        update={"observed_complete": complete, "observed_at": at or utcnow()}
    )


class Harness:
    """Calls the services with a fresh session each, as a route would."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self.sf = session_factory

    # -- scope rows (lifecycle routes are not under test) --------------------

    async def new_deployment(
        self, *, app_name: str = "app", kind: str = "modal", activated: bool = True
    ) -> UUID:
        deployment_id = uuid4()
        async with self.sf() as s:
            generation = await s.scalar(
                text(
                    "SELECT coalesce(max(generation), 0) + 1 FROM deployment"
                    " WHERE environment_id = :env AND kind = :kind"
                    " AND app_name = :app"
                ),
                {"env": ENV, "kind": kind, "app": app_name},
            )
            await s.execute(
                text(
                    "INSERT INTO deployment (id, environment_id, kind, app_name,"
                    " code_id, deployed_at, generation, activated_at)"
                    " VALUES (:id, :env, :kind, :app, :code, now(), :gen,"
                    " CASE WHEN :activated THEN now() END)"
                ),
                {
                    "id": deployment_id,
                    "env": ENV,
                    "kind": kind,
                    "app": app_name,
                    "code": str(deployment_id),
                    "gen": generation,
                    "activated": activated,
                },
            )
            await s.commit()
        return deployment_id

    async def new_build(self) -> UUID:
        build_id = uuid4()
        async with self.sf() as s:
            await s.execute(
                text(
                    "INSERT INTO build (id, environment_id, name, root_task_ids,"
                    " last_active_at, status) VALUES (:id, :env, 'b', '[]', now(),"
                    " 'running')"
                ),
                {"id": build_id, "env": ENV},
            )
            await s.commit()
        return build_id

    # -- services ------------------------------------------------------------

    async def plan(
        self,
        build_id: UUID,
        deployment_id: UUID,
        roots: Sequence[RegistrationItem],
        *,
        settings: Mapping[str, str] | None = None,
        plan_id: UUID | None = None,
    ) -> registration.PlanState:
        async with self.sf() as s:
            return await registration.create_plan(
                s,
                ENV,
                build_id=build_id,
                plan_id=plan_id or uuid4(),
                deployment_id=deployment_id,
                settings_body=settings or {},
                roots=[unexpanded(r) for r in roots],
            )

    async def register(
        self, plan_id: UUID, items: Sequence[RegistrationItem]
    ) -> registration.MembersResult:
        async with self.sf() as s:
            return await registration.register_members(s, ENV, plan_id, items)

    async def seal(self, plan_id: UUID) -> registration.PlanState:
        async with self.sf() as s:
            return await registration.seal_plan(s, ENV, plan_id)

    async def closure(self, plan_id: UUID) -> registration.ClosureResult:
        async with self.sf() as s:
            return await registration.closure(s, ENV, plan_id)

    async def frontier(self, build_id: UUID) -> frontier_service.Frontier:
        async with self.sf() as s:
            return await frontier_service.get_frontier(s, ENV, build_id)

    async def transition(
        self, plan_id: UUID, it: RegistrationItem, transition: Transition
    ) -> transitions.TransitionOutcome:
        async with self.sf() as s:
            return await transitions.apply_member_transition(
                s, ENV, plan_id=plan_id, task_id=it.task_id, transition=transition
            )

    async def start(
        self,
        plan_id: UUID,
        it: RegistrationItem,
        execution_id: UUID | None = None,
        **kwargs: Any,
    ) -> UUID:
        """A granted claiming start; returns its execution id."""
        execution_id = execution_id or uuid4()
        await self.transition(plan_id, it, Transition.start(execution_id, **kwargs))
        return execution_id

    async def run(self, plan_id: UUID, it: RegistrationItem) -> UUID:
        """Claim and complete ``it``; returns the execution id."""
        execution_id = await self.start(plan_id, it)
        await self.transition(plan_id, it, Transition.complete(execution_id))
        return execution_id

    async def renew(
        self, it: RegistrationItem, execution_id: UUID
    ) -> transitions.TransitionOutcome:
        async with self.sf() as s:
            return await transitions.renew_claim(
                s, ENV, task_id=it.task_id, execution_id=execution_id
            )

    # -- the one-root-plan shortcut -------------------------------------------

    async def planned(
        self,
        roots: Sequence[RegistrationItem],
        members: Sequence[RegistrationItem] = (),
        *,
        deployment_id: UUID | None = None,
        settings: Mapping[str, str] | None = None,
        seal: bool = False,
    ) -> tuple[UUID, registration.PlanState]:
        """A new build with a first (active) plan; returns (build, plan)."""
        deployment_id = deployment_id or await self.new_deployment()
        build_id = await self.new_build()
        plan = await self.plan(build_id, deployment_id, roots, settings=settings)
        if members:
            await self.register(plan.id, members)
        if seal:
            plan = await self.seal(plan.id)
        return build_id, plan

    # -- reads -----------------------------------------------------------------

    async def _rows(self, sql: str, **params: Any) -> list[RowMapping]:
        async with self.sf() as s:
            return list((await s.execute(text(sql), params)).mappings().all())

    async def task(self, it: RegistrationItem) -> RowMapping:
        rows = await self._rows(
            "SELECT * FROM task WHERE environment_id = :env AND task_id = :tid",
            env=ENV,
            tid=it.task_id,
        )
        assert len(rows) == 1, rows
        return rows[0]

    async def task_count(self, it: RegistrationItem) -> int:
        rows = await self._rows(
            "SELECT count(*) AS n FROM task WHERE task_id = :tid", tid=it.task_id
        )
        return rows[0]["n"]

    async def instance(
        self, deployment_id: UUID, it: RegistrationItem
    ) -> RowMapping | None:
        rows = await self._rows(
            "SELECT * FROM task_instance WHERE deployment_id = :d"
            " AND instance_hash = :h",
            d=deployment_id,
            h=it.instance_hash,
        )
        return rows[0] if rows else None

    async def members(self, plan_id: UUID) -> dict[str, RowMapping]:
        rows = await self._rows(
            "SELECT t.task_id, m.*, i.instance_hash FROM plan_member m"
            " JOIN task t ON t.id = m.task_pk"
            " JOIN task_instance i ON i.id = m.instance_id"
            " WHERE m.plan_id = :p",
            p=plan_id,
        )
        return {r["task_id"]: r for r in rows}

    async def events(
        self,
        it: RegistrationItem | None = None,
        *,
        types: Sequence[str] | None = None,
        plan_id: UUID | None = None,
        build_id: UUID | None = None,
    ) -> list[RowMapping]:
        sql = (
            "SELECT e.*, e.event_type::text AS type FROM event e"
            " LEFT JOIN task t ON t.id = e.task_pk WHERE e.environment_id = :env"
        )
        params: dict[str, Any] = {"env": ENV}
        if it is not None:
            sql += " AND t.task_id = :tid"
            params["tid"] = it.task_id
        if types is not None:
            sql += " AND e.event_type::text = ANY(:types)"
            params["types"] = list(types)
        if plan_id is not None:
            sql += " AND e.plan_id = :plan"
            params["plan"] = plan_id
        if build_id is not None:
            sql += " AND e.build_id = :build"
            params["build"] = build_id
        return await self._rows(sql + " ORDER BY e.created_at, e.id", **params)

    async def execution(self, execution_id: UUID) -> RowMapping:
        rows = await self._rows(
            "SELECT * FROM execution WHERE id = :id", id=execution_id
        )
        assert len(rows) == 1, rows
        return rows[0]

    async def build(self, build_id: UUID) -> RowMapping:
        rows = await self._rows("SELECT * FROM build WHERE id = :id", id=build_id)
        return rows[0]

    async def edges(self, deployment_id: UUID) -> set[tuple[str, str]]:
        rows = await self._rows(
            "SELECT d.instance_hash AS down, u.instance_hash AS up"
            " FROM task_instance_dependency e"
            " JOIN task_instance d ON d.id = e.downstream_instance_id"
            " JOIN task_instance u ON u.id = e.upstream_instance_id"
            " WHERE e.deployment_id = :d",
            d=deployment_id,
        )
        return {(r["down"], r["up"]) for r in rows}

    async def count(self, table: str) -> int:
        rows = await self._rows(f"SELECT count(*) AS n FROM {table}")
        return rows[0]["n"]

    # -- time travel -------------------------------------------------------------

    async def lapse_claim(self, it: RegistrationItem) -> None:
        async with self.sf() as s:
            await s.execute(
                text(
                    "UPDATE task SET claim_expires_at = now() - interval '1 second'"
                    " WHERE task_id = :tid"
                ),
                {"tid": it.task_id},
            )
            await s.commit()


def task_ids(members: Sequence[Any]) -> set[str]:
    """The task ids of a frontier list."""
    return {m.task_id for m in members}
