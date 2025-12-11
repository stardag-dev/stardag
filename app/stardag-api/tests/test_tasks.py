"""Tests for task CRUD endpoints."""

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_create_task(client: AsyncClient):
    """Test creating a new task."""
    task_data = {
        "task_id": "test-task-123",
        "task_family": "TestTask",
        "task_data": {"param": "value"},
        "user": "test-user",
        "commit_hash": "abc123",
        "dependency_ids": [],
    }
    response = await client.post("/api/v1/tasks", json=task_data)
    assert response.status_code == 201
    data = response.json()
    assert data["task_id"] == "test-task-123"
    assert data["task_family"] == "TestTask"
    assert data["status"] == "pending"
    assert data["user"] == "test-user"


@pytest.mark.asyncio
async def test_create_duplicate_task(client: AsyncClient):
    """Test that creating a duplicate task returns 409."""
    task_data = {
        "task_id": "duplicate-task",
        "task_family": "TestTask",
        "task_data": {},
        "user": "test-user",
        "commit_hash": "abc123",
    }
    response = await client.post("/api/v1/tasks", json=task_data)
    assert response.status_code == 201

    response = await client.post("/api/v1/tasks", json=task_data)
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_get_task(client: AsyncClient):
    """Test retrieving a task by ID."""
    task_data = {
        "task_id": "get-task-123",
        "task_family": "TestTask",
        "task_data": {"key": "value"},
        "user": "test-user",
        "commit_hash": "abc123",
    }
    await client.post("/api/v1/tasks", json=task_data)

    response = await client.get("/api/v1/tasks/get-task-123")
    assert response.status_code == 200
    data = response.json()
    assert data["task_id"] == "get-task-123"
    assert data["task_data"] == {"key": "value"}


@pytest.mark.asyncio
async def test_get_task_not_found(client: AsyncClient):
    """Test that getting a non-existent task returns 404."""
    response = await client.get("/api/v1/tasks/nonexistent")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_list_tasks(client: AsyncClient):
    """Test listing tasks with pagination."""
    # Create multiple tasks
    for i in range(5):
        task_data = {
            "task_id": f"list-task-{i}",
            "task_family": "ListTask",
            "task_data": {},
            "user": "test-user",
            "commit_hash": "abc123",
        }
        await client.post("/api/v1/tasks", json=task_data)

    response = await client.get("/api/v1/tasks", params={"page_size": 3})
    assert response.status_code == 200
    data = response.json()
    assert len(data["tasks"]) == 3
    assert data["total"] == 5
    assert data["page"] == 1
    assert data["page_size"] == 3


@pytest.mark.asyncio
async def test_list_tasks_filter_by_family(client: AsyncClient):
    """Test filtering tasks by family."""
    await client.post(
        "/api/v1/tasks",
        json={
            "task_id": "family-a-1",
            "task_family": "FamilyA",
            "task_data": {},
            "user": "test-user",
            "commit_hash": "abc123",
        },
    )
    await client.post(
        "/api/v1/tasks",
        json={
            "task_id": "family-b-1",
            "task_family": "FamilyB",
            "task_data": {},
            "user": "test-user",
            "commit_hash": "abc123",
        },
    )

    response = await client.get("/api/v1/tasks", params={"task_family": "FamilyA"})
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    assert data["tasks"][0]["task_family"] == "FamilyA"


@pytest.mark.asyncio
async def test_start_task(client: AsyncClient):
    """Test starting a task."""
    task_data = {
        "task_id": "start-task-123",
        "task_family": "TestTask",
        "task_data": {},
        "user": "test-user",
        "commit_hash": "abc123",
    }
    await client.post("/api/v1/tasks", json=task_data)

    response = await client.post("/api/v1/tasks/start-task-123/start")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "running"
    assert data["started_at"] is not None


@pytest.mark.asyncio
async def test_complete_task(client: AsyncClient):
    """Test completing a task."""
    task_data = {
        "task_id": "complete-task-123",
        "task_family": "TestTask",
        "task_data": {},
        "user": "test-user",
        "commit_hash": "abc123",
    }
    await client.post("/api/v1/tasks", json=task_data)
    await client.post("/api/v1/tasks/complete-task-123/start")

    response = await client.post("/api/v1/tasks/complete-task-123/complete")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "completed"
    assert data["completed_at"] is not None


@pytest.mark.asyncio
async def test_fail_task(client: AsyncClient):
    """Test failing a task with error message."""
    task_data = {
        "task_id": "fail-task-123",
        "task_family": "TestTask",
        "task_data": {},
        "user": "test-user",
        "commit_hash": "abc123",
    }
    await client.post("/api/v1/tasks", json=task_data)
    await client.post("/api/v1/tasks/fail-task-123/start")

    response = await client.post(
        "/api/v1/tasks/fail-task-123/fail", params={"error_message": "Something broke"}
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "failed"
    assert data["error_message"] == "Something broke"
    assert data["completed_at"] is not None


@pytest.mark.asyncio
async def test_update_task(client: AsyncClient):
    """Test updating a task via PATCH."""
    task_data = {
        "task_id": "update-task-123",
        "task_family": "TestTask",
        "task_data": {},
        "user": "test-user",
        "commit_hash": "abc123",
    }
    await client.post("/api/v1/tasks", json=task_data)

    response = await client.patch(
        "/api/v1/tasks/update-task-123", json={"status": "running"}
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "running"
    assert data["started_at"] is not None


@pytest.mark.asyncio
async def test_task_with_dependencies(client: AsyncClient):
    """Test creating a task with dependencies."""
    # Create dependency tasks
    for i in range(2):
        await client.post(
            "/api/v1/tasks",
            json={
                "task_id": f"dep-{i}",
                "task_family": "DepTask",
                "task_data": {},
                "user": "test-user",
                "commit_hash": "abc123",
            },
        )

    # Create task with dependencies
    task_data = {
        "task_id": "with-deps-task",
        "task_family": "TestTask",
        "task_data": {},
        "user": "test-user",
        "commit_hash": "abc123",
        "dependency_ids": ["dep-0", "dep-1"],
    }
    response = await client.post("/api/v1/tasks", json=task_data)
    assert response.status_code == 201
    data = response.json()
    assert data["dependency_ids"] == ["dep-0", "dep-1"]
