"""Tests for SaaS guardrails: rate limits, payload size limits, entity creation limits."""

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
