"""Tests for SaaS guardrails: rate limits, payload size limits, entity creation limits."""

from unittest.mock import patch

import pytest
from httpx import AsyncClient

from stardag_api.limits import (
    EntityCountCache,
    ErrorCode,
    InMemoryRateLimiter,
    LimitsSettings,
    check_payload_size,
    check_rate_limit,
    check_structural_limit,
)


# ---------------------------------------------------------------------------
# Unit tests for core limit functions
# ---------------------------------------------------------------------------


class TestLimitsSettings:
    def test_all_defaults_are_none(self):
        """All limits disabled by default (OSS-safe)."""
        settings = LimitsSettings()
        assert settings.max_task_data_bytes is None
        assert settings.max_artifact_body_bytes is None
        assert settings.max_requests_per_minute is None
        assert settings.max_builds_per_workspace_24h is None
        assert settings.max_tasks_per_workspace_24h is None
        assert settings.max_events_per_workspace_24h is None
        assert settings.max_artifacts_per_workspace_24h is None
        assert settings.max_dependency_ids_per_task is None
        assert settings.max_artifacts_per_task is None
        assert settings.entity_count_cache_ttl == 60


class TestPayloadSizeCheck:
    def test_disabled_when_none(self):
        result = check_payload_size(
            {"big": "data"}, None, ErrorCode.TASK_DATA_SIZE_LIMIT, "test"
        )
        assert result is None

    def test_under_limit(self):
        result = check_payload_size(
            {"x": 1}, 1000, ErrorCode.TASK_DATA_SIZE_LIMIT, "task_data"
        )
        assert result is None

    def test_over_limit(self):
        # A small dict serialized to >5 bytes
        result = check_payload_size(
            {"data": "x" * 100}, 10, ErrorCode.TASK_DATA_SIZE_LIMIT, "task_data"
        )
        assert result is not None
        assert result.error_code == ErrorCode.TASK_DATA_SIZE_LIMIT
        assert result.limit == 10
        assert result.current is not None
        assert result.current > 10
        assert "info@stardag.com" in result.message


class TestStructuralLimit:
    def test_disabled_when_none(self):
        result = check_structural_limit(
            999, None, ErrorCode.DEPENDENCY_COUNT_LIMIT, "deps"
        )
        assert result is None

    def test_under_limit(self):
        result = check_structural_limit(5, 10, ErrorCode.DEPENDENCY_COUNT_LIMIT, "deps")
        assert result is None

    def test_at_limit(self):
        result = check_structural_limit(
            10, 10, ErrorCode.DEPENDENCY_COUNT_LIMIT, "deps"
        )
        assert result is None

    def test_over_limit(self):
        result = check_structural_limit(
            11, 10, ErrorCode.DEPENDENCY_COUNT_LIMIT, "deps"
        )
        assert result is not None
        assert result.error_code == ErrorCode.DEPENDENCY_COUNT_LIMIT
        assert result.limit == 10
        assert result.current == 11


class TestInMemoryRateLimiter:
    def test_allows_under_limit(self):
        limiter = InMemoryRateLimiter()
        from uuid import UUID

        ws = UUID("00000000-0000-0000-0000-000000000001")
        for _ in range(5):
            assert limiter.check(ws, 10) is None

    def test_blocks_at_limit(self):
        limiter = InMemoryRateLimiter()
        from uuid import UUID

        ws = UUID("00000000-0000-0000-0000-000000000001")
        for _ in range(10):
            limiter.check(ws, 10)
        result = limiter.check(ws, 10)
        assert result is not None
        assert result >= 1

    def test_different_workspaces_independent(self):
        limiter = InMemoryRateLimiter()
        from uuid import UUID

        ws1 = UUID("00000000-0000-0000-0000-000000000001")
        ws2 = UUID("00000000-0000-0000-0000-000000000002")
        for _ in range(10):
            limiter.check(ws1, 10)
        # ws1 is at limit
        assert limiter.check(ws1, 10) is not None
        # ws2 should be fine
        assert limiter.check(ws2, 10) is None

    def test_clear(self):
        limiter = InMemoryRateLimiter()
        from uuid import UUID

        ws = UUID("00000000-0000-0000-0000-000000000001")
        for _ in range(10):
            limiter.check(ws, 10)
        limiter.clear()
        assert limiter.check(ws, 10) is None


class TestRateLimitCheck:
    def test_disabled_when_none(self):
        from uuid import UUID

        settings = LimitsSettings(max_requests_per_minute=None)
        result = check_rate_limit(
            UUID("00000000-0000-0000-0000-000000000001"), settings
        )
        assert result is None

    def test_returns_error_when_exceeded(self):
        from uuid import UUID

        from stardag_api.limits import _rate_limiter

        _rate_limiter.clear()
        settings = LimitsSettings(max_requests_per_minute=2)
        ws = UUID("00000000-0000-0000-0000-000000000001")
        check_rate_limit(ws, settings)
        check_rate_limit(ws, settings)
        result = check_rate_limit(ws, settings)
        assert result is not None
        assert result.error_code == ErrorCode.RATE_LIMIT
        assert result.retry_after is not None
        assert result.retry_after >= 1


class TestEntityCountCache:
    def test_put_and_get(self):
        cache = EntityCountCache()
        entry = cache.put("key", 42)
        assert entry.estimated_count == 42

        retrieved = cache.get("key", ttl=60)
        assert retrieved is not None
        assert retrieved.estimated_count == 42

    def test_increment(self):
        cache = EntityCountCache()
        cache.put("key", 10)
        cache.increment("key")
        cache.increment("key")
        entry = cache.get("key", ttl=60)
        assert entry is not None
        assert entry.estimated_count == 12

    def test_ttl_expiry(self):
        import time

        cache = EntityCountCache()
        cache.put("key", 10)
        # Simulate expiry by setting fetched_at in the past
        cache._cache["key"].fetched_at = time.monotonic() - 100
        assert cache.get("key", ttl=60) is None

    def test_increment_missing_key_noop(self):
        cache = EntityCountCache()
        cache.increment("missing")  # Should not raise

    def test_clear(self):
        cache = EntityCountCache()
        cache.put("key", 10)
        cache.clear()
        assert cache.get("key", ttl=60) is None


# ---------------------------------------------------------------------------
# Integration tests via HTTP client
# ---------------------------------------------------------------------------


def _limits_settings(**overrides) -> LimitsSettings:
    """Create LimitsSettings with specified overrides."""
    return LimitsSettings(**overrides)


@pytest.mark.asyncio
async def test_existing_tests_pass_with_limits_disabled(client: AsyncClient):
    """Sanity check: basic operations work with all limits disabled (default)."""
    response = await client.post("/api/v1/builds", json={})
    assert response.status_code == 201


@pytest.mark.asyncio
async def test_rate_limit_429(client: AsyncClient):
    """Rate limit returns 429 with Retry-After header."""
    settings = _limits_settings(max_requests_per_minute=2)
    with patch("stardag_api.routes.builds.limits_settings", settings):
        await client.post("/api/v1/builds", json={})
        await client.post("/api/v1/builds", json={})
        response = await client.post("/api/v1/builds", json={})
    assert response.status_code == 429
    detail = response.json()["detail"]
    assert detail["error_code"] == "RATE_LIMIT"
    assert "Retry-After" in response.headers


@pytest.mark.asyncio
async def test_build_creation_limit_429(client: AsyncClient):
    """Build 24h limit returns 429."""
    settings = _limits_settings(max_builds_per_workspace_24h=2)
    with patch("stardag_api.routes.builds.limits_settings", settings):
        await client.post("/api/v1/builds", json={})
        await client.post("/api/v1/builds", json={})
        response = await client.post("/api/v1/builds", json={})
    assert response.status_code == 429
    detail = response.json()["detail"]
    assert detail["error_code"] == "BUILD_CREATION_LIMIT"


@pytest.mark.asyncio
async def test_task_data_size_limit_429(client: AsyncClient):
    """Oversized task_data returns 429."""
    settings = _limits_settings(max_task_data_bytes=10)
    with patch("stardag_api.routes.builds.limits_settings", settings):
        response = await client.post("/api/v1/builds", json={})
        build_id = response.json()["id"]

        task_data = {
            "task_id": "big-task",
            "task_namespace": "",
            "task_name": "BigTask",
            "task_data": {"payload": "x" * 100},
        }
        response = await client.post(f"/api/v1/builds/{build_id}/tasks", json=task_data)
    assert response.status_code == 429
    detail = response.json()["detail"]
    assert detail["error_code"] == "TASK_DATA_SIZE_LIMIT"


@pytest.mark.asyncio
async def test_dependency_count_limit_429(client: AsyncClient):
    """Too many dependency_task_ids returns 429."""
    settings = _limits_settings(max_dependency_ids_per_task=2)
    with patch("stardag_api.routes.builds.limits_settings", settings):
        response = await client.post("/api/v1/builds", json={})
        build_id = response.json()["id"]

        task_data = {
            "task_id": "many-deps-task",
            "task_namespace": "",
            "task_name": "ManyDepsTask",
            "task_data": {},
            "dependency_task_ids": ["dep1", "dep2", "dep3"],
        }
        response = await client.post(f"/api/v1/builds/{build_id}/tasks", json=task_data)
    assert response.status_code == 429
    detail = response.json()["detail"]
    assert detail["error_code"] == "DEPENDENCY_COUNT_LIMIT"


@pytest.mark.asyncio
async def test_artifact_body_size_limit_429(client: AsyncClient):
    """Oversized artifact body returns 429."""
    settings = _limits_settings(max_artifact_body_bytes=10)
    with patch("stardag_api.routes.builds.limits_settings", settings):
        # Create build and task
        response = await client.post("/api/v1/builds", json={})
        build_id = response.json()["id"]

        task_data = {
            "task_id": "artifact-task",
            "task_namespace": "",
            "task_name": "ArtifactTask",
            "task_data": {},
        }
        await client.post(f"/api/v1/builds/{build_id}/tasks", json=task_data)

        # Try to upload oversized artifact
        artifacts = [{"type": "json", "name": "big", "body": {"data": "x" * 100}}]
        response = await client.post(
            f"/api/v1/builds/{build_id}/tasks/artifact-task/artifacts", json=artifacts
        )
    assert response.status_code == 429
    detail = response.json()["detail"]
    assert detail["error_code"] == "ARTIFACT_BODY_SIZE_LIMIT"


@pytest.mark.asyncio
async def test_event_creation_limit_429(client: AsyncClient):
    """Event 24h limit returns 429."""
    # Set a very low event limit - create_build creates 1 event (BUILD_STARTED),
    # then complete_build tries to create another
    settings = _limits_settings(max_events_per_workspace_24h=1)
    with patch("stardag_api.routes.builds.limits_settings", settings):
        response = await client.post("/api/v1/builds", json={})
        build_id = response.json()["id"]

        response = await client.post(f"/api/v1/builds/{build_id}/complete")
    assert response.status_code == 429
    detail = response.json()["detail"]
    assert detail["error_code"] == "EVENT_CREATION_LIMIT"


@pytest.mark.asyncio
async def test_error_response_format(client: AsyncClient):
    """Verify the full error response structure."""
    settings = _limits_settings(max_builds_per_workspace_24h=1)
    with patch("stardag_api.routes.builds.limits_settings", settings):
        await client.post("/api/v1/builds", json={})
        response = await client.post("/api/v1/builds", json={})
    assert response.status_code == 429
    detail = response.json()["detail"]
    assert "error_code" in detail
    assert "message" in detail
    assert "limit" in detail
    assert "info@stardag.com" in detail["message"]


@pytest.mark.asyncio
async def test_limits_disabled_allows_all(client: AsyncClient):
    """With all limits None (default), everything passes."""
    # Create many builds - should all succeed with default settings
    for _ in range(5):
        response = await client.post("/api/v1/builds", json={})
        assert response.status_code == 201


@pytest.mark.asyncio
async def test_task_creation_limit_only_counts_new_tasks(client: AsyncClient):
    """Task 24h limit only blocks new tasks, not references to existing ones."""
    settings = _limits_settings(max_tasks_per_workspace_24h=1)
    with patch("stardag_api.routes.builds.limits_settings", settings):
        # Create first build and register a task
        response = await client.post("/api/v1/builds", json={})
        build1_id = response.json()["id"]
        task_data = {
            "task_id": "shared-task",
            "task_namespace": "",
            "task_name": "SharedTask",
            "task_data": {},
        }
        response = await client.post(
            f"/api/v1/builds/{build1_id}/tasks", json=task_data
        )
        assert response.status_code == 201

        # Create second build and reference same task (should succeed - not new)
        response = await client.post("/api/v1/builds", json={})
        build2_id = response.json()["id"]
        response = await client.post(
            f"/api/v1/builds/{build2_id}/tasks", json=task_data
        )
        assert response.status_code == 201

        # Try to create a genuinely new task (should fail)
        new_task_data = {
            "task_id": "new-task",
            "task_namespace": "",
            "task_name": "NewTask",
            "task_data": {},
        }
        response = await client.post(
            f"/api/v1/builds/{build2_id}/tasks", json=new_task_data
        )
    assert response.status_code == 429
    detail = response.json()["detail"]
    assert detail["error_code"] == "TASK_REGISTRATION_LIMIT"
