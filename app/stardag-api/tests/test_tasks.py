"""Tests for task endpoints (environment-scoped queries)."""

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_list_tasks_empty(client: AsyncClient):
    """Test listing tasks when none exist."""
    response = await client.get("/api/v1/tasks")
    assert response.status_code == 200
    data = response.json()
    assert data["tasks"] == []
    assert data["total"] == 0


@pytest.mark.asyncio
async def test_get_task_not_found(client: AsyncClient):
    """Test that getting a non-existent task returns 404."""
    response = await client.get("/api/v1/tasks/nonexistent")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_register_task_with_output_uri(client: AsyncClient):
    """Test registering a task with output_uri."""
    # Create a build
    response = await client.post("/api/v1/builds", json={})
    build_id = response.json()["id"]

    # Register a task with output_uri
    task_data = {
        "task_id": "task-with-output-uri",
        "task_namespace": "test",
        "task_name": "TaskWithOutput",
        "task_data": {"param": "value"},
        "output_uri": "s3://bucket/path/to/output.json",
        "dependency_task_ids": [],
    }
    response = await client.post(f"/api/v1/builds/{build_id}/tasks", json=task_data)
    assert response.status_code == 201
    data = response.json()
    assert data["task_id"] == "task-with-output-uri"
    assert data["output_uri"] == "s3://bucket/path/to/output.json"


@pytest.mark.asyncio
async def test_register_task_without_output_uri(client: AsyncClient):
    """Test registering a task without output_uri (null)."""
    # Create a build
    response = await client.post("/api/v1/builds", json={})
    build_id = response.json()["id"]

    # Register a task without output_uri
    task_data = {
        "task_id": "task-without-output-uri",
        "task_namespace": "test",
        "task_name": "TaskWithoutOutput",
        "task_data": {},
    }
    response = await client.post(f"/api/v1/builds/{build_id}/tasks", json=task_data)
    assert response.status_code == 201
    data = response.json()
    assert data["task_id"] == "task-without-output-uri"
    assert data["output_uri"] is None


@pytest.mark.asyncio
async def test_get_task_includes_output_uri(client: AsyncClient):
    """Test that get_task endpoint includes output_uri."""
    # Create a build and register task
    response = await client.post("/api/v1/builds", json={})
    build_id = response.json()["id"]

    task_data = {
        "task_id": "task-get-with-uri",
        "task_namespace": "test",
        "task_name": "TestTask",
        "task_data": {},
        "output_uri": "/local/path/to/output.pkl",
    }
    await client.post(f"/api/v1/builds/{build_id}/tasks", json=task_data)

    # Get the task
    response = await client.get("/api/v1/tasks/task-get-with-uri")
    assert response.status_code == 200
    data = response.json()
    assert data["output_uri"] == "/local/path/to/output.pkl"


@pytest.mark.asyncio
async def test_list_tasks_includes_output_uri(client: AsyncClient):
    """Test that list_tasks endpoint includes output_uri."""
    # Create a build and register task
    response = await client.post("/api/v1/builds", json={})
    build_id = response.json()["id"]

    task_data = {
        "task_id": "task-list-with-uri",
        "task_namespace": "test",
        "task_name": "TestTask",
        "task_data": {},
        "output_uri": "/path/to/output.json",
    }
    await client.post(f"/api/v1/builds/{build_id}/tasks", json=task_data)

    # List tasks
    response = await client.get("/api/v1/tasks")
    assert response.status_code == 200
    data = response.json()
    assert len(data["tasks"]) == 1
    assert data["tasks"][0]["output_uri"] == "/path/to/output.json"


@pytest.mark.asyncio
async def test_get_task_metadata(client: AsyncClient):
    """Test getting task metadata for SDK task_get_metadata."""
    # Create a build and register task
    response = await client.post("/api/v1/builds", json={})
    build_id = response.json()["id"]

    task_data = {
        "task_id": "metadata-task-123",
        "task_namespace": "my.namespace",
        "task_name": "MetadataTask",
        "task_data": {"param1": "value1", "param2": 42},
        "version": "1.0.0",
        "output_uri": "s3://bucket/tasks/output.json",
    }
    await client.post(f"/api/v1/builds/{build_id}/tasks", json=task_data)

    # Get task metadata
    response = await client.get("/api/v1/tasks/metadata-task-123/metadata")
    assert response.status_code == 200
    data = response.json()

    # Verify the response matches TaskMetadata schema
    assert data["id"] == "metadata-task-123"
    assert data["body"] == {"param1": "value1", "param2": 42}
    assert data["name"] == "MetadataTask"
    assert data["namespace"] == "my.namespace"
    assert data["version"] == "1.0.0"
    assert data["output_uri"] == "s3://bucket/tasks/output.json"
    assert data["status"] == "pending"  # Not started yet
    assert data["registered_at"] is not None
    assert data["started_at"] is None
    assert data["completed_at"] is None
    assert data["error_message"] is None


@pytest.mark.asyncio
async def test_get_task_metadata_with_status(client: AsyncClient):
    """Test task metadata reflects status changes."""
    # Create a build and register task
    response = await client.post("/api/v1/builds", json={})
    build_id = response.json()["id"]

    task_data = {
        "task_id": "status-metadata-task",
        "task_namespace": "",
        "task_name": "StatusTask",
        "task_data": {},
        "output_uri": "/output/path.json",
    }
    await client.post(f"/api/v1/builds/{build_id}/tasks", json=task_data)

    # Start the task
    await client.post(f"/api/v1/builds/{build_id}/tasks/status-metadata-task/start")

    # Check metadata shows running
    response = await client.get("/api/v1/tasks/status-metadata-task/metadata")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "running"
    assert data["started_at"] is not None

    # Complete the task
    await client.post(f"/api/v1/builds/{build_id}/tasks/status-metadata-task/complete")

    # Check metadata shows completed
    response = await client.get("/api/v1/tasks/status-metadata-task/metadata")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "completed"
    assert data["completed_at"] is not None


@pytest.mark.asyncio
async def test_get_task_metadata_not_found(client: AsyncClient):
    """Test that getting metadata for non-existent task returns 404."""
    response = await client.get("/api/v1/tasks/nonexistent-task/metadata")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_get_task_metadata_empty_version(client: AsyncClient):
    """Test task metadata with no version returns empty string."""
    # Create a build and register task without version
    response = await client.post("/api/v1/builds", json={})
    build_id = response.json()["id"]

    task_data = {
        "task_id": "no-version-task",
        "task_namespace": "test",
        "task_name": "NoVersionTask",
        "task_data": {},
    }
    await client.post(f"/api/v1/builds/{build_id}/tasks", json=task_data)

    # Get task metadata
    response = await client.get("/api/v1/tasks/no-version-task/metadata")
    assert response.status_code == 200
    data = response.json()
    assert data["version"] == ""  # Empty string for missing version


# ---------------------------------------------------------------------------
# Enumerating tasks by status: "which tasks are holding an execution claim?"
# ---------------------------------------------------------------------------


def _register(task_id: str) -> dict:
    return {
        "task_id": task_id,
        "task_namespace": "ns",
        "task_name": "T",
        "task_data": {},
    }


async def _seed_statuses(client: AsyncClient) -> str:
    """One task per interesting status, all in one build. Returns build id."""
    build_id = (await client.post("/api/v1/builds", json={})).json()["id"]
    for task_id in ("st-pending", "st-running", "st-suspended", "st-done"):
        await client.post(f"/api/v1/builds/{build_id}/tasks", json=_register(task_id))
    await client.post(f"/api/v1/builds/{build_id}/tasks/st-running/start")
    await client.post(f"/api/v1/builds/{build_id}/tasks/st-suspended/start")
    await client.post(f"/api/v1/builds/{build_id}/tasks/st-suspended/suspend")
    await client.post(f"/api/v1/builds/{build_id}/tasks/st-done/start")
    await client.post(f"/api/v1/builds/{build_id}/tasks/st-done/complete")
    return build_id


@pytest.mark.asyncio
async def test_list_tasks_status_filter_single_and_multi(client: AsyncClient):
    """?status=running finds claim holders; the param repeats for a union."""
    await _seed_statuses(client)

    running = (await client.get("/api/v1/tasks", params={"status": "running"})).json()
    assert [t["task_id"] for t in running["tasks"]] == ["st-running"]
    assert running["total"] == 1

    # Repeated values union. Suspended matters too: an abandoned suspension
    # is equally unschedulable, and retryable since #208 A2.
    both = (
        await client.get(
            "/api/v1/tasks", params=[("status", "running"), ("status", "suspended")]
        )
    ).json()
    assert sorted(t["task_id"] for t in both["tasks"]) == ["st-running", "st-suspended"]
    assert both["total"] == 2

    # Unfiltered still returns everything (no behaviour change).
    assert (await client.get("/api/v1/tasks")).json()["total"] == 4


@pytest.mark.asyncio
async def test_list_tasks_status_filter_rejects_unknown_status(client: AsyncClient):
    response = await client.get("/api/v1/tasks", params={"status": "not-a-status"})
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_task_response_carries_latest_status_fields(client: AsyncClient):
    """latest_status / latest_status_at / latest_status_build_id — the last
    is the claim holder, and without it a blocked build cannot say who to
    ask."""
    build_id = await _seed_statuses(client)

    listed = (await client.get("/api/v1/tasks", params={"status": "running"})).json()
    task = listed["tasks"][0]
    assert task["latest_status"] == "running"
    assert task["latest_status_at"] is not None
    assert task["latest_status_build_id"] == build_id

    # Same on the single-task read and on registration.
    single = (await client.get("/api/v1/tasks/st-running")).json()
    assert single["latest_status"] == "running"
    assert single["latest_status_build_id"] == build_id

    registered = (
        await client.post(
            f"/api/v1/builds/{build_id}/tasks", json=_register("st-fresh")
        )
    ).json()
    assert registered["latest_status"] == "pending"
    assert registered["latest_status_build_id"] == build_id


@pytest.mark.asyncio
async def test_list_tasks_status_older_than_is_a_strict_boundary(
    client: AsyncClient, async_session
):
    """`status_older_than` is `<`, not `<=`: a task whose status landed
    exactly at the cutoff is not stale yet."""
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import select

    from stardag_api.models import Task

    await _seed_statuses(client)

    cutoff = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = (await async_session.execute(select(Task))).scalars().all()
    stamps = {
        "st-running": cutoff - timedelta(seconds=1),  # older → matches
        "st-suspended": cutoff,  # exactly at cutoff → does not
        "st-pending": cutoff + timedelta(seconds=1),  # newer → does not
    }
    for row in rows:
        if row.task_id in stamps:
            row.latest_status_at = stamps[row.task_id]
    await async_session.commit()

    stale = (
        await client.get(
            "/api/v1/tasks", params={"status_older_than": cutoff.isoformat()}
        )
    ).json()
    assert [t["task_id"] for t in stale["tasks"]] == ["st-running"]

    # Composes with the status filter.
    stale_suspended = (
        await client.get(
            "/api/v1/tasks",
            params={"status_older_than": cutoff.isoformat(), "status": "suspended"},
        )
    ).json()
    assert stale_suspended["tasks"] == []


@pytest.mark.asyncio
async def test_list_tasks_status_filter_orders_oldest_claim_first(
    client: AsyncClient, async_session
):
    """Triage order: the longest-held claim is the most likely abandoned."""
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import select

    from stardag_api.models import Task

    build_id = (await client.post("/api/v1/builds", json={})).json()["id"]
    for task_id in ("claim-new", "claim-old", "claim-mid"):
        await client.post(f"/api/v1/builds/{build_id}/tasks", json=_register(task_id))
        await client.post(f"/api/v1/builds/{build_id}/tasks/{task_id}/start")

    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    ages = {"claim-old": 0, "claim-mid": 10, "claim-new": 20}
    for row in (await async_session.execute(select(Task))).scalars().all():
        row.latest_status_at = base + timedelta(minutes=ages[row.task_id])
    await async_session.commit()

    listed = (await client.get("/api/v1/tasks", params={"status": "running"})).json()
    assert [t["task_id"] for t in listed["tasks"]] == [
        "claim-old",
        "claim-mid",
        "claim-new",
    ]

    # Unfiltered keeps the historical newest-registered-first order.
    unfiltered = (await client.get("/api/v1/tasks")).json()
    assert [t["task_id"] for t in unfiltered["tasks"]] == [
        "claim-mid",
        "claim-old",
        "claim-new",
    ]


@pytest.mark.asyncio
async def test_list_tasks_status_filter_environment_isolation(
    client: AsyncClient, as_environment_b
):
    """Claim enumeration is environment-scoped — a shared workspace must not
    leak another environment's running tasks."""
    await _seed_statuses(client)

    with as_environment_b():
        other = (await client.get("/api/v1/tasks", params={"status": "running"})).json()
        assert other == {"tasks": [], "total": 0, "page": 1, "page_size": 20}

    mine = (await client.get("/api/v1/tasks", params={"status": "running"})).json()
    assert [t["task_id"] for t in mine["tasks"]] == ["st-running"]


@pytest.mark.asyncio
async def test_list_tasks_status_filter_requires_auth(
    unauthenticated_client: AsyncClient,
):
    response = await unauthenticated_client.get(
        "/api/v1/tasks", params={"status": "running"}
    )
    assert response.status_code in (401, 403)
