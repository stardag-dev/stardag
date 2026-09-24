"""The ``/api/v2`` routes of the static path, end to end over HTTP.

The invariants are pinned at the service layer (``test_v2_registration``,
``test_v2_frontier``, ``test_v2_transitions``); this checks the routes are
thin and faithful: the environment comes from the credentials, bodies parse
into the service calls, and a service refusal surfaces as its status code
with the service's ``code``.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from httpx import AsyncClient

from stardag_api.schemas_v2 import RegistrationItem
from tests.v2_support import Harness, item, unexpanded


@pytest.fixture
def h(session_factory) -> Harness:
    return Harness(session_factory)


def wire(it: RegistrationItem) -> dict[str, Any]:
    return it.model_dump(mode="json")


async def _post(client: AsyncClient, path: str, body: Any = None) -> Any:
    response = await client.post(f"/api/v2{path}", json=body)
    assert response.status_code == 200, response.text
    return response.json()


async def test_static_path_over_http(client: AsyncClient, h: Harness):
    """Build, plan, members, seal, frontier, claim, renew, complete."""
    deployment = await h.new_deployment()
    leaf = item("Leaf")
    root = item("Root", upstreams=[leaf])

    build = await _post(client, "/builds", {"root_task_ids": [root.task_id]})
    assert build["status"] == "running"
    read = await client.get(f"/api/v2/builds/{build['id']}")
    assert read.status_code == 200 and read.json()["id"] == build["id"]

    plan = await _post(
        client,
        f"/builds/{build['id']}/plans",
        {
            "plan_id": str(uuid4()),
            "deployment_id": str(deployment),
            "settings": {"THREADS": "2"},
            "roots": [wire(unexpanded(root))],
        },
    )
    assert plan["created"] and plan["generation"] == 1

    frontier = (await client.get(f"/api/v2/builds/{build['id']}/frontier")).json()
    assert [m["task_id"] for m in frontier["discovery_jobs"]] == [root.task_id]

    members = await _post(
        client, f"/plans/{plan['id']}/members", {"items": [wire(leaf), wire(root)]}
    )
    assert members["tasks_created"] == 1 and members["edges_created"] == 1
    sealed = await _post(client, f"/plans/{plan['id']}/seal")
    assert sealed["sealed_at"] is not None

    frontier = (await client.get(f"/api/v2/builds/{build['id']}/frontier")).json()
    assert frontier["sealed"] and not frontier["plan_complete"]
    (runnable,) = frontier["runnable"]
    assert runnable["task_id"] == leaf.task_id and runnable["body"] == leaf.body

    execution = str(uuid4())
    base = f"/plans/{plan['id']}/members"
    started = await _post(
        client,
        f"{base}/{leaf.task_id}/start",
        {"execution_id": execution, "claim_ttl_seconds": 60},
    )
    assert started["applied"] and started["status"] == "running"
    renewed = await _post(
        client,
        f"/tasks/{leaf.task_id}/claim/renew",
        {"execution_id": execution, "claim_ttl_seconds": 600},
    )
    assert renewed["claim_expires_at"] > started["claim_expires_at"]
    done = await _post(
        client, f"{base}/{leaf.task_id}/complete", {"execution_id": execution}
    )
    assert done["status"] == "completed"

    root_execution = str(uuid4())
    await _post(
        client, f"{base}/{root.task_id}/start", {"execution_id": root_execution}
    )
    await _post(
        client, f"{base}/{root.task_id}/complete", {"execution_id": root_execution}
    )
    frontier = (await client.get(f"/api/v2/builds/{build['id']}/frontier")).json()
    assert frontier["plan_complete"]


async def test_refusals_carry_their_status_and_code(client: AsyncClient, h: Harness):
    deployment = await h.new_deployment()
    t = item("T")
    build = await _post(client, "/builds", {"root_task_ids": [t.task_id]})
    plan = await _post(
        client,
        f"/builds/{build['id']}/plans",
        {
            "plan_id": str(uuid4()),
            "deployment_id": str(deployment),
            "roots": [wire(unexpanded(t))],
        },
    )
    await _post(client, f"/plans/{plan['id']}/members", {"items": [wire(t)]})
    base = f"/api/v2/plans/{plan['id']}/members/{t.task_id}"

    first = str(uuid4())
    await _post(
        client,
        f"/plans/{plan['id']}/members/{t.task_id}/start",
        {"execution_id": first},
    )
    refused = await client.post(f"{base}/start", json={"execution_id": str(uuid4())})
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "task_already_running"

    stale = await client.post(f"{base}/complete", json={"execution_id": str(uuid4())})
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "unknown_execution"

    missing = await client.post(f"/api/v2/plans/{uuid4()}/seal")
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "unknown_plan"

    conflict = await client.post(
        f"/api/v2/plans/{plan['id']}/members",
        json={"items": [wire(item("T", extra={"other": True}))]},
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "instance_conflict"
    assert conflict.json()["detail"]["fields"] == ["other"]


async def test_the_environment_comes_from_the_credentials(
    client: AsyncClient, as_environment_b, h: Harness
):
    """A build is invisible from another environment, by construction."""
    build = await _post(client, "/builds", {"root_task_ids": ["r"]})
    with as_environment_b():
        response = await client.get(f"/api/v2/builds/{build['id']}/frontier")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "unknown_build"
