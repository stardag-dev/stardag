"""Tests for run management endpoints."""

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_create_run(client: AsyncClient):
    """Test creating a new run."""
    run_data = {
        "workspace_id": "default",
        "user": "default",
        "commit_hash": "abc123",
        "root_task_ids": [],
        "description": "Test run",
    }
    response = await client.post("/api/v1/runs", json=run_data)
    assert response.status_code == 201
    data = response.json()
    assert data["workspace_id"] == "default"
    assert data["status"] == "running"  # Run starts in running state
    assert data["name"] is not None  # Has memorable slug
    assert "-" in data["name"]  # Format: adjective-noun-number


@pytest.mark.asyncio
async def test_create_run_minimal(client: AsyncClient):
    """Test creating a run with minimal data (defaults)."""
    response = await client.post("/api/v1/runs", json={})
    assert response.status_code == 201
    data = response.json()
    assert data["workspace_id"] == "default"
    assert data["status"] == "running"


@pytest.mark.asyncio
async def test_get_run(client: AsyncClient):
    """Test retrieving a run by ID."""
    # Create a run first
    response = await client.post("/api/v1/runs", json={})
    run_id = response.json()["id"]

    # Get it back
    response = await client.get(f"/api/v1/runs/{run_id}")
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == run_id
    assert data["status"] == "running"


@pytest.mark.asyncio
async def test_get_run_not_found(client: AsyncClient):
    """Test that getting a non-existent run returns 404."""
    response = await client.get("/api/v1/runs/nonexistent-id")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_list_runs(client: AsyncClient):
    """Test listing runs with pagination."""
    # Create multiple runs
    for _ in range(3):
        await client.post("/api/v1/runs", json={})

    response = await client.get("/api/v1/runs", params={"page_size": 2})
    assert response.status_code == 200
    data = response.json()
    assert len(data["runs"]) == 2
    assert data["total"] == 3
    assert data["page"] == 1
    assert data["page_size"] == 2


@pytest.mark.asyncio
async def test_complete_run(client: AsyncClient):
    """Test completing a run."""
    # Create a run
    response = await client.post("/api/v1/runs", json={})
    run_id = response.json()["id"]

    # Complete it
    response = await client.post(f"/api/v1/runs/{run_id}/complete")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "completed"
    assert data["completed_at"] is not None


@pytest.mark.asyncio
async def test_fail_run(client: AsyncClient):
    """Test failing a run with error message."""
    # Create a run
    response = await client.post("/api/v1/runs", json={})
    run_id = response.json()["id"]

    # Fail it
    response = await client.post(
        f"/api/v1/runs/{run_id}/fail", params={"error_message": "Something broke"}
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "failed"
    assert data["completed_at"] is not None


@pytest.mark.asyncio
async def test_register_task_to_run(client: AsyncClient):
    """Test registering a task within a run."""
    # Create a run
    response = await client.post("/api/v1/runs", json={})
    run_id = response.json()["id"]

    # Register a task
    task_data = {
        "task_id": "test-task-123",
        "task_namespace": "test",
        "task_family": "TestTask",
        "task_data": {"param": "value"},
        "dependency_task_ids": [],
    }
    response = await client.post(f"/api/v1/runs/{run_id}/tasks", json=task_data)
    assert response.status_code == 201
    data = response.json()
    assert data["task_id"] == "test-task-123"
    assert data["task_family"] == "TestTask"


@pytest.mark.asyncio
async def test_start_task_in_run(client: AsyncClient):
    """Test starting a task within a run."""
    # Create a run
    response = await client.post("/api/v1/runs", json={})
    run_id = response.json()["id"]

    # Register a task
    task_data = {
        "task_id": "start-task-123",
        "task_namespace": "",
        "task_family": "TestTask",
        "task_data": {},
    }
    await client.post(f"/api/v1/runs/{run_id}/tasks", json=task_data)

    # Start the task
    response = await client.post(f"/api/v1/runs/{run_id}/tasks/start-task-123/start")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "running"
    assert data["started_at"] is not None


@pytest.mark.asyncio
async def test_complete_task_in_run(client: AsyncClient):
    """Test completing a task within a run."""
    # Create a run
    response = await client.post("/api/v1/runs", json={})
    run_id = response.json()["id"]

    # Register and start a task
    task_data = {
        "task_id": "complete-task-123",
        "task_namespace": "",
        "task_family": "TestTask",
        "task_data": {},
    }
    await client.post(f"/api/v1/runs/{run_id}/tasks", json=task_data)
    await client.post(f"/api/v1/runs/{run_id}/tasks/complete-task-123/start")

    # Complete the task
    response = await client.post(
        f"/api/v1/runs/{run_id}/tasks/complete-task-123/complete"
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "completed"
    assert data["completed_at"] is not None


@pytest.mark.asyncio
async def test_fail_task_in_run(client: AsyncClient):
    """Test failing a task within a run."""
    # Create a run
    response = await client.post("/api/v1/runs", json={})
    run_id = response.json()["id"]

    # Register and start a task
    task_data = {
        "task_id": "fail-task-123",
        "task_namespace": "",
        "task_family": "TestTask",
        "task_data": {},
    }
    await client.post(f"/api/v1/runs/{run_id}/tasks", json=task_data)
    await client.post(f"/api/v1/runs/{run_id}/tasks/fail-task-123/start")

    # Fail the task
    response = await client.post(
        f"/api/v1/runs/{run_id}/tasks/fail-task-123/fail",
        params={"error_message": "Task error"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "failed"
    assert data["error_message"] == "Task error"


@pytest.mark.asyncio
async def test_list_tasks_in_run(client: AsyncClient):
    """Test listing all tasks in a run."""
    # Create a run
    response = await client.post("/api/v1/runs", json={})
    run_id = response.json()["id"]

    # Register multiple tasks
    for i in range(3):
        task_data = {
            "task_id": f"list-task-{i}",
            "task_namespace": "",
            "task_family": "TestTask",
            "task_data": {},
        }
        await client.post(f"/api/v1/runs/{run_id}/tasks", json=task_data)

    # List tasks in run
    response = await client.get(f"/api/v1/runs/{run_id}/tasks")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 3


@pytest.mark.asyncio
async def test_list_events_in_run(client: AsyncClient):
    """Test listing events for a run."""
    # Create a run
    response = await client.post("/api/v1/runs", json={})
    run_id = response.json()["id"]

    # Register and start a task to generate events
    task_data = {
        "task_id": "event-task",
        "task_namespace": "",
        "task_family": "TestTask",
        "task_data": {},
    }
    await client.post(f"/api/v1/runs/{run_id}/tasks", json=task_data)
    await client.post(f"/api/v1/runs/{run_id}/tasks/event-task/start")

    # List events
    response = await client.get(f"/api/v1/runs/{run_id}/events")
    assert response.status_code == 200
    data = response.json()
    # Should have: RUN_STARTED, TASK_PENDING, TASK_STARTED
    assert len(data) >= 3


@pytest.mark.asyncio
async def test_get_run_graph(client: AsyncClient):
    """Test getting the task graph for a run."""
    # Create a run
    response = await client.post("/api/v1/runs", json={})
    run_id = response.json()["id"]

    # Register tasks with dependency
    await client.post(
        f"/api/v1/runs/{run_id}/tasks",
        json={
            "task_id": "upstream-task",
            "task_namespace": "",
            "task_family": "UpstreamTask",
            "task_data": {},
        },
    )
    await client.post(
        f"/api/v1/runs/{run_id}/tasks",
        json={
            "task_id": "downstream-task",
            "task_namespace": "",
            "task_family": "DownstreamTask",
            "task_data": {},
            "dependency_task_ids": ["upstream-task"],
        },
    )

    # Get graph
    response = await client.get(f"/api/v1/runs/{run_id}/graph")
    assert response.status_code == 200
    data = response.json()
    assert len(data["nodes"]) == 2
    assert len(data["edges"]) == 1


@pytest.mark.asyncio
async def test_task_reuse_across_runs(client: AsyncClient):
    """Test that tasks are reused across runs (same task_id in same workspace)."""
    # Create first run and register task
    response = await client.post("/api/v1/runs", json={})
    run1_id = response.json()["id"]

    task_data = {
        "task_id": "shared-task",
        "task_namespace": "",
        "task_family": "SharedTask",
        "task_data": {"value": 1},
    }
    response = await client.post(f"/api/v1/runs/{run1_id}/tasks", json=task_data)
    task_db_id_1 = response.json()["id"]

    # Create second run and register same task
    response = await client.post("/api/v1/runs", json={})
    run2_id = response.json()["id"]

    response = await client.post(f"/api/v1/runs/{run2_id}/tasks", json=task_data)
    task_db_id_2 = response.json()["id"]

    # Task should be reused (same database ID)
    assert task_db_id_1 == task_db_id_2

    # But each run should track its own status via events
    # Complete in run1
    await client.post(f"/api/v1/runs/{run1_id}/tasks/shared-task/start")
    await client.post(f"/api/v1/runs/{run1_id}/tasks/shared-task/complete")

    # Task status in run1 should be completed
    response = await client.get(f"/api/v1/runs/{run1_id}/tasks")
    run1_tasks = response.json()
    assert any(
        t["task_id"] == "shared-task" and t["status"] == "completed" for t in run1_tasks
    )

    # Task status in run2 should still be pending
    response = await client.get(f"/api/v1/runs/{run2_id}/tasks")
    run2_tasks = response.json()
    assert any(
        t["task_id"] == "shared-task" and t["status"] == "pending" for t in run2_tasks
    )
