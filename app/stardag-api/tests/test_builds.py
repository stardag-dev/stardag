"""Tests for build management endpoints."""

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_create_build(client: AsyncClient):
    """Test creating a new run."""
    build_data = {
        "workspace_id": "default",
        "user": "default",
        "commit_hash": "abc123",
        "root_task_ids": [],
        "description": "Test build",
    }
    response = await client.post("/api/v1/builds", json=build_data)
    assert response.status_code == 201
    data = response.json()
    assert data["workspace_id"] == "default"
    assert data["status"] == "running"  # Build starts in buildning state
    assert data["name"] is not None  # Has memorable slug
    assert "-" in data["name"]  # Format: adjective-noun-number


@pytest.mark.asyncio
async def test_create_build_minimal(client: AsyncClient):
    """Test creating a build with minimal data (defaults)."""
    response = await client.post("/api/v1/builds", json={})
    assert response.status_code == 201
    data = response.json()
    assert data["workspace_id"] == "default"
    assert data["status"] == "running"


@pytest.mark.asyncio
async def test_get_build(client: AsyncClient):
    """Test retrieving a build by ID."""
    # Create a build first
    response = await client.post("/api/v1/builds", json={})
    build_id = response.json()["id"]

    # Get it back
    response = await client.get(f"/api/v1/builds/{build_id}")
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == build_id
    assert data["status"] == "running"


@pytest.mark.asyncio
async def test_get_build_not_found(client: AsyncClient):
    """Test that getting a non-existent run returns 404."""
    response = await client.get("/api/v1/builds/nonexistent-id")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_list_builds(client: AsyncClient):
    """Test listing runs with pagination."""
    # Create multiple runs
    for _ in range(3):
        await client.post("/api/v1/builds", json={})

    response = await client.get(
        "/api/v1/builds", params={"workspace_id": "default", "page_size": 2}
    )
    assert response.status_code == 200
    data = response.json()
    assert len(data["builds"]) == 2
    assert data["total"] == 3
    assert data["page"] == 1
    assert data["page_size"] == 2


@pytest.mark.asyncio
async def test_complete_build(client: AsyncClient):
    """Test completing a build."""
    # Create a build
    response = await client.post("/api/v1/builds", json={})
    build_id = response.json()["id"]

    # Complete it
    response = await client.post(f"/api/v1/builds/{build_id}/complete")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "completed"
    assert data["completed_at"] is not None


@pytest.mark.asyncio
async def test_fail_build(client: AsyncClient):
    """Test failing a build with error message."""
    # Create a build
    response = await client.post("/api/v1/builds", json={})
    build_id = response.json()["id"]

    # Fail it
    response = await client.post(
        f"/api/v1/builds/{build_id}/fail", params={"error_message": "Something broke"}
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "failed"
    assert data["completed_at"] is not None


@pytest.mark.asyncio
async def test_register_task_to_build(client: AsyncClient):
    """Test registering a task within a build."""
    # Create a build
    response = await client.post("/api/v1/builds", json={})
    build_id = response.json()["id"]

    # Register a task
    task_data = {
        "task_id": "test-task-123",
        "task_namespace": "test",
        "task_name": "TestTask",
        "task_data": {"param": "value"},
        "dependency_task_ids": [],
    }
    response = await client.post(f"/api/v1/builds/{build_id}/tasks", json=task_data)
    assert response.status_code == 201
    data = response.json()
    assert data["task_id"] == "test-task-123"
    assert data["task_name"] == "TestTask"


@pytest.mark.asyncio
async def test_start_task_in_build(client: AsyncClient):
    """Test starting a task within a build."""
    # Create a build
    response = await client.post("/api/v1/builds", json={})
    build_id = response.json()["id"]

    # Register a task
    task_data = {
        "task_id": "start-task-123",
        "task_namespace": "",
        "task_name": "TestTask",
        "task_data": {},
    }
    await client.post(f"/api/v1/builds/{build_id}/tasks", json=task_data)

    # Start the task
    response = await client.post(
        f"/api/v1/builds/{build_id}/tasks/start-task-123/start"
    )
    assert response.status_code == 200
    data = response.json()
    assert data["task_id"] == "start-task-123"
    assert data["status"] == "running"


@pytest.mark.asyncio
async def test_complete_task_in_build(client: AsyncClient):
    """Test completing a task within a build."""
    # Create a build
    response = await client.post("/api/v1/builds", json={})
    build_id = response.json()["id"]

    # Register and start a task
    task_data = {
        "task_id": "complete-task-123",
        "task_namespace": "",
        "task_name": "TestTask",
        "task_data": {},
    }
    await client.post(f"/api/v1/builds/{build_id}/tasks", json=task_data)
    await client.post(f"/api/v1/builds/{build_id}/tasks/complete-task-123/start")

    # Complete the task
    response = await client.post(
        f"/api/v1/builds/{build_id}/tasks/complete-task-123/complete"
    )
    assert response.status_code == 200
    data = response.json()
    assert data["task_id"] == "complete-task-123"
    assert data["status"] == "completed"


@pytest.mark.asyncio
async def test_fail_task_in_build(client: AsyncClient):
    """Test failing a task within a build."""
    # Create a build
    response = await client.post("/api/v1/builds", json={})
    build_id = response.json()["id"]

    # Register and start a task
    task_data = {
        "task_id": "fail-task-123",
        "task_namespace": "",
        "task_name": "TestTask",
        "task_data": {},
    }
    await client.post(f"/api/v1/builds/{build_id}/tasks", json=task_data)
    await client.post(f"/api/v1/builds/{build_id}/tasks/fail-task-123/start")

    # Fail the task
    response = await client.post(
        f"/api/v1/builds/{build_id}/tasks/fail-task-123/fail",
        params={"error_message": "Task error"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["task_id"] == "fail-task-123"
    assert data["status"] == "failed"


@pytest.mark.asyncio
async def test_list_tasks_in_build(client: AsyncClient):
    """Test listing all tasks in a build."""
    # Create a build
    response = await client.post("/api/v1/builds", json={})
    build_id = response.json()["id"]

    # Register multiple tasks
    for i in range(3):
        task_data = {
            "task_id": f"list-task-{i}",
            "task_namespace": "",
            "task_name": "TestTask",
            "task_data": {},
        }
        await client.post(f"/api/v1/builds/{build_id}/tasks", json=task_data)

    # List tasks in build
    response = await client.get(f"/api/v1/builds/{build_id}/tasks")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 3


@pytest.mark.asyncio
async def test_list_events_in_build(client: AsyncClient):
    """Test listing events for a build."""
    # Create a build
    response = await client.post("/api/v1/builds", json={})
    build_id = response.json()["id"]

    # Register and start a task to generate events
    task_data = {
        "task_id": "event-task",
        "task_namespace": "",
        "task_name": "TestTask",
        "task_data": {},
    }
    await client.post(f"/api/v1/builds/{build_id}/tasks", json=task_data)
    await client.post(f"/api/v1/builds/{build_id}/tasks/event-task/start")

    # List events
    response = await client.get(f"/api/v1/builds/{build_id}/events")
    assert response.status_code == 200
    data = response.json()
    # Should have: RUN_STARTED, TASK_PENDING, TASK_STARTED
    assert len(data) >= 3


@pytest.mark.asyncio
async def test_get_build_graph(client: AsyncClient):
    """Test getting the task graph for a build."""
    # Create a build
    response = await client.post("/api/v1/builds", json={})
    build_id = response.json()["id"]

    # Register tasks with dependency
    await client.post(
        f"/api/v1/builds/{build_id}/tasks",
        json={
            "task_id": "upstream-task",
            "task_namespace": "",
            "task_name": "UpstreamTask",
            "task_data": {},
        },
    )
    await client.post(
        f"/api/v1/builds/{build_id}/tasks",
        json={
            "task_id": "downstream-task",
            "task_namespace": "",
            "task_name": "DownstreamTask",
            "task_data": {},
            "dependency_task_ids": ["upstream-task"],
        },
    )

    # Get graph
    response = await client.get(f"/api/v1/builds/{build_id}/graph")
    assert response.status_code == 200
    data = response.json()
    assert len(data["nodes"]) == 2
    assert len(data["edges"]) == 1


@pytest.mark.asyncio
async def test_task_reuse_across_runs(client: AsyncClient):
    """Test that tasks are reused across builds (same task_id in same workspace)."""
    # Create first run and register task
    response = await client.post("/api/v1/builds", json={})
    build1_id = response.json()["id"]

    task_data = {
        "task_id": "shared-task",
        "task_namespace": "",
        "task_name": "SharedTask",
        "task_data": {"value": 1},
    }
    response = await client.post(f"/api/v1/builds/{build1_id}/tasks", json=task_data)
    task_db_id_1 = response.json()["id"]

    # Create second run and register same task
    response = await client.post("/api/v1/builds", json={})
    build2_id = response.json()["id"]

    response = await client.post(f"/api/v1/builds/{build2_id}/tasks", json=task_data)
    task_db_id_2 = response.json()["id"]

    # Task should be reused (same database ID)
    assert task_db_id_1 == task_db_id_2

    # But each build should track its own status via events
    # Complete in build1
    await client.post(f"/api/v1/builds/{build1_id}/tasks/shared-task/start")
    await client.post(f"/api/v1/builds/{build1_id}/tasks/shared-task/complete")

    # Task status in build1 should be completed
    response = await client.get(f"/api/v1/builds/{build1_id}/tasks")
    build1_tasks = response.json()
    assert any(
        t["task_id"] == "shared-task" and t["status"] == "completed"
        for t in build1_tasks
    )

    # Task status in build2 should still be pending
    response = await client.get(f"/api/v1/builds/{build2_id}/tasks")
    build2_tasks = response.json()
    assert any(
        t["task_id"] == "shared-task" and t["status"] == "pending" for t in build2_tasks
    )
