"""The reads the SDK client calls beyond the frontier, and task artifacts,
over HTTP (``api-pg`` tier).

The shapes are the client's (``registry/_api_registry.py`` on the SDK side):
``GET /builds`` → ``{"builds": [...]}`` read by ``id``; ``GET
/plans/{id}/roots`` → ``{"roots": [FrontierMember]}``; ``GET /tasks/{id}``
→ the task's identity fields plus its instances.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from stardag_api.config import limits_settings
from tests.v2_support import ENV, Harness, item


@pytest.fixture
def h(session_factory) -> Harness:
    return Harness(session_factory)


async def _get(client: AsyncClient, path: str, **params: Any) -> Any:
    response = await client.get(f"/api/v2{path}", params=params)
    assert response.status_code == 200, response.text
    return response.json()


async def _reactive(client: AsyncClient, build_id: Any, app: str) -> None:
    response = await client.put(
        f"/api/v2/builds/{build_id}/reactive-meta", json={"app_name": app}
    )
    assert response.status_code == 200, response.text


async def test_list_builds_filters_by_status_and_reactive_app(
    client: AsyncClient, h: Harness
):
    """The watchdog's query: RUNNING builds owned by one reactive app, most
    recently active first."""
    t = item("T")
    owned_old, _ = await h.planned([t], [t])
    owned_new, _ = await h.planned([t], [t])
    other_app, _ = await h.planned([t], [t])
    finished, _ = await h.planned([t], [t])
    await h.new_build([t])  # not reactive
    for build in (owned_old, owned_new, finished):
        await _reactive(client, build, "app")
    await _reactive(client, other_app, "other")
    await client.post(f"/api/v2/builds/{finished}/cancel")
    await client.post(f"/api/v2/builds/{owned_new}/resume")  # bumps last_active_at

    listed = await _get(client, "/builds", status="running", reactive_app_name="app")
    assert [b["id"] for b in listed["builds"]] == [str(owned_new), str(owned_old)]
    assert listed["builds"][0]["reactive_app_name"] == "app"
    assert len((await _get(client, "/builds"))["builds"]) == 5
    assert len((await _get(client, "/builds", limit=2))["builds"]) == 2
    cancelled = await _get(client, "/builds", status="cancelled")
    assert [b["id"] for b in cancelled["builds"]] == [str(finished)]
    assert (await client.get("/api/v2/builds?status=nope")).status_code == 422


async def test_plan_roots_carry_the_root_bodies(client: AsyncClient, h: Harness):
    """Only the plan's roots, with the instance bodies a rolling-over tick
    rehydrates and re-hashes."""
    leaf = item("Leaf")
    a = item("A", upstreams=[leaf], extra={"threads": 4})
    b = item("B")
    _, plan = await h.planned([a, b], [leaf, a, b])
    roots = await _get(client, f"/plans/{plan.id}/roots")
    assert roots["plan_id"] == str(plan.id)
    by_id = {r["task_id"]: r for r in roots["roots"]}
    assert set(by_id) == {a.task_id, b.task_id}
    assert by_id[a.task_id]["body"] == a.body
    assert by_id[a.task_id]["instance_hash"] == a.instance_hash
    assert by_id[a.task_id]["is_root"] and by_id[a.task_id]["status"] == "pending"
    missing = await client.get(f"/api/v2/plans/{uuid4()}/roots")
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "unknown_plan"


async def test_get_task_returns_its_instances(client: AsyncClient, h: Harness):
    """A task holds no parameters: it comes with its instances in the
    caller's environment, each a body under one scope, newest first."""
    fast = item("T", extra={"mode": "fast"})
    slow = item("T", extra={"mode": "slow"})
    await h.planned([fast], [fast])
    await h.planned([slow], [slow])

    task = await _get(client, f"/tasks/{fast.task_id}")
    assert task["task_id"] == fast.task_id and task["task_name"] == "T"
    assert task["output_uri"] == fast.output_uri and task["status"] == "pending"
    assert [i["body"] for i in task["instances"]] == [slow.body, fast.body]
    assert task["instances"][0]["instance_hash"] == slow.instance_hash
    assert len((await _get(client, f"/tasks/{fast.task_id}", limit=1))["instances"])
    missing = await client.get("/api/v2/tasks/nope")
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "unknown_task"


async def test_artifacts_are_upserted_per_task_type_and_name(
    client: AsyncClient, h: Harness
):
    """Uploaded through the member, owned by the task: a re-upload of the
    same (type, name) replaces the body, a new name adds a row; a task that
    is not a member of the plan is 404 ``not_a_member``."""
    t = item("T")
    _, plan = await h.planned([t], [t])
    execution = await h.run(plan.id, t)
    path = f"/api/v2/plans/{plan.id}/members/{t.task_id}/artifacts"
    report = {"type": "markdown", "name": "report", "body": {"content": "# v1"}}

    first = await client.post(
        path, json={"execution_id": str(execution), "artifacts": [report]}
    )
    assert first.status_code == 200, first.text
    (row,) = first.json()["artifacts"]
    assert (row["artifact_type"], row["name"], row["body"]) == (
        "markdown",
        "report",
        {"content": "# v1"},
    )

    again = await client.post(
        path,
        json={
            "artifacts": [
                {**report, "body": {"content": "# v2"}},
                {"type": "json", "name": "metrics", "body": {"loss": 0.1}},
            ]
        },
    )
    assert again.status_code == 200, again.text
    listed = await _get(client, f"/tasks/{t.task_id}/artifacts")
    assert [(a["name"], a["body"]) for a in listed["artifacts"]] == [
        ("report", {"content": "# v2"}),
        ("metrics", {"loss": 0.1}),
    ]
    assert listed["artifacts"][0]["id"] == row["id"]

    stranger = item("Other")
    await h.planned([stranger], [stranger])
    refused = await client.post(
        f"/api/v2/plans/{plan.id}/members/{stranger.task_id}/artifacts",
        json={"artifacts": [report]},
    )
    assert refused.status_code == 404
    assert refused.json()["detail"]["code"] == "not_a_member"


async def test_artifact_guardrails(
    client: AsyncClient, h: Harness, monkeypatch: pytest.MonkeyPatch
):
    """v1's two guardrails, 429 as in v1: the body size, and the count per
    task — a replaced artifact does not count twice."""
    t = item("T")
    _, plan = await h.planned([t], [t])
    path = f"/api/v2/plans/{plan.id}/members/{t.task_id}/artifacts"
    monkeypatch.setattr(limits_settings, "max_artifacts_per_task", 2)
    monkeypatch.setattr(limits_settings, "max_artifact_body_bytes", 64)

    def art(name: str, size: int = 1) -> dict[str, Any]:
        return {"type": "json", "name": name, "body": {"x": "y" * size}}

    ok = await client.post(path, json={"artifacts": [art("a"), art("b")]})
    assert ok.status_code == 200, ok.text
    replaced = await client.post(path, json={"artifacts": [art("a", 2)]})
    assert replaced.status_code == 200, replaced.text
    over = await client.post(path, json={"artifacts": [art("c")]})
    assert over.status_code == 429
    assert over.json()["detail"]["code"] == "artifacts_per_task_limit"
    big = await client.post(path, json={"artifacts": [art("a", 100)]})
    assert big.status_code == 429
    assert big.json()["detail"]["code"] == "artifact_body_size_limit"


async def test_artifact_quota_serialises_on_the_task_row_lock(
    client: AsyncClient,
    h: Harness,
    async_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
):
    """Two uploads of different ``(type, name)`` pairs must not both pass
    the per-task quota from an unlocked count and together exceed it: the
    upload takes the task row lock (``FOR NO KEY UPDATE``) before counting,
    so a concurrent uncommitted upload is waited for, not raced."""
    t = item("T")
    _, plan = await h.planned([t], [t])
    path = f"/api/v2/plans/{plan.id}/members/{t.task_id}/artifacts"
    monkeypatch.setattr(limits_settings, "max_artifacts_per_task", 2)

    def art(name: str) -> dict[str, Any]:
        return {"type": "json", "name": name, "body": {"x": "y"}}

    task_pk = (await h.task(t))["id"]
    async with async_engine.connect() as other:
        # Session A: an in-flight upload of one artifact, not yet committed.
        await other.execute(
            text("SELECT id FROM task WHERE id = :id FOR NO KEY UPDATE"),
            {"id": task_pk},
        )
        await other.execute(
            text(
                "INSERT INTO task_artifact (id, environment_id, task_pk,"
                " artifact_type, name, body_json)"
                " VALUES (:id, :env, :task_pk, 'json', 'a', '{}'::jsonb)"
            ),
            {"id": uuid4(), "env": ENV, "task_pk": task_pk},
        )

        # Session B: two more artifacts, which combined with A's uncommitted
        # one exceed the quota of 2 — but an unlocked count reads 0 for A's
        # row and would wrongly let both through.
        uploading = asyncio.create_task(
            client.post(path, json={"artifacts": [art("b"), art("c")]})
        )
        await asyncio.sleep(0.3)
        assert not uploading.done(), "must wait on session A's task row lock"
        await other.commit()

    refused = await uploading
    assert refused.status_code == 429, refused.text
    assert refused.json()["detail"]["code"] == "artifacts_per_task_limit"
