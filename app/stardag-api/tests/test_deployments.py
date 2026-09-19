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
