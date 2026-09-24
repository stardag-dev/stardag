"""The reads the CLI and the UI build on, over HTTP (``api-pg`` tier).

Plans (one, a build's, a plan's graph), a task's executions across builds,
the claim on the task response, the build's failure reason, one deployment,
tasks by status, and build-list paging. Shapes follow the consumers where
they already code against a route (the UI's ``fetchPlanGraph`` →
``{plan_id, members, edges}``).
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine

from stardag_api.services.transitions import Transition
from tests.v2_support import Harness, item


@pytest.fixture
def h(session_factory) -> Harness:
    return Harness(session_factory)


async def _get(client: AsyncClient, path: str, **params: Any) -> Any:
    response = await client.get(f"/api/v2{path}", params=params)
    assert response.status_code == 200, response.text
    return response.json()


async def _status(client: AsyncClient, path: str, **params: Any) -> int:
    return (await client.get(f"/api/v2{path}", params=params)).status_code


async def test_a_plan_and_a_builds_plans(client: AsyncClient, h: Harness):
    """``GET /plans/{id}``: lifecycle timestamps, the scope with its
    deployment resolved, and member counts — excluded members counted apart
    from the status counts. ``GET /builds/{id}/plans``: every plan, newest
    generation first, a registering replacement included."""
    leaf = item("Leaf")
    a = item("A", upstreams=[leaf])
    build, plan = await h.planned([a], [leaf, a], seal=True)
    await h.run(plan.id, leaf)
    excluded = await client.post(
        f"/api/v2/plans/{plan.id}/members/{leaf.task_id}/exclude", json={}
    )
    assert excluded.status_code == 200, excluded.text

    detail = await _get(client, f"/plans/{plan.id}")
    assert detail["id"] == str(plan.id) and detail["build_id"] == str(build)
    assert detail["generation"] == 1 and detail["is_active"] is True
    assert detail["created_at"] and detail["activated_at"] and detail["sealed_at"]
    assert detail["superseded_at"] is None
    assert detail["deployment"]["id"] == detail["deployment_id"]
    assert detail["deployment"]["is_current"] is True
    assert (detail["member_count"], detail["root_count"]) == (2, 1)
    assert detail["excluded_count"] == 1
    assert detail["member_counts"] == {"pending": 1}

    replacement = await h.plan(build, await h.new_deployment(), [a])
    plans = await _get(client, f"/builds/{build}/plans")
    assert plans["build_id"] == str(build)
    assert [p["id"] for p in plans["plans"]] == [str(replacement.id), str(plan.id)]
    newest = plans["plans"][0]
    assert newest["generation"] == 2 and newest["is_active"] is False
    assert newest["activated_at"] is None and newest["root_count"] == 1

    assert await _status(client, f"/plans/{uuid4()}") == 404
    assert await _status(client, f"/builds/{uuid4()}/plans") == 404


async def test_a_builds_plans_resolves_deployments_in_a_fixed_number_of_queries(
    client: AsyncClient, h: Harness, async_engine: AsyncEngine
):
    """Each plan generation of a rollover names its own deployment, so a
    build with several plans names several distinct deployments.
    ``GET /builds/{id}/plans`` must resolve them together, not one
    ``get_deployment`` round trip per plan — the statement count for a
    build with many plans must not exceed that for a build with few."""

    async def statement_count(n_replacements: int) -> int:
        root = item(f"root-{n_replacements}")
        build, _ = await h.planned([root])
        for _ in range(n_replacements):
            await h.plan(build, await h.new_deployment(), [root])

        statements: list[str] = []

        def capture(conn, cursor, statement, *args):  # noqa: ARG001
            statements.append(statement)

        event.listen(async_engine.sync_engine, "before_cursor_execute", capture)
        try:
            response = await client.get(f"/api/v2/builds/{build}/plans")
        finally:
            event.remove(async_engine.sync_engine, "before_cursor_execute", capture)
        assert response.status_code == 200, response.text
        assert len(response.json()["plans"]) == n_replacements + 1
        return len(statements)

    few = await statement_count(1)
    many = await statement_count(5)
    assert many == few, (
        f"the query count grew with the number of plans/deployments ({few} -> {many})"
    )


async def test_the_plan_graph_holds_members_and_member_edges(
    client: AsyncClient, h: Harness
):
    """``GET /plans/{id}/graph``: every member with identity, status,
    admission and attempts; the instance edges between member instances,
    dynamic ones marked; upstreams admitted by the closure step included."""
    deployment = await h.new_deployment()
    leaf = item("Leaf")
    a = item("A", upstreams=[leaf], extra={"threads": 2})
    _, plan = await h.planned([a], [leaf, a], deployment_id=deployment, seal=True)
    await h.run(plan.id, leaf)
    execution = await h.start(plan.id, a)
    child = item("C")
    await h.yield_(plan.id, a, execution, [child])

    graph = await _get(client, f"/plans/{plan.id}/graph")
    assert graph["plan_id"] == str(plan.id)
    assert graph["deployment_id"] == str(deployment)
    members = {m["task_id"]: m for m in graph["members"]}
    assert set(members) == {leaf.task_id, a.task_id, child.task_id}
    assert members[a.task_id]["instance_hash"] == a.instance_hash
    assert members[a.task_id]["task_name"] == "A"
    assert members[a.task_id]["is_root"] is True
    assert members[a.task_id]["admitted_by"] == "root"
    assert members[a.task_id]["status"] == "suspended"
    assert members[a.task_id]["attempts"] == 1
    assert members[leaf.task_id]["admitted_by"] == "static"
    assert members[leaf.task_id]["status"] == "completed"
    assert members[child.task_id]["admitted_by"] == "dynamic"
    assert members[child.task_id]["attempts"] == 0
    assert members[child.task_id]["excluded_at"] is None
    instance = {tid: m["instance_id"] for tid, m in members.items()}
    edges = {
        (e["upstream_instance_id"], e["downstream_instance_id"], e["is_dynamic"])
        for e in graph["edges"]
    }
    assert edges == {
        (instance[leaf.task_id], instance[a.task_id], False),
        (instance[child.task_id], instance[a.task_id], True),
    }

    # Another build under the same scope names only the root; the closure
    # step admits what the scope already knows A needs.
    _, other = await h.planned([a], deployment_id=deployment)
    await h.closure(other.id)
    graph = await _get(client, f"/plans/{other.id}/graph")
    members = {m["task_id"]: m for m in graph["members"]}
    assert members[leaf.task_id]["admitted_by"] == "closure"
    assert members[child.task_id]["admitted_by"] == "closure"
    assert len(graph["edges"]) == 2
    # The other build ran nothing: attempts are the build's own.
    assert members[a.task_id]["attempts"] == 0

    assert await _status(client, f"/plans/{uuid4()}/graph") == 404


async def test_a_tasks_executions_across_builds_and_its_claim(
    client: AsyncClient, h: Harness
):
    """``GET /tasks/{id}/executions``: every execution of the completion,
    across builds, newest first, each with its build; ``include_ended=false``
    keeps the unended. The task response names the claim's holder."""
    t = item("T")
    first_build, first = await h.planned([t], [t])
    failed = await h.start(first.id, t)
    await h.transition(first.id, t, Transition.fail(failed, "boom"))
    await h.transition(first.id, t, Transition.retry())
    second_build, second = await h.planned([t], [t])
    live = await h.start(second.id, t)

    listed = await _get(client, f"/tasks/{t.task_id}/executions")
    assert listed["task_id"] == t.task_id
    rows = listed["executions"]
    assert [e["id"] for e in rows] == [str(live), str(failed)]
    assert [e["build_id"] for e in rows] == [str(second_build), str(first_build)]
    assert rows[1]["outcome"] == "failed" and rows[0]["ended_at"] is None
    assert all(e["in_current_plan"] for e in rows)
    unended = await _get(
        client, f"/tasks/{t.task_id}/executions", include_ended="false"
    )
    assert [e["id"] for e in unended["executions"]] == [str(live)]
    one = await _get(client, f"/tasks/{t.task_id}/executions", limit=1)
    assert len(one["executions"]) == 1

    task = await _get(client, f"/tasks/{t.task_id}")
    assert task["claim_plan_id"] == str(second.id)
    assert task["claim_build_id"] == str(second_build)
    assert task["execution_id"] == str(live)
    await h.transition(second.id, t, Transition.complete(live))
    task = await _get(client, f"/tasks/{t.task_id}")
    assert task["claim_plan_id"] is None and task["claim_build_id"] is None

    assert await _status(client, "/tasks/nope/executions") == 404


async def test_a_failed_build_carries_its_reason(client: AsyncClient, h: Harness):
    """``error_message`` on the build: the message of the ``BUILD_FAILED``
    that made it FAILED, on every response; gone once it is resumed."""
    t = item("T")
    build, _ = await h.planned([t], [t])
    assert (await _get(client, f"/builds/{build}"))["error_message"] is None
    failed = await client.post(
        f"/api/v2/builds/{build}/fail", json={"error_message": "boom"}
    )
    assert failed.status_code == 200 and failed.json()["error_message"] == "boom"
    assert (await _get(client, f"/builds/{build}"))["error_message"] == "boom"
    listed = await _get(client, "/builds", status="failed")
    assert [b["error_message"] for b in listed["builds"]] == ["boom"]
    resumed = await client.post(f"/api/v2/builds/{build}/resume")
    assert resumed.json()["build"]["error_message"] is None


async def test_one_deployment(client: AsyncClient, h: Harness):
    """``GET /deployments/{id}``, marked current or not."""
    older = await h.new_deployment(app_name="app")
    assert (await _get(client, f"/deployments/{older}"))["is_current"] is True
    newer = await h.new_deployment(app_name="app")
    read = await _get(client, f"/deployments/{older}")
    assert read["id"] == str(older) and read["kind"] == "modal"
    assert read["is_current"] is False and read["app_name"] == "app"
    assert "created" not in read  # a read is not a lookup-or-create
    assert (await _get(client, f"/deployments/{newer}"))["is_current"] is True
    assert await _status(client, f"/deployments/{uuid4()}") == 404


async def test_tasks_by_status_a_page_at_a_time(client: AsyncClient, h: Harness):
    """``GET /tasks?status=``: most recent status change first, paged by an
    opaque cursor; an undecodable cursor is 400."""
    tasks = [item(f"T{i}") for i in range(5)]
    _, plan = await h.planned(tasks, tasks)
    for t in tasks[:2]:
        await h.run(plan.id, t)

    done = await _get(client, "/tasks", status="completed")
    assert [t["task_id"] for t in done["tasks"]] == [
        tasks[1].task_id,
        tasks[0].task_id,
    ]
    assert done["next_cursor"] is None
    assert "instances" not in done["tasks"][0]

    seen: list[str] = []
    cursor = None
    for expected in (2, 1):
        params: dict[str, Any] = {"status": "pending", "limit": 2}
        if cursor:
            params["cursor"] = cursor
        page = await _get(client, "/tasks", **params)
        assert len(page["tasks"]) == expected
        seen += [t["task_id"] for t in page["tasks"]]
        cursor = page["next_cursor"]
    assert cursor is None
    assert sorted(seen) == sorted(t.task_id for t in tasks[2:])
    assert len((await _get(client, "/tasks"))["tasks"]) == 5
    assert await _status(client, "/tasks", cursor="not-a-cursor") == 400
    assert await _status(client, "/tasks", status="nope") == 422


async def test_build_list_pages_carry_the_total(client: AsyncClient, h: Harness):
    """``GET /builds``: ``limit`` and ``cursor`` page the listing without
    repeats or gaps, and ``total`` counts every build the filters match."""
    t = item("T")
    builds = [(await h.planned([t], [t]))[0] for _ in range(5)]
    await client.post(f"/api/v2/builds/{builds[0]}/cancel")

    seen: list[str] = []
    cursor = None
    for expected in (2, 2, 1):
        params: dict[str, Any] = {"limit": 2}
        if cursor:
            params["cursor"] = cursor
        page = await _get(client, "/builds", **params)
        assert page["total"] == 5
        assert len(page["builds"]) == expected
        seen += [b["id"] for b in page["builds"]]
        cursor = page["next_cursor"]
    assert cursor is None
    assert sorted(seen) == sorted(str(b) for b in builds)
    running = await _get(client, "/builds", status="running", limit=1)
    assert running["total"] == 4 and running["next_cursor"] is not None


async def test_build_list_idle_filter(client: AsyncClient, h: Harness):
    """``GET /builds?idle_for_seconds=``: running builds whose last lifecycle
    change is at least that old — a finished build is never idle — paged
    and counted like any other filter; any status but ``running`` is a
    contradiction (400), and the floor is a minute (422)."""
    t = item("T")
    stale, fresh, stale_done, stale_other = [
        (await h.planned([t], [t]))[0] for _ in range(4)
    ]
    await client.post(f"/api/v2/builds/{stale_done}/cancel")
    async with h.sf() as s:
        await s.execute(
            text(
                "UPDATE build SET last_active_at = now() - interval '2 hours'"
                " WHERE id IN (:a, :b, :c)"
            ),
            {"a": stale, "b": stale_done, "c": stale_other},
        )
        await s.commit()

    idle = await _get(client, "/builds", idle_for_seconds=3600)
    assert idle["total"] == 2
    assert {b["id"] for b in idle["builds"]} == {str(stale), str(stale_other)}
    assert str(fresh) not in {b["id"] for b in idle["builds"]}

    first = await _get(client, "/builds", idle_for_seconds=3600, limit=1)
    assert first["total"] == 2 and first["next_cursor"] is not None
    second = await _get(
        client, "/builds", idle_for_seconds=3600, limit=1, cursor=first["next_cursor"]
    )
    assert second["next_cursor"] is None
    assert {first["builds"][0]["id"], second["builds"][0]["id"]} == {
        str(stale),
        str(stale_other),
    }

    assert (await _get(client, "/builds", idle_for_seconds=3 * 3600))["total"] == 0
    running = await _get(client, "/builds", idle_for_seconds=3600, status="running")
    assert running["total"] == 2

    refused = await client.get(
        "/api/v2/builds", params={"idle_for_seconds": 3600, "status": "cancelled"}
    )
    assert refused.status_code == 400
    assert refused.json()["detail"]["code"] == "idle_requires_running"
    assert await _status(client, "/builds", idle_for_seconds=59) == 422


async def test_build_list_idle_filter_reflects_task_activity(
    client: AsyncClient, h: Harness
):
    """``last_active_at`` moves on task activity too, as in v1: a build
    that only looks idle by lifecycle-change staleness drops out of the
    filter once a task it holds transitions."""
    t = item("T")
    build, plan = await h.planned([t], [t])
    async with h.sf() as s:
        await s.execute(
            text(
                "UPDATE build SET last_active_at = now() - interval '2 hours'"
                " WHERE id = :b"
            ),
            {"b": build},
        )
        await s.commit()

    idle = await _get(client, "/builds", idle_for_seconds=3600)
    assert str(build) in {b["id"] for b in idle["builds"]}

    await h.start(plan.id, t)

    idle_after = await _get(client, "/builds", idle_for_seconds=3600)
    assert str(build) not in {b["id"] for b in idle_after["builds"]}
