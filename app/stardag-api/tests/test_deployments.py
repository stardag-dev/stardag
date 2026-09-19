"""Deployment records: which code versions of an app have been deployed.

A deployment is exactly the execution backend's — one code version of one
app, one live at a time — and a running build follows the current one. The
record is provenance plus "what is current", which is the newest row for
the app. See ``models/deployment.py``.
"""

from datetime import datetime, timezone

import pytest
from httpx import AsyncClient

DEPLOYMENTS = "/api/v1/deployments"


def _instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _record(app_name: str, code_id: str) -> dict:
    return {"app_name": app_name, "code_id": code_id}


@pytest.mark.asyncio
async def test_record_is_idempotent_and_refreshes_deployed_at(client: AsyncClient):
    """Deploying the same code again is the same deployment, current again."""
    first = await client.post(DEPLOYMENTS, json=_record("myapp", "abc"))
    assert first.status_code == 201, first.text
    assert first.json()["current"] is True
    again = await client.post(DEPLOYMENTS, json=_record("myapp", "abc"))
    assert again.status_code == 201, again.text
    assert again.json()["id"] == first.json()["id"]
    assert _instant(again.json()["deployed_at"]) >= _instant(
        first.json()["deployed_at"]
    )
    assert set(again.json()) == {
        "id",
        "environment_id",
        "app_name",
        "code_id",
        "deployed_at",
        "modal_app_id",
        "current",
    }


@pytest.mark.asyncio
async def test_the_backends_app_id_rides_along_and_follows_the_redeploy(
    client: AsyncClient,
):
    """Modal's app id is recorded when the deploy knows it, listed, and
    replaced on a re-record that supplies one."""
    first = await client.post(
        DEPLOYMENTS, json={**_record("myapp", "abc"), "modal_app_id": "ap-first"}
    )
    assert first.status_code == 201, first.text
    assert first.json()["modal_app_id"] == "ap-first"

    listed = (await client.get(DEPLOYMENTS, params={"app_name": "myapp"})).json()
    assert listed["deployments"][0]["modal_app_id"] == "ap-first"

    kept = await client.post(DEPLOYMENTS, json=_record("myapp", "abc"))
    assert kept.json()["id"] == first.json()["id"]
    assert kept.json()["modal_app_id"] == "ap-first"  # not supplied: kept

    replaced = await client.post(
        DEPLOYMENTS, json={**_record("myapp", "abc"), "modal_app_id": "ap-second"}
    )
    assert replaced.json()["modal_app_id"] == "ap-second"

    bare = await client.post(DEPLOYMENTS, json=_record("other", "zzz"))
    assert bare.json()["modal_app_id"] is None


@pytest.mark.asyncio
async def test_list_is_newest_first_and_marks_the_current_one_per_app(
    client: AsyncClient,
):
    await client.post(DEPLOYMENTS, json=_record("myapp", "111"))
    await client.post(DEPLOYMENTS, json=_record("myapp", "222"))
    await client.post(DEPLOYMENTS, json=_record("other", "333"))

    listed = (await client.get(DEPLOYMENTS)).json()["deployments"]
    assert [(d["app_name"], d["code_id"]) for d in listed] == [
        ("other", "333"),
        ("myapp", "222"),
        ("myapp", "111"),
    ]
    assert [d["current"] for d in listed] == [True, True, False]

    mine = (await client.get(DEPLOYMENTS, params={"app_name": "myapp"})).json()
    assert [d["code_id"] for d in mine["deployments"]] == ["222", "111"]
    assert [d["current"] for d in mine["deployments"]] == [True, False]


@pytest.mark.asyncio
async def test_redeploying_older_code_makes_it_current_again(client: AsyncClient):
    """A rollback is a deploy: the backend runs that code now, and the
    record says so rather than inventing a second row for it."""
    await client.post(DEPLOYMENTS, json=_record("myapp", "old"))
    await client.post(DEPLOYMENTS, json=_record("myapp", "new"))
    rolled_back = await client.post(DEPLOYMENTS, json=_record("myapp", "old"))
    assert rolled_back.status_code == 201, rolled_back.text

    listed = (await client.get(DEPLOYMENTS, params={"app_name": "myapp"})).json()
    assert [d["code_id"] for d in listed["deployments"]] == ["old", "new"]
    assert [d["current"] for d in listed["deployments"]] == [True, False]
    assert len(listed["deployments"]) == 2


@pytest.mark.asyncio
async def test_there_is_nothing_to_retire(client: AsyncClient):
    """Nothing is kept alive beside the current deployment, so there is no
    retire route and nothing for a collector to ask."""
    recorded = (await client.post(DEPLOYMENTS, json=_record("myapp", "abc"))).json()
    gone = await client.post(f"{DEPLOYMENTS}/{recorded['id']}/retire")
    assert gone.status_code in (404, 405), gone.text


# --- The record is one upsert, not a read and a write ----------------------


def test_the_upsert_compiles_for_both_dialects():
    """Two deploys of one code racing each other must both succeed, so the
    insert-or-refresh is decided by the database in one statement."""
    from sqlalchemy.dialects import postgresql, sqlite

    from stardag_api.routes.deployments import upsert_deployment_stmt

    values = {
        "id": "00000000-0000-7000-8000-000000000001",
        "environment_id": "00000000-0000-7000-8000-000000000002",
        "app_name": "myapp",
        "code_id": "abc",
        "deployed_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "modal_app_id": None,
        "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
    }
    pg = str(
        upsert_deployment_stmt("postgresql", values).compile(
            dialect=postgresql.dialect()
        )
    )
    assert "ON CONFLICT (environment_id, app_name, code_id) DO UPDATE" in pg
    assert "coalesce(excluded.modal_app_id, deployments.modal_app_id)" in pg
    lite = str(
        upsert_deployment_stmt("sqlite", values).compile(dialect=sqlite.dialect())
    )
    assert "ON CONFLICT (environment_id, app_name, code_id) DO UPDATE" in lite
    assert "excluded.deployed_at" in lite


@pytest.mark.asyncio
async def test_a_record_over_an_existing_row_refreshes_it_in_one_statement(
    client: AsyncClient,
):
    """The functional half of the same guarantee, on the shared session: a
    second record of the same code does not error on the unique constraint
    and comes back as one row with a newer ``deployed_at`` and the Modal app
    id kept when the second deploy did not name one."""
    first = (
        await client.post(
            DEPLOYMENTS, json={**_record("racer", "c0de"), "modal_app_id": "ap-1"}
        )
    ).json()
    again = await client.post(DEPLOYMENTS, json=_record("racer", "c0de"))
    assert again.status_code == 201, again.text
    body = again.json()
    assert body["id"] == first["id"]
    assert body["modal_app_id"] == "ap-1"
    assert _instant(body["deployed_at"]) >= _instant(first["deployed_at"])
    listed = (await client.get(DEPLOYMENTS, params={"app_name": "racer"})).json()
    assert len(listed["deployments"]) == 1
