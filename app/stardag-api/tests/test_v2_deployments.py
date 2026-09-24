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
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

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


async def test_a_resent_create_reports_whether_the_row_is_current(h: Harness):
    """A re-sent (idempotent) Modal create reports ``is_current`` as of the
    read, as a local lookup does: true once the row is the app's highest
    activated generation, false once a later one is activated."""
    d = await _create(h)
    assert not (await _create(h, deployment_id=d.id)).is_current
    await _activate(h, d.id)
    again = await _create(h, deployment_id=d.id)
    assert (again.is_current, again.created) == (True, False)
    await _activate(h, (await _create(h, code_id="newer")).id)
    assert not (await _create(h, deployment_id=d.id)).is_current


@pytest.mark.parametrize("app_name", ["svc", "other"])
async def test_a_create_racing_one_for_the_same_id_finds_its_row(
    h: Harness, async_engine: AsyncEngine, app_name: str
):
    """Two creates of one client-minted id, the first uncommitted when the
    second runs (under another app lock if it names another app): the
    second waits on the primary key and finds the row — returned when the
    fields agree, 409 ``deployment_id_conflict`` when they differ — never a
    unique-key error."""
    deployment_id = uuid4()
    async with async_engine.connect() as first:
        await first.execute(
            text(
                "INSERT INTO deployment (id, environment_id, kind, app_name,"
                " code_id, deployed_at, generation) VALUES (:id, :env, 'modal',"
                " 'svc', 'abc', now(), 1)"
            ),
            {"id": deployment_id, "env": ENV},
        )
        second = asyncio.create_task(
            _create(h, deployment_id=deployment_id, app_name=app_name)
        )
        await asyncio.sleep(0.3)
        await first.commit()
    if app_name == "svc":
        found = await asyncio.wait_for(second, 10)
        assert (found.id, found.generation, found.created) == (deployment_id, 1, False)
    else:
        with pytest.raises(Conflict) as exc:
            await asyncio.wait_for(second, 10)
        assert exc.value.code == "deployment_id_conflict"
        assert exc.value.detail["fields"] == ["app_name"]


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


# --------------------------------------------------------------------------
# Currency checks serialise with activation (the app lock)
# --------------------------------------------------------------------------


def _app_lock_key(app_name: str) -> str:
    return f"deployment:{ENV}:modal:{app_name}"


async def test_a_seal_waits_for_an_activation_in_flight(
    h: Harness, async_engine: AsyncEngine
):
    """D3's activation holds the app lock exclusively until it commits; a
    seal of a plan under D2 checks currency under the same lock (shared),
    so it waits, then sees D3 current and is refused — it cannot seal D2's
    plan after D3's activation committed."""
    d2 = await h.new_deployment(app_name="svc")
    d3 = await h.new_deployment(app_name="svc", activated=False)
    root = item("Root")
    _, plan = await h.planned([root], [root], deployment_id=d2)

    async with async_engine.connect() as activation:
        await activation.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
            {"k": _app_lock_key("svc")},
        )
        await activation.execute(
            text("UPDATE deployment SET activated_at = now() WHERE id = :d"),
            {"d": d3},
        )
        sealing = asyncio.create_task(h.seal(plan.id))
        await asyncio.sleep(0.3)
        assert not sealing.done(), "the seal must wait for the activation"
        await activation.commit()

    with pytest.raises(Conflict) as exc:
        await asyncio.wait_for(sealing, 10)
    assert exc.value.code == "deployment_not_current"


async def test_an_activation_waits_for_a_currency_check_in_flight(
    h: Harness, async_engine: AsyncEngine
):
    """The other side: activation takes the app lock exclusively, so a
    seal holding it shared (to its commit) is waited for."""
    d3 = await h.new_deployment(app_name="svc", activated=False)
    async with async_engine.connect() as seal:
        await seal.execute(
            text("SELECT pg_advisory_xact_lock_shared(hashtextextended(:k, 0))"),
            {"k": _app_lock_key("svc")},
        )

        async def activate() -> deployments.DeploymentState:
            async with h.sf() as s:
                return await deployments.activate_deployment(s, ENV, d3)

        activating = asyncio.create_task(activate())
        await asyncio.sleep(0.3)
        assert not activating.done(), "the activation must wait for the seal"
        await seal.commit()
    state = await asyncio.wait_for(activating, 10)
    assert state.activated_at is not None and state.is_current


async def test_activation_records_what_the_finished_deploy_knows(
    client: AsyncClient,
):
    """``/activate {modal_app_id?, image_id?}``: the body is optional (the
    old empty call still activates); a given value fills a NULL column and
    must match a recorded one (409 ``deployment_activation_conflict``,
    nothing written); a re-sent identical activation is a no-op."""
    created = await client.post(
        "/api/v2/deployments",
        json={
            "id": str(uuid4()),
            "kind": "modal",
            "app_name": "svc",
            "code_id": "c1",
            "image_id": "im-1",
        },
    )
    assert created.status_code == 200, created.text
    path = f"/api/v2/deployments/{created.json()['id']}/activate"

    clash = await client.post(path, json={"image_id": "im-2"})
    assert clash.status_code == 409
    assert clash.json()["detail"]["code"] == "deployment_activation_conflict"
    assert clash.json()["detail"]["fields"] == ["image_id"]

    body = {"modal_app_id": "ap-123", "image_id": "im-1"}
    activated = await client.post(path, json=body)
    assert activated.status_code == 200, activated.text
    row = activated.json()
    assert row["activated_at"] is not None and row["is_current"]
    assert (row["modal_app_id"], row["image_id"]) == ("ap-123", "im-1")
    again = await client.post(path, json=body)
    assert again.json()["activated_at"] == row["activated_at"]
    assert (await client.post(path)).status_code == 200

    empty = await client.post(
        "/api/v2/deployments",
        json={"id": str(uuid4()), "kind": "modal", "app_name": "svc", "code_id": "c2"},
    )
    bare = await client.post(f"/api/v2/deployments/{empty.json()['id']}/activate")
    assert bare.status_code == 200 and bare.json()["modal_app_id"] is None
