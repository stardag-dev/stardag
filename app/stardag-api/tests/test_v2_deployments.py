"""Deployments and settings: the deterministic scope (``api-pg`` tier).

Written from design.md, "The deterministic scope" and the ``deployment`` /
``settings`` entities. Each test names its scenario and the column or
constraint that decides it.
"""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient

from stardag_api.models import DeploymentKind
from stardag_api.services import deployments
from stardag_api.services.errors import BadRequest, Conflict
from tests.v2_support import ENV, Harness, item

MODAL = DeploymentKind.MODAL
LOCAL = DeploymentKind.LOCAL


@pytest.fixture
def h(session_factory) -> Harness:
    return Harness(session_factory)


async def _create(
    h: Harness,
    *,
    kind: DeploymentKind = MODAL,
    app_name: str | None = "svc",
    code_id: str = "abc",
    deployment_id: UUID | None = None,
) -> deployments.DeploymentState:
    async with h.sf() as s:
        return await deployments.create_deployment(
            s,
            ENV,
            deployment_id=deployment_id or (uuid4() if kind is MODAL else None),
            kind=kind,
            app_name=app_name,
            code_id=code_id,
        )


async def _activate(h: Harness, deployment_id: UUID) -> deployments.DeploymentState:
    async with h.sf() as s:
        return await deployments.activate_deployment(s, ENV, deployment_id)


async def _current(h: Harness, app_name: str = "svc") -> list[UUID]:
    async with h.sf() as s:
        rows = await deployments.list_deployments(
            s, ENV, app_name=app_name, current_only=True
        )
    return [r.id for r in rows]


async def test_s6_redeploy_of_unchanged_code_is_a_new_generation_and_scope(
    h: Harness,
):
    """S6 — a redeploy with no code change is a new deployment row, hence a
    new scope: the build re-plans under it and its instances are registered
    again (cheap: bodies identical)."""
    d1 = await _create(h)
    assert d1.created and d1.generation == 1 and d1.activated_at is None
    await _activate(h, d1.id)
    root = item("Root", upstreams=[])
    build, p1 = await h.planned([root], [root], deployment_id=d1.id)

    d2 = await _create(h)  # same code id
    assert d2.generation == 2 and d2.code_id == d1.code_id
    await _activate(h, d2.id)
    assert await _current(h) == [d2.id]

    p2 = await h.plan(build, d2.id, [root])
    assert p2.created and p2.id != p1.id and p2.activated_at is None
    registered = await h.register(p2.id, [root])
    assert registered.tasks_created == 0  # one completion, two scopes
    old_instance = await h.instance(d1.id, root)
    new_instance = await h.instance(d2.id, root)
    assert old_instance and new_instance and old_instance["id"] != new_instance["id"]
    assert (await h.seal(p2.id)).activated_at is not None


async def test_s9_local_and_modal_deployments_never_collide(h: Harness):
    """S9 — ``kind`` is in every lookup: a local code id equal to a Modal
    one is a different deployment. A local row is looked up or created by
    ``(environment, code_id)``, born activated, ``app_name`` ``"local"``."""
    modal = await _create(h, code_id="sha1")
    local = await _create(h, kind=LOCAL, app_name=None, code_id="sha1")
    assert local.id != modal.id and local.created
    assert (local.kind, local.app_name) == (LOCAL, "local")
    # Born activated, and never current: a local deployment is
    # authoritative for its own plans, never superseded by another.
    assert local.activated_at is not None and not local.is_current
    assert local.generation == 1

    again = await _create(h, kind=LOCAL, app_name=None, code_id="sha1")
    assert again.id == local.id and not again.created

    other = await _create(h, kind=LOCAL, app_name=None, code_id="sha2")
    assert other.generation == 2
    assert await _current(h, "local") == []
    assert await _current(h, "svc") == []  # the Modal one is not activated

    with pytest.raises(Conflict) as exc:
        await _create(h, kind=LOCAL, app_name="elsewhere", code_id="sha1")
    assert exc.value.code == "local_deployment_conflict"


async def test_s37_a_late_record_cannot_roll_the_app_back(h: Harness):
    """S37 — ``generation`` is assigned when a deploy *starts* (the create),
    so an activation that lands late does not make its older deployment
    current; re-sending a record is idempotent (same row, same
    generation)."""
    older = await _create(h)
    newer = await _create(h)
    await _activate(h, newer.id)
    late = await _activate(h, older.id)
    assert late.activated_at is not None and not late.is_current
    assert await _current(h) == [newer.id]

    resent = await _create(h, deployment_id=older.id)
    assert resent.id == older.id and resent.generation == older.generation
    assert not resent.created

    with pytest.raises(Conflict) as exc:
        await _create(h, deployment_id=older.id, code_id="other")
    assert exc.value.code == "deployment_id_conflict"
    assert exc.value.detail["fields"] == ["code_id"]


async def test_activation_is_idempotent_by_state(h: Harness):
    d = await _create(h)
    first = await _activate(h, d.id)
    again = await _activate(h, d.id)
    assert first.activated_at is not None
    assert again.activated_at == first.activated_at


async def test_concurrent_creates_get_distinct_generations(h: Harness):
    """Generation assignment is serialised per app: five concurrent deploy
    starts get five distinct generations, no unique-key error."""
    created = await asyncio.gather(*(_create(h) for _ in range(5)))
    assert sorted(d.generation for d in created) == [1, 2, 3, 4, 5]


async def test_a_modal_deployment_names_its_id_and_app(h: Harness):
    with pytest.raises(BadRequest) as exc:
        async with h.sf() as s:
            await deployments.create_deployment(
                s, ENV, deployment_id=None, kind=MODAL, app_name="svc", code_id="x"
            )
    assert exc.value.code == "deployment_id_required"


async def test_settings_are_validated_at_the_server(h: Harness):
    """A flat ``str → str`` body; ``STARDAG_*`` / ``MODAL_*`` keys are
    reserved for the framework (400 ``reserved_settings_key``), at the
    server as well as the trigger (S27)."""
    root = item("Root")
    deployment = await h.new_deployment()
    build = await h.new_build([root])
    for key in ("STARDAG_PLAN_ID", "MODAL_TOKEN_ID"):
        with pytest.raises(BadRequest) as exc:
            await h.plan(build, deployment, [root], settings={key: "x"})
        assert exc.value.code == "reserved_settings_key"
    with pytest.raises(BadRequest) as exc:
        deployments.validate_settings({"THREADS": 4})  # type: ignore[dict-item]
    assert exc.value.code == "invalid_settings"
    assert deployments.validate_settings({"THREADS": "4"}) == {"THREADS": "4"}


async def test_deployments_and_settings_over_http(client: AsyncClient, h: Harness):
    deployment_id = str(uuid4())
    body = {"id": deployment_id, "kind": "modal", "app_name": "svc", "code_id": "c1"}
    created = await client.post("/api/v2/deployments", json=body)
    assert created.status_code == 200 and created.json()["generation"] == 1
    activated = await client.post(f"/api/v2/deployments/{deployment_id}/activate")
    assert activated.json()["is_current"]
    listed = await client.get("/api/v2/deployments", params={"current": True})
    assert [d["id"] for d in listed.json()["deployments"]] == [deployment_id]

    local = await client.post(
        "/api/v2/deployments", json={"kind": "local", "code_id": "c1"}
    )
    assert local.json()["app_name"] == "local" and local.json()["activated_at"]

    root = item("Root")
    build = await client.post("/api/v2/builds", json={"root_task_ids": [root.task_id]})
    plan = await client.post(
        f"/api/v2/builds/{build.json()['id']}/plans",
        json={
            "plan_id": str(uuid4()),
            "deployment_id": deployment_id,
            "settings": {"THREADS": "2"},
            "roots": [
                root.model_copy(update={"declared_upstreams": None}).model_dump(
                    mode="json"
                )
            ],
        },
    )
    shash = plan.json()["settings_hash"]
    settings = await client.get(f"/api/v2/settings/{shash}")
    assert settings.json() == {"hash": shash, "body": {"THREADS": "2"}}
    reserved = await client.post(
        f"/api/v2/builds/{build.json()['id']}/plans",
        json={
            "plan_id": str(uuid4()),
            "deployment_id": deployment_id,
            "settings": {"STARDAG_X": "1"},
            "roots": [
                root.model_copy(update={"declared_upstreams": None}).model_dump(
                    mode="json"
                )
            ],
        },
    )
    assert reserved.status_code == 400
    assert reserved.json()["detail"]["code"] == "reserved_settings_key"
