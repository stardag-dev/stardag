"""Deployment records: which code version is deployed under which handle.

The record, not the app name, is the identity — the resolver reads the
newest live one for a family, and the garbage collector asks which ones no
running build still needs. See ``models/deployment.py``.
"""

from datetime import datetime, timezone

import pytest
from httpx import AsyncClient

DEPLOYMENTS = "/api/v1/deployments"
BUILDS = "/api/v1/builds"


def _instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _record(handle: str, code_id: str, family: str = "myapp") -> dict:
    return {"family": family, "handle": handle, "code_id": code_id}


@pytest.mark.asyncio
async def test_record_is_idempotent_on_handle_and_code_id(client: AsyncClient):
    first = await client.post(DEPLOYMENTS, json=_record("myapp--abc", "abc"))
    assert first.status_code == 201, first.text
    again = await client.post(DEPLOYMENTS, json=_record("myapp--abc", "abc"))
    assert again.status_code == 201, again.text
    assert again.json()["id"] == first.json()["id"]
    assert again.json()["running_builds"] == 0
    assert again.json()["retired_at"] is None


@pytest.mark.asyncio
async def test_a_handle_names_exactly_one_code_id(client: AsyncClient):
    """The handle is derived from the code id, so another code id under
    the same handle is a resolver bug, not a new version."""
    await client.post(DEPLOYMENTS, json=_record("myapp--abc", "abc"))
    clash = await client.post(DEPLOYMENTS, json=_record("myapp--abc", "def"))
    assert clash.status_code == 409, clash.text
    detail = clash.json()["detail"]
    assert detail["error_code"] == "deployment_handle_taken"
    assert detail["code_id"] == "abc"
    assert detail["requested_code_id"] == "def"


@pytest.mark.asyncio
async def test_list_is_newest_first_and_filters_by_family(client: AsyncClient):
    await client.post(DEPLOYMENTS, json=_record("myapp--111", "111"))
    await client.post(DEPLOYMENTS, json=_record("myapp--222", "222"))
    await client.post(DEPLOYMENTS, json=_record("other--333", "333", family="other"))

    listed = (await client.get(DEPLOYMENTS)).json()["deployments"]
    assert [d["handle"] for d in listed] == ["other--333", "myapp--222", "myapp--111"]

    mine = (await client.get(DEPLOYMENTS, params={"family": "myapp"})).json()
    assert [d["handle"] for d in mine["deployments"]] == ["myapp--222", "myapp--111"]


@pytest.mark.asyncio
async def test_retire_refuses_while_a_build_runs_on_it(client: AsyncClient):
    """Retiring records the bookkeeping for stopping the app, and a running
    build on the handle would be stranded — so it is refused unless forced.
    A retired deployment leaves the default listing and comes back when
    re-recorded (the app was deployed again)."""
    recorded = (
        await client.post(DEPLOYMENTS, json=_record("myapp--abc", "abc"))
    ).json()
    build_id = (await client.post(BUILDS, json={})).json()["id"]
    response = await client.put(
        f"{BUILDS}/{build_id}/reactive-meta", json={"app_name": "myapp--abc"}
    )
    assert response.status_code == 200, response.text

    listed = (await client.get(DEPLOYMENTS)).json()["deployments"]
    assert listed[0]["running_builds"] == 1

    refused = await client.post(f"{DEPLOYMENTS}/{recorded['id']}/retire")
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["error_code"] == "deployment_in_use"
    assert refused.json()["detail"]["running_builds"] == 1

    forced = await client.post(
        f"{DEPLOYMENTS}/{recorded['id']}/retire", params={"force": "true"}
    )
    assert forced.status_code == 200, forced.text
    assert forced.json()["retired_at"] is not None

    assert (await client.get(DEPLOYMENTS)).json()["deployments"] == []
    with_retired = (
        await client.get(DEPLOYMENTS, params={"include_retired": "true"})
    ).json()["deployments"]
    assert [d["handle"] for d in with_retired] == ["myapp--abc"]

    # Deployed again: the record is un-retired rather than duplicated.
    again = await client.post(DEPLOYMENTS, json=_record("myapp--abc", "abc"))
    assert again.status_code == 201, again.text
    assert again.json()["id"] == recorded["id"]
    assert again.json()["retired_at"] is None
    assert [
        d["handle"] for d in (await client.get(DEPLOYMENTS)).json()["deployments"]
    ] == ["myapp--abc"]


@pytest.mark.asyncio
async def test_a_resident_build_keeps_a_deployment_live(client: AsyncClient):
    """A non-reactive trigger never sets ``reactive_app_name``; the handle it
    runs on is in the build's executor metadata. It executes on the
    deployment all the same, so it counts, and retiring is refused."""
    recorded = (
        await client.post(DEPLOYMENTS, json=_record("myapp--abc", "abc"))
    ).json()
    started = await client.post(
        BUILDS,
        json={"executor_metadata": {"kind": "modal", "app_name": "myapp--abc"}},
    )
    assert started.status_code == 201, started.text
    assert started.json()["reactive_app_name"] is None

    listed = (await client.get(DEPLOYMENTS)).json()["deployments"]
    assert listed[0]["running_builds"] == 1

    refused = await client.post(f"{DEPLOYMENTS}/{recorded['id']}/retire")
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["running_builds"] == 1

    # A build on another app, and one on this app that has finished, count
    # for nothing.
    await client.post(
        BUILDS, json={"executor_metadata": {"kind": "modal", "app_name": "other"}}
    )
    done = await client.post(f"{BUILDS}/{started.json()['id']}/complete")
    assert done.status_code == 200, done.text
    listed = (await client.get(DEPLOYMENTS)).json()["deployments"]
    assert listed[0]["running_builds"] == 0


@pytest.mark.asyncio
async def test_retire_a_deployment_nobody_runs_on(client: AsyncClient):
    recorded = (
        await client.post(DEPLOYMENTS, json=_record("myapp--idle", "idle"))
    ).json()
    retired = await client.post(f"{DEPLOYMENTS}/{recorded['id']}/retire")
    assert retired.status_code == 200, retired.text
    assert retired.json()["retired_at"] is not None
    # Idempotent: the timestamp does not move. (Compared as instants — the
    # first response carries the offset the row was written with, the
    # re-read carries whatever the dialect kept.)
    again = await client.post(f"{DEPLOYMENTS}/{recorded['id']}/retire")
    assert again.status_code == 200
    assert _instant(again.json()["retired_at"]) == _instant(
        retired.json()["retired_at"]
    )


@pytest.mark.asyncio
async def test_retire_unknown_is_404(client: AsyncClient):
    response = await client.post(
        f"{DEPLOYMENTS}/00000000-0000-0000-0000-00000000dead/retire"
    )
    assert response.status_code == 404
