"""Tests for task registry assets endpoints."""

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
        "task_id": "asset-test-task",
        "task_namespace": "test",
        "task_name": "AssetTestTask",
        "task_data": {"param": "value"},
    }
    await client.post(f"/api/v1/builds/{build_id}/tasks", json=task_data)

    return build_id, "asset-test-task"


@pytest.mark.asyncio
async def test_upload_markdown_asset(client: AsyncClient, build_with_task):
    """Test uploading a markdown registry asset."""
    build_id, task_id = build_with_task

    assets = [
        {
            "type": "markdown",
            "name": "report",
            "body": {"content": "# Test Report\n\nThis is a test."},
        }
    ]
    response = await client.post(
        f"/api/v1/builds/{build_id}/tasks/{task_id}/assets", json=assets
    )
    assert response.status_code == 201
    data = response.json()
    assert len(data["assets"]) == 1
    asset = data["assets"][0]
    assert asset["asset_type"] == "markdown"
    assert asset["name"] == "report"
    assert asset["body"] == {"content": "# Test Report\n\nThis is a test."}
    assert asset["task_id"] == task_id


@pytest.mark.asyncio
async def test_upload_json_asset(client: AsyncClient, build_with_task):
    """Test uploading a JSON registry asset."""
    build_id, task_id = build_with_task

    assets = [
        {
            "type": "json",
            "name": "metrics",
            "body": {"accuracy": 0.95, "count": 100},
        }
    ]
    response = await client.post(
        f"/api/v1/builds/{build_id}/tasks/{task_id}/assets", json=assets
    )
    assert response.status_code == 201
    data = response.json()
    assert len(data["assets"]) == 1
    asset = data["assets"][0]
    assert asset["asset_type"] == "json"
    assert asset["name"] == "metrics"
    assert asset["body"] == {"accuracy": 0.95, "count": 100}


@pytest.mark.asyncio
async def test_upload_multiple_assets(client: AsyncClient, build_with_task):
    """Test uploading multiple assets at once."""
    build_id, task_id = build_with_task

    assets = [
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
        f"/api/v1/builds/{build_id}/tasks/{task_id}/assets", json=assets
    )
    assert response.status_code == 201
    data = response.json()
    assert len(data["assets"]) == 2


@pytest.mark.asyncio
async def test_get_task_assets(client: AsyncClient, build_with_task):
    """Test retrieving assets for a task."""
    build_id, task_id = build_with_task

    # Upload assets
    assets = [
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
    await client.post(f"/api/v1/builds/{build_id}/tasks/{task_id}/assets", json=assets)

    # Get assets via tasks endpoint
    response = await client.get(f"/api/v1/tasks/{task_id}/assets")
    assert response.status_code == 200
    data = response.json()
    assert len(data["assets"]) == 2

    # Verify content
    asset_by_name = {a["name"]: a for a in data["assets"]}
    assert asset_by_name["report"]["asset_type"] == "markdown"
    assert asset_by_name["report"]["body"] == {"content": "# Report"}
    assert asset_by_name["stats"]["asset_type"] == "json"
    assert asset_by_name["stats"]["body"] == {"value": 42}


@pytest.mark.asyncio
async def test_get_assets_empty(client: AsyncClient, build_with_task):
    """Test getting assets when none exist."""
    _, task_id = build_with_task

    response = await client.get(f"/api/v1/tasks/{task_id}/assets")
    assert response.status_code == 200
    data = response.json()
    assert data["assets"] == []


@pytest.mark.asyncio
async def test_get_assets_task_not_found(client: AsyncClient):
    """Test getting assets for non-existent task."""
    response = await client.get("/api/v1/tasks/nonexistent/assets")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_upload_assets_build_not_found(client: AsyncClient):
    """Test uploading assets to non-existent build."""
    assets = [{"type": "markdown", "name": "test", "body": {"content": "test"}}]
    response = await client.post(
        "/api/v1/builds/nonexistent/tasks/sometask/assets", json=assets
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_upload_assets_task_not_found(client: AsyncClient):
    """Test uploading assets to non-existent task."""
    # Create a build
    response = await client.post("/api/v1/builds", json={})
    build_id = response.json()["id"]

    assets = [{"type": "markdown", "name": "test", "body": {"content": "test"}}]
    response = await client.post(
        f"/api/v1/builds/{build_id}/tasks/nonexistent/assets", json=assets
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_asset_update_on_reupload(client: AsyncClient, build_with_task):
    """Test that re-uploading same asset name/type updates it."""
    build_id, task_id = build_with_task

    # Upload initial asset
    assets1 = [
        {
            "type": "json",
            "name": "metrics",
            "body": {"version": 1},
        }
    ]
    await client.post(f"/api/v1/builds/{build_id}/tasks/{task_id}/assets", json=assets1)

    # Upload again with same name/type
    assets2 = [
        {
            "type": "json",
            "name": "metrics",
            "body": {"version": 2, "extra": "data"},
        }
    ]
    await client.post(f"/api/v1/builds/{build_id}/tasks/{task_id}/assets", json=assets2)

    # Get assets - should have only one with updated body
    response = await client.get(f"/api/v1/tasks/{task_id}/assets")
    data = response.json()
    assert len(data["assets"]) == 1
    assert data["assets"][0]["body"] == {"version": 2, "extra": "data"}


@pytest.mark.asyncio
async def test_json_asset_with_nested_data(client: AsyncClient, build_with_task):
    """Test JSON asset with deeply nested structure."""
    build_id, task_id = build_with_task

    assets = [
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
        f"/api/v1/builds/{build_id}/tasks/{task_id}/assets", json=assets
    )
    assert response.status_code == 201

    # Verify retrieval
    response = await client.get(f"/api/v1/tasks/{task_id}/assets")
    data = response.json()
    body = data["assets"][0]["body"]
    assert body["metrics"]["train"]["accuracy"] == 0.95
    assert body["config"]["layers"] == [64, 128, 64]


@pytest.mark.asyncio
async def test_get_assets_with_environment_id(client: AsyncClient, build_with_task):
    """Test getting assets with explicit environment_id parameter.

    This verifies the endpoint works with environment_id, which is required
    for JWT authentication (UI calls).
    """
    build_id, task_id = build_with_task

    # Upload an asset first
    assets = [{"type": "json", "name": "test", "body": {"key": "value"}}]
    await client.post(f"/api/v1/builds/{build_id}/tasks/{task_id}/assets", json=assets)

    # Get assets with environment_id parameter (simulates UI call)
    response = await client.get(
        f"/api/v1/tasks/{task_id}/assets", params={"environment_id": "default"}
    )
    assert response.status_code == 200
    data = response.json()
    assert len(data["assets"]) == 1
    assert data["assets"][0]["name"] == "test"
