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
