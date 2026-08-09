"""Tests for the per-build reactive scheduler tick summary endpoints."""

import contextlib
from uuid import UUID

import pytest
from httpx import AsyncClient

from stardag_api.config import settings

FAKE_BUILD_ID = "00000000-0000-0000-0000-000000000099"


async def _create_build(client: AsyncClient) -> str:
    response = await client.post("/api/v1/builds", json={})
    assert response.status_code == 201
    return response.json()["id"]


def _summary(outcome: str = "lingered_out", **fields) -> dict:
    """A representative TickSummary payload."""
    return {
        "outcome": outcome,
        "terminal_status": None,
        "spawned": 0,
        "self_healed": 0,
        "failed_recorded": 0,
        "cancelled_refs": 0,
        "iterations": 1,
        "limit_denied": 0,
        "claim_denied": 0,
        "skipped": 0,
        **fields,
    }


@pytest.fixture
async def as_environment_b(async_engine):
    """Context manager switching the app's auth override to a SECOND
    environment in the same workspace (the tenancy boundary is the
    environment)."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from stardag_api.auth import SdkAuth, require_sdk_auth
    from stardag_api.main import app
    from stardag_api.models import Environment, User
    from tests.conftest import DEFAULT_USER_ID, DEFAULT_WORKSPACE_ID

    env_b_id = UUID("00000000-0000-0000-0000-00000000000b")
    session_maker = async_sessionmaker(async_engine, expire_on_commit=False)
    async with session_maker() as session:
        session.add(
            Environment(
                id=env_b_id,
                workspace_id=DEFAULT_WORKSPACE_ID,
                name="Environment B",
                slug="env-b",
            )
        )
        await session.commit()

    auth_b = SdkAuth(
        environment=Environment(
            id=env_b_id, workspace_id=DEFAULT_WORKSPACE_ID, name="Environment B"
        ),
        workspace_id=DEFAULT_WORKSPACE_ID,
        user=User(
            id=DEFAULT_USER_ID,
            external_id="default-local-user",
            email="default@localhost",
            display_name="Default User",
        ),
    )

    async def override_require_sdk_auth_b() -> SdkAuth:
        return auth_b

    @contextlib.contextmanager
    def _switch():
        previous = app.dependency_overrides[require_sdk_auth]
        app.dependency_overrides[require_sdk_auth] = override_require_sdk_auth_b
        try:
            yield
        finally:
            app.dependency_overrides[require_sdk_auth] = previous

    return _switch


@pytest.mark.asyncio
async def test_post_and_list_round_trip(client: AsyncClient):
    """A reported summary comes back verbatim, newest first."""
    build_id = await _create_build(client)

    for outcome in ("lease_held", "lingered_out", "terminal"):
        response = await client.post(
            f"/api/v1/builds/{build_id}/tick-summaries",
            json=_summary(outcome, spawned=2),
        )
        assert response.status_code == 201
        created = response.json()
        assert created["build_id"] == build_id
        assert created["outcome"] == outcome
        assert created["summary"]["spawned"] == 2
        # The blob is a faithful copy, outcome included.
        assert created["summary"]["outcome"] == outcome
        assert created["created_at"] is not None

    response = await client.get(f"/api/v1/builds/{build_id}/tick-summaries")
    assert response.status_code == 200
    data = response.json()
    assert data["build_id"] == build_id
    assert [s["outcome"] for s in data["summaries"]] == [
        "terminal",
        "lingered_out",
        "lease_held",
    ]
    assert data["summaries"][0]["summary"] == _summary("terminal", spawned=2)


@pytest.mark.asyncio
async def test_unknown_fields_are_preserved(client: AsyncClient):
    """Fields this server has never heard of survive the round trip —
    the SDK must be able to grow TickSummary without a server release."""
    build_id = await _create_build(client)
    payload = _summary(
        "lingered_out",
        blocked_by_other_build=3,
        blocked_details=[{"task_id": "abc", "build_id": "def"}],
        some_future_flag=True,
    )

    response = await client.post(
        f"/api/v1/builds/{build_id}/tick-summaries", json=payload
    )
    assert response.status_code == 201
    assert response.json()["summary"] == payload

    listed = (await client.get(f"/api/v1/builds/{build_id}/tick-summaries")).json()[
        "summaries"
    ]
    assert listed[0]["summary"] == payload


@pytest.mark.asyncio
async def test_outcome_is_required(client: AsyncClient):
    """outcome is the one promoted field, so it is the one required key."""
    build_id = await _create_build(client)
    response = await client.post(
        f"/api/v1/builds/{build_id}/tick-summaries", json={"spawned": 1}
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_retention_prunes_at_the_boundary(client: AsyncClient, monkeypatch):
    """Insert-time pruning keeps exactly the newest N, and the (N+1)th
    insert evicts the oldest — not the newest, and not more than one."""
    monkeypatch.setattr(settings, "max_tick_summaries_per_build", 5)
    build_id = await _create_build(client)

    # Exactly at the limit: nothing pruned yet.
    for i in range(5):
        response = await client.post(
            f"/api/v1/builds/{build_id}/tick-summaries",
            json=_summary("lease_held", iterations=i),
        )
        assert response.status_code == 201

    listed = (
        await client.get(
            f"/api/v1/builds/{build_id}/tick-summaries", params={"limit": 100}
        )
    ).json()["summaries"]
    assert [s["summary"]["iterations"] for s in listed] == [4, 3, 2, 1, 0]

    # One over: the oldest goes, the window stays at N.
    await client.post(
        f"/api/v1/builds/{build_id}/tick-summaries",
        json=_summary("lease_held", iterations=5),
    )
    listed = (
        await client.get(
            f"/api/v1/builds/{build_id}/tick-summaries", params={"limit": 100}
        )
    ).json()["summaries"]
    assert [s["summary"]["iterations"] for s in listed] == [5, 4, 3, 2, 1]

    # Well past the limit: still exactly N.
    for i in range(6, 20):
        await client.post(
            f"/api/v1/builds/{build_id}/tick-summaries",
            json=_summary("lease_held", iterations=i),
        )
    listed = (
        await client.get(
            f"/api/v1/builds/{build_id}/tick-summaries", params={"limit": 100}
        )
    ).json()["summaries"]
    assert [s["summary"]["iterations"] for s in listed] == [19, 18, 17, 16, 15]


@pytest.mark.asyncio
async def test_retention_is_scoped_per_build(client: AsyncClient, monkeypatch):
    """Pruning one build's trail never touches another's."""
    monkeypatch.setattr(settings, "max_tick_summaries_per_build", 2)
    build_a = await _create_build(client)
    build_b = await _create_build(client)

    await client.post(
        f"/api/v1/builds/{build_b}/tick-summaries", json=_summary("terminal")
    )
    for _ in range(5):
        await client.post(
            f"/api/v1/builds/{build_a}/tick-summaries", json=_summary("lease_held")
        )

    a_listed = (await client.get(f"/api/v1/builds/{build_a}/tick-summaries")).json()
    b_listed = (await client.get(f"/api/v1/builds/{build_b}/tick-summaries")).json()
    assert len(a_listed["summaries"]) == 2
    assert len(b_listed["summaries"]) == 1


@pytest.mark.asyncio
async def test_oversized_summary_rejected(client: AsyncClient):
    """A pathological summary is refused rather than stored."""
    build_id = await _create_build(client)
    response = await client.post(
        f"/api/v1/builds/{build_id}/tick-summaries",
        json=_summary("lingered_out", blocked_task_ids=["x" * 64] * 200),
    )
    assert response.status_code == 422
    assert "at most" in response.json()["detail"]

    # Nothing was persisted.
    listed = (await client.get(f"/api/v1/builds/{build_id}/tick-summaries")).json()
    assert listed["summaries"] == []


@pytest.mark.asyncio
async def test_long_outcome_rejected(client: AsyncClient):
    """outcome is a String(32) column — over-long values 422 rather than
    blowing up in the database."""
    build_id = await _create_build(client)
    response = await client.post(
        f"/api/v1/builds/{build_id}/tick-summaries", json=_summary("x" * 33)
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_limit_query_param(client: AsyncClient):
    """The read window is caller-controlled and hard-capped."""
    build_id = await _create_build(client)
    for i in range(5):
        await client.post(
            f"/api/v1/builds/{build_id}/tick-summaries",
            json=_summary("lease_held", iterations=i),
        )

    listed = (
        await client.get(
            f"/api/v1/builds/{build_id}/tick-summaries", params={"limit": 2}
        )
    ).json()["summaries"]
    assert [s["summary"]["iterations"] for s in listed] == [4, 3]

    assert (
        await client.get(
            f"/api/v1/builds/{build_id}/tick-summaries", params={"limit": 201}
        )
    ).status_code == 422
    assert (
        await client.get(
            f"/api/v1/builds/{build_id}/tick-summaries", params={"limit": 0}
        )
    ).status_code == 422


@pytest.mark.asyncio
async def test_unknown_build_is_404(client: AsyncClient):
    assert (
        await client.post(
            f"/api/v1/builds/{FAKE_BUILD_ID}/tick-summaries", json=_summary()
        )
    ).status_code == 404
    assert (
        await client.get(f"/api/v1/builds/{FAKE_BUILD_ID}/tick-summaries")
    ).status_code == 404


@pytest.mark.asyncio
async def test_environment_isolation(client: AsyncClient, as_environment_b):
    """Tick summaries are environment-scoped: another environment's auth
    can neither write nor read this build's trail."""
    build_id = await _create_build(client)
    await client.post(
        f"/api/v1/builds/{build_id}/tick-summaries", json=_summary("terminal")
    )

    with as_environment_b():
        assert (
            await client.post(
                f"/api/v1/builds/{build_id}/tick-summaries", json=_summary("lease_held")
            )
        ).status_code == 403
        assert (
            await client.get(f"/api/v1/builds/{build_id}/tick-summaries")
        ).status_code == 403

    # Untouched in the owning environment.
    listed = (await client.get(f"/api/v1/builds/{build_id}/tick-summaries")).json()
    assert [s["outcome"] for s in listed["summaries"]] == ["terminal"]


@pytest.mark.asyncio
async def test_empty_trail(client: AsyncClient):
    """A build that has never been ticked lists empty, not 404."""
    build_id = await _create_build(client)
    response = await client.get(f"/api/v1/builds/{build_id}/tick-summaries")
    assert response.status_code == 200
    assert response.json() == {"build_id": build_id, "summaries": []}


@pytest.mark.asyncio
async def test_round_trip_on_postgres(pg_client: AsyncClient):
    """Same round trip against real Postgres: exercises the JSONB column
    and the NOT IN (subquery LIMIT) prune on the production dialect."""
    build_id = await _create_build(pg_client)
    payload = _summary("lingered_out", blocked_by_other_build=2)
    for _ in range(3):
        assert (
            await pg_client.post(
                f"/api/v1/builds/{build_id}/tick-summaries", json=payload
            )
        ).status_code == 201

    listed = (await pg_client.get(f"/api/v1/builds/{build_id}/tick-summaries")).json()[
        "summaries"
    ]
    assert len(listed) == 3
    assert listed[0]["summary"] == payload
