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
