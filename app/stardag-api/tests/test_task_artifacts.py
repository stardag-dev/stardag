"""Tests for task artifact endpoints."""

import pytest
from httpx import AsyncClient


@pytest.fixture
async def build_with_task(client: AsyncClient) -> tuple[str, str]:
    """Create a build with a task and return (build_id, task_id)."""
    # Create a build
    response = await client.post("/api/v1/builds", json={})
    build_id = response.json()["id"]

    # Register a task
    task_data = {
        "task_id": "artifact-test-task",
        "task_namespace": "test",
        "task_name": "ArtifactTestTask",
        "task_data": {"param": "value"},
    }
    await client.post(f"/api/v1/builds/{build_id}/tasks", json=task_data)

    return build_id, "artifact-test-task"


@pytest.mark.asyncio
async def test_upload_markdown_artifact(client: AsyncClient, build_with_task):
    """Test uploading a markdown artifact."""
    build_id, task_id = build_with_task

    artifacts = [
        {
            "type": "markdown",
            "name": "report",
            "body": {"content": "# Test Report\n\nThis is a test."},
        }
    ]
    response = await client.post(
        f"/api/v1/builds/{build_id}/tasks/{task_id}/artifacts", json=artifacts
    )
    assert response.status_code == 201
    data = response.json()
    assert len(data["artifacts"]) == 1
    artifact = data["artifacts"][0]
    assert artifact["artifact_type"] == "markdown"
    assert artifact["name"] == "report"
    assert artifact["body"] == {"content": "# Test Report\n\nThis is a test."}
    assert artifact["task_id"] == task_id


@pytest.mark.asyncio
async def test_upload_json_artifact(client: AsyncClient, build_with_task):
    """Test uploading a JSON artifact."""
    build_id, task_id = build_with_task

    artifacts = [
        {
            "type": "json",
            "name": "metrics",
            "body": {"accuracy": 0.95, "count": 100},
        }
    ]
    response = await client.post(
        f"/api/v1/builds/{build_id}/tasks/{task_id}/artifacts", json=artifacts
    )
    assert response.status_code == 201
    data = response.json()
    assert len(data["artifacts"]) == 1
    artifact = data["artifacts"][0]
    assert artifact["artifact_type"] == "json"
    assert artifact["name"] == "metrics"
    assert artifact["body"] == {"accuracy": 0.95, "count": 100}


@pytest.mark.asyncio
async def test_upload_multiple_artifacts(client: AsyncClient, build_with_task):
    """Test uploading multiple artifacts at once."""
    build_id, task_id = build_with_task

    artifacts = [
        {
            "type": "markdown",
            "name": "summary",
            "body": {"content": "# Summary"},
        },
        {
            "type": "json",
            "name": "data",
            "body": {"key": "value"},
        },
    ]
    response = await client.post(
        f"/api/v1/builds/{build_id}/tasks/{task_id}/artifacts", json=artifacts
    )
    assert response.status_code == 201
    data = response.json()
    assert len(data["artifacts"]) == 2


@pytest.mark.asyncio
async def test_get_task_artifacts(client: AsyncClient, build_with_task):
    """Test retrieving artifacts for a task."""
    build_id, task_id = build_with_task

    # Upload artifacts
    artifacts = [
        {
            "type": "markdown",
            "name": "report",
            "body": {"content": "# Report"},
        },
        {
            "type": "json",
            "name": "stats",
            "body": {"value": 42},
        },
    ]
    await client.post(
        f"/api/v1/builds/{build_id}/tasks/{task_id}/artifacts", json=artifacts
    )

    # Get artifacts via tasks endpoint
    response = await client.get(f"/api/v1/tasks/{task_id}/artifacts")
    assert response.status_code == 200
    data = response.json()
    assert len(data["artifacts"]) == 2

    # Verify content
    artifact_by_name = {a["name"]: a for a in data["artifacts"]}
    assert artifact_by_name["report"]["artifact_type"] == "markdown"
    assert artifact_by_name["report"]["body"] == {"content": "# Report"}
    assert artifact_by_name["stats"]["artifact_type"] == "json"
    assert artifact_by_name["stats"]["body"] == {"value": 42}


@pytest.mark.asyncio
async def test_get_artifacts_empty(client: AsyncClient, build_with_task):
    """Test getting artifacts when none exist."""
    _, task_id = build_with_task

    response = await client.get(f"/api/v1/tasks/{task_id}/artifacts")
    assert response.status_code == 200
    data = response.json()
    assert data["artifacts"] == []


@pytest.mark.asyncio
async def test_get_artifacts_task_not_found(client: AsyncClient):
    """Test getting artifacts for non-existent task."""
    response = await client.get("/api/v1/tasks/nonexistent/artifacts")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_upload_artifacts_build_not_found(client: AsyncClient):
    """Test uploading artifacts to non-existent build."""
    artifacts = [{"type": "markdown", "name": "test", "body": {"content": "test"}}]
    # Use a valid UUID format that doesn't exist in the database
    fake_uuid = "00000000-0000-0000-0000-000000000099"
    response = await client.post(
        f"/api/v1/builds/{fake_uuid}/tasks/sometask/artifacts", json=artifacts
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_upload_artifacts_task_not_found(client: AsyncClient):
    """Test uploading artifacts to non-existent task."""
    # Create a build
    response = await client.post("/api/v1/builds", json={})
    build_id = response.json()["id"]

    artifacts = [{"type": "markdown", "name": "test", "body": {"content": "test"}}]
    response = await client.post(
        f"/api/v1/builds/{build_id}/tasks/nonexistent/artifacts", json=artifacts
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_artifact_update_on_reupload(client: AsyncClient, build_with_task):
    """Test that re-uploading same artifact name/type updates it."""
    build_id, task_id = build_with_task

    # Upload initial artifact
    artifacts1 = [
        {
            "type": "json",
            "name": "metrics",
            "body": {"version": 1},
        }
    ]
    await client.post(
        f"/api/v1/builds/{build_id}/tasks/{task_id}/artifacts", json=artifacts1
    )

    # Upload again with same name/type
    artifacts2 = [
        {
            "type": "json",
            "name": "metrics",
            "body": {"version": 2, "extra": "data"},
        }
    ]
    await client.post(
        f"/api/v1/builds/{build_id}/tasks/{task_id}/artifacts", json=artifacts2
    )

    # Get artifacts - should have only one with updated body
    response = await client.get(f"/api/v1/tasks/{task_id}/artifacts")
    data = response.json()
    assert len(data["artifacts"]) == 1
    assert data["artifacts"][0]["body"] == {"version": 2, "extra": "data"}


@pytest.mark.asyncio
async def test_json_artifact_with_nested_data(client: AsyncClient, build_with_task):
    """Test JSON artifact with deeply nested structure."""
    build_id, task_id = build_with_task

    artifacts = [
        {
            "type": "json",
            "name": "complex",
            "body": {
                "metrics": {
                    "train": {"loss": 0.1, "accuracy": 0.95},
                    "test": {"loss": 0.2, "accuracy": 0.90},
                },
                "config": {
                    "layers": [64, 128, 64],
                    "activation": "relu",
                },
            },
        }
    ]
    response = await client.post(
        f"/api/v1/builds/{build_id}/tasks/{task_id}/artifacts", json=artifacts
    )
    assert response.status_code == 201

    # Verify retrieval
    response = await client.get(f"/api/v1/tasks/{task_id}/artifacts")
    data = response.json()
    body = data["artifacts"][0]["body"]
    assert body["metrics"]["train"]["accuracy"] == 0.95
    assert body["config"]["layers"] == [64, 128, 64]


@pytest.mark.asyncio
async def test_get_artifacts_with_environment_id(client: AsyncClient, build_with_task):
    """Test getting artifacts with explicit environment_id parameter.

    This verifies the endpoint works with environment_id, which is required
    for JWT authentication (UI calls).
    """
    build_id, task_id = build_with_task

    # Upload an artifact first
    artifacts = [{"type": "json", "name": "test", "body": {"key": "value"}}]
    await client.post(
        f"/api/v1/builds/{build_id}/tasks/{task_id}/artifacts", json=artifacts
    )

    # Get artifacts with environment_id parameter (simulates UI call)
    response = await client.get(
        f"/api/v1/tasks/{task_id}/artifacts", params={"environment_id": "default"}
    )
    assert response.status_code == 200
    data = response.json()
    assert len(data["artifacts"]) == 1
    assert data["artifacts"][0]["name"] == "test"
