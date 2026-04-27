"""Tests for the api_keys service, focusing on validation, the in-process
TTL cache, and revocation semantics."""

from __future__ import annotations

import time
from unittest.mock import patch
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.services import api_keys
from stardag_api.services.api_keys import (
    _ValidationCache,
    create_api_key,
    revoke_api_key,
    validate_api_key,
)
from tests.conftest import DEFAULT_ENVIRONMENT_ID


# ---------------------------------------------------------------------------
# _ValidationCache unit tests
# ---------------------------------------------------------------------------


class TestValidationCache:
    def test_get_missing_returns_none(self) -> None:
        cache = _ValidationCache()
        assert cache.get("sk_anything") is None

    def test_put_then_get_returns_id(self) -> None:
        cache = _ValidationCache(ttl_seconds=60)
        api_key_id = UUID("00000000-0000-0000-0000-00000000abcd")
        cache.put("sk_full_key", api_key_id)
        assert cache.get("sk_full_key") == api_key_id

    def test_get_after_ttl_returns_none(self) -> None:
        cache = _ValidationCache(ttl_seconds=60)
        api_key_id = UUID("00000000-0000-0000-0000-00000000abcd")

        with patch.object(time, "monotonic", return_value=1000.0):
            cache.put("sk_full_key", api_key_id)

        # Past the TTL.
        with patch.object(time, "monotonic", return_value=1061.0):
            assert cache.get("sk_full_key") is None

    def test_invalidate_drops_entry(self) -> None:
        cache = _ValidationCache()
        api_key_id = UUID("00000000-0000-0000-0000-00000000abcd")
        cache.put("sk_full_key", api_key_id)
        cache.invalidate("sk_full_key")
        assert cache.get("sk_full_key") is None

    def test_clear_drops_all_entries(self) -> None:
        cache = _ValidationCache()
        cache.put("sk_a", UUID("00000000-0000-0000-0000-000000000001"))
        cache.put("sk_b", UUID("00000000-0000-0000-0000-000000000002"))
        cache.clear()
        assert cache.get("sk_a") is None
        assert cache.get("sk_b") is None

    def test_max_entries_clears_when_exceeded(self) -> None:
        cache = _ValidationCache(max_entries=3)
        for i in range(3):
            cache.put(f"sk_k{i}", UUID(int=i))
        # Fourth put should clear the cache before inserting.
        cache.put("sk_k_overflow", UUID(int=99))
        # The first three are gone.
        assert cache.get("sk_k0") is None
        assert cache.get("sk_k1") is None
        # The new entry is present.
        assert cache.get("sk_k_overflow") == UUID(int=99)

    def test_keys_are_hashed_not_stored_plain(self) -> None:
        # Ensure the dict is keyed by sha256 digest, not the raw secret.
        cache = _ValidationCache()
        cache.put("sk_secret", UUID(int=1))
        # No raw key should appear in the cache state.
        for stored_key in cache._cache:  # noqa: SLF001 — internal state check
            assert b"sk_secret" not in stored_key


# ---------------------------------------------------------------------------
# validate_api_key integration tests
# ---------------------------------------------------------------------------


class TestValidateApiKey:
    async def test_invalid_format_returns_none(
        self, async_session: AsyncSession
    ) -> None:
        assert await validate_api_key(async_session, "not-a-key") is None
        assert await validate_api_key(async_session, "") is None

    async def test_valid_key_returns_record(self, async_session: AsyncSession) -> None:
        api_key, full_key = await create_api_key(
            async_session, DEFAULT_ENVIRONMENT_ID, name="t1"
        )
        await async_session.commit()
        result = await validate_api_key(async_session, full_key)
        assert result is not None
        assert result.id == api_key.id

    async def test_unknown_key_returns_none(self, async_session: AsyncSession) -> None:
        # Well-formed but never created.
        unknown = "sk_abcdef_" + "x" * 32
        assert await validate_api_key(async_session, unknown) is None

    async def test_revoked_key_returns_none(self, async_session: AsyncSession) -> None:
        api_key, full_key = await create_api_key(
            async_session, DEFAULT_ENVIRONMENT_ID, name="t-revoke"
        )
        await async_session.commit()
        # Validate once to warm cache.
        assert await validate_api_key(async_session, full_key) is not None

        await revoke_api_key(async_session, api_key.id)
        await async_session.commit()
        # Cache hit path must still detect revocation via PK refetch.
        assert await validate_api_key(async_session, full_key) is None


# ---------------------------------------------------------------------------
# Cache hit path skips bcrypt; cache miss path uses asyncio.to_thread
# ---------------------------------------------------------------------------


class TestValidateCachingBehavior:
    async def test_cache_hit_skips_bcrypt(self, async_session: AsyncSession) -> None:
        _, full_key = await create_api_key(
            async_session, DEFAULT_ENVIRONMENT_ID, name="t-cache"
        )
        await async_session.commit()

        # Warm the cache.
        assert await validate_api_key(async_session, full_key) is not None

        # On the second call, verify_api_key (bcrypt) must not run.
        with patch.object(api_keys, "verify_api_key_async") as mock_verify:
            result = await validate_api_key(async_session, full_key)
            assert result is not None
            mock_verify.assert_not_awaited()

    async def test_cache_miss_uses_async_verify(
        self, async_session: AsyncSession
    ) -> None:
        _, full_key = await create_api_key(
            async_session, DEFAULT_ENVIRONMENT_ID, name="t-miss"
        )
        await async_session.commit()

        # Cold cache. verify_api_key_async should be awaited at least once.
        with patch.object(
            api_keys, "verify_api_key_async", wraps=api_keys.verify_api_key_async
        ) as mock_verify:
            result = await validate_api_key(async_session, full_key)
            assert result is not None
            mock_verify.assert_awaited()

    async def test_cache_isolated_per_full_key(
        self, async_session: AsyncSession
    ) -> None:
        _, key_a = await create_api_key(
            async_session, DEFAULT_ENVIRONMENT_ID, name="t-a"
        )
        _, key_b = await create_api_key(
            async_session, DEFAULT_ENVIRONMENT_ID, name="t-b"
        )
        await async_session.commit()
        # Validating key_a must not let key_b validate from cache.
        assert await validate_api_key(async_session, key_a) is not None
        with patch.object(api_keys, "verify_api_key_async") as mock_verify:
            mock_verify.return_value = False
            # key_b is unknown to the cache, so it must take the verify path.
            await validate_api_key(async_session, key_b)
            mock_verify.assert_awaited()


# ---------------------------------------------------------------------------
# verify_api_key_async runs in a thread
# ---------------------------------------------------------------------------


class TestVerifyAsync:
    async def test_async_verify_returns_true_for_valid(self) -> None:
        # Use a known-good plaintext/hash pair so we don't depend on the
        # in-DB record. generate_api_key bakes the hash so we round-trip it.
        from stardag_api.services.api_keys import generate_api_key

        full_key, _, key_hash = generate_api_key(DEFAULT_ENVIRONMENT_ID)
        assert await api_keys.verify_api_key_async(full_key, key_hash) is True

    async def test_async_verify_returns_false_for_wrong(self) -> None:
        from stardag_api.services.api_keys import generate_api_key

        _, _, key_hash = generate_api_key(DEFAULT_ENVIRONMENT_ID)
        assert await api_keys.verify_api_key_async("sk_wrong_value", key_hash) is False


@pytest.fixture(autouse=True)
def _clear_validation_cache_each_test():
    """The autouse cache-clearing fixture in conftest also clears this one,
    but be explicit here so test order doesn't matter."""
    api_keys._validation_cache.clear()
    yield
    api_keys._validation_cache.clear()
