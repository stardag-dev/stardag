"""API Key service for generating, validating, and managing API keys."""

import asyncio
import hashlib
import logging
import secrets
import time
from datetime import datetime, timezone
from uuid import UUID

import bcrypt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.models import ApiKey

logger = logging.getLogger(__name__)

# Key format: sk_<environment_prefix>_<random_bytes>
# Total length: ~40 characters
KEY_PREFIX = "sk_"
KEY_RANDOM_BYTES = 24  # 32 chars in base64

# Validation cache TTL. Bcrypt verification is ~20-100ms of CPU per check at
# default cost (12 rounds). Caching the result for a short window collapses
# repeated SDK calls (which can be hundreds per build burst) to a single bcrypt
# verification per key per minute per process. Revoked/expired keys are
# detected on the next request after the entry expires.
_VALIDATION_CACHE_TTL_SECONDS = 60.0
# Soft cap on cache entries; valid keys only grow the cache, so this is mostly
# a safety net. If exceeded the cache is cleared and re-warmed.
_VALIDATION_CACHE_MAX_ENTRIES = 10_000


def generate_api_key(environment_id: UUID) -> tuple[str, str, str]:
    """Generate a new API key.

    Returns:
        Tuple of (full_key, key_prefix, key_hash)
        - full_key: The complete API key to show to user (only shown once)
        - key_prefix: First 8 chars for identification
        - key_hash: bcrypt hash for storage
    """
    # Generate random bytes and encode as URL-safe base64
    random_part = secrets.token_urlsafe(KEY_RANDOM_BYTES)

    # Create the full key with prefix
    # Use first 6 chars of environment_id hex for namespacing
    environment_prefix = environment_id.hex[:6]
    full_key = f"{KEY_PREFIX}{environment_prefix}_{random_part}"

    # Extract prefix for display (first 8 chars after sk_)
    key_prefix = full_key[3:11]  # Skip "sk_", take next 8 chars

    # Hash the key for storage
    key_hash = bcrypt.hashpw(full_key.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

    return full_key, key_prefix, key_hash


def verify_api_key(full_key: str, key_hash: str) -> bool:
    """Verify an API key against its stored hash.

    Args:
        full_key: The full API key provided by the client
        key_hash: The stored bcrypt hash

    Returns:
        True if the key is valid, False otherwise
    """
    try:
        return bcrypt.checkpw(full_key.encode("utf-8"), key_hash.encode("utf-8"))
    except Exception:
        return False


async def verify_api_key_async(full_key: str, key_hash: str) -> bool:
    """Async wrapper around verify_api_key.

    bcrypt is CPU-bound and synchronous; running it on the event loop blocks
    every other request on this worker for the duration of the check. Offload
    to a thread so the loop can keep serving traffic.
    """
    return await asyncio.to_thread(verify_api_key, full_key, key_hash)


class _ValidationCache:
    """In-process TTL cache for validate_api_key results.

    Keys are sha256(full_key) so the plaintext key isn't kept as a dict key.
    Values are (api_key_id, expires_at_monotonic). Entries expire after
    _VALIDATION_CACHE_TTL_SECONDS; revoked keys are detected on the next
    request after the entry expires (acceptable per the in-memory guardrail
    pattern used elsewhere in this service).

    Concurrency: mutated only by async handlers running on a single uvicorn
    worker's event loop, so dict ops are safe without a lock.
    """

    def __init__(
        self,
        ttl_seconds: float = _VALIDATION_CACHE_TTL_SECONDS,
        max_entries: int = _VALIDATION_CACHE_MAX_ENTRIES,
    ) -> None:
        self._ttl = ttl_seconds
        self._max_entries = max_entries
        self._cache: dict[bytes, tuple[UUID, float]] = {}

    @staticmethod
    def _hash(full_key: str) -> bytes:
        return hashlib.sha256(full_key.encode("utf-8")).digest()

    def get(self, full_key: str) -> UUID | None:
        entry = self._cache.get(self._hash(full_key))
        if entry is None:
            return None
        api_key_id, expires_at = entry
        if time.monotonic() > expires_at:
            self._cache.pop(self._hash(full_key), None)
            return None
        return api_key_id

    def put(self, full_key: str, api_key_id: UUID) -> None:
        if len(self._cache) >= self._max_entries:
            logger.warning(
                "api-key validation cache exceeded %d entries; clearing",
                self._max_entries,
            )
            self._cache.clear()
        self._cache[self._hash(full_key)] = (
            api_key_id,
            time.monotonic() + self._ttl,
        )

    def invalidate(self, full_key: str) -> None:
        self._cache.pop(self._hash(full_key), None)

    def clear(self) -> None:
        self._cache.clear()


_validation_cache = _ValidationCache()


async def create_api_key(
    db: AsyncSession,
    environment_id: UUID,
    name: str,
    created_by_id: UUID | None = None,
) -> tuple[ApiKey, str]:
    """Create a new API key for an environment.

    Args:
        db: Database session
        environment_id: The environment to create the key for
        name: Human-readable name for the key
        created_by_id: User ID of the creator (optional)

    Returns:
        Tuple of (ApiKey model, full_key)
        The full_key is only returned once and should be shown to the user.
    """
    full_key, key_prefix, key_hash = generate_api_key(environment_id)

    api_key = ApiKey(
        environment_id=environment_id,
        name=name,
        key_prefix=key_prefix,
        key_hash=key_hash,
        created_by_id=created_by_id,
    )
    db.add(api_key)
    await db.flush()
    await db.refresh(api_key)

    return api_key, full_key


async def list_api_keys(
    db: AsyncSession,
    environment_id: UUID,
    include_revoked: bool = False,
) -> list[ApiKey]:
    """List API keys for an environment.

    Args:
        db: Database session
        environment_id: The environment to list keys for
        include_revoked: Whether to include revoked keys

    Returns:
        List of ApiKey models (without the actual key values)
    """
    query = select(ApiKey).where(ApiKey.environment_id == environment_id)

    if not include_revoked:
        query = query.where(ApiKey.revoked_at.is_(None))

    query = query.order_by(ApiKey.created_at.desc())

    result = await db.execute(query)
    return list(result.scalars().all())


async def get_api_key_by_id(
    db: AsyncSession,
    key_id: UUID,
) -> ApiKey | None:
    """Get an API key by its ID.

    Args:
        db: Database session
        key_id: The API key ID

    Returns:
        ApiKey model or None if not found
    """
    return await db.get(ApiKey, key_id)


async def revoke_api_key(
    db: AsyncSession,
    key_id: UUID,
) -> ApiKey | None:
    """Revoke an API key.

    Args:
        db: Database session
        key_id: The API key ID to revoke

    Returns:
        The revoked ApiKey model or None if not found
    """
    api_key = await db.get(ApiKey, key_id)
    if api_key is None:
        return None

    api_key.revoked_at = datetime.now(timezone.utc)
    await db.flush()
    await db.refresh(api_key)

    return api_key


async def find_api_key_by_prefix(
    db: AsyncSession,
    key_prefix: str,
) -> list[ApiKey]:
    """Find API keys by their prefix.

    Used during authentication to narrow down potential matches.

    Args:
        db: Database session
        key_prefix: The key prefix to search for

    Returns:
        List of matching ApiKey models (active only)
    """
    result = await db.execute(
        select(ApiKey)
        .where(ApiKey.key_prefix == key_prefix)
        .where(ApiKey.revoked_at.is_(None))
    )
    return list(result.scalars().all())


async def validate_api_key(
    db: AsyncSession,
    full_key: str,
) -> ApiKey | None:
    """Validate an API key and return the associated key record.

    This is the main authentication function for API key auth. Successful
    validations are cached in-process for _VALIDATION_CACHE_TTL_SECONDS so
    we don't run bcrypt on every authenticated request. On cache hit we
    still re-fetch the ApiKey by primary key and re-check `revoked_at`, so
    revocations take effect on the next request after revoke. The bcrypt
    verification on cache miss runs in a thread so it doesn't block the
    event loop.

    Args:
        db: Database session
        full_key: The full API key provided by the client

    Returns:
        ApiKey model if valid, None otherwise
    """
    # Check key format
    if not full_key.startswith(KEY_PREFIX):
        return None

    # Extract prefix for lookup (first 8 chars after "sk_")
    try:
        key_prefix = full_key[3:11]
    except IndexError:
        return None

    # Cache hit: confirm the row still exists and isn't revoked, then return.
    # We trade one PK lookup for skipping the prefix scan + bcrypt loop.
    cached_id = _validation_cache.get(full_key)
    if cached_id is not None:
        api_key = await db.get(ApiKey, cached_id)
        if api_key is not None and api_key.revoked_at is None:
            return api_key
        # Revoked or deleted since cache entry was created.
        _validation_cache.invalidate(full_key)

    # Cache miss: do the full validation flow.
    candidates = await find_api_key_by_prefix(db, key_prefix)

    for candidate in candidates:
        if await verify_api_key_async(full_key, candidate.key_hash):
            # Update last_used_at on cache miss only — once per TTL window
            # is plenty for this informational column and avoids per-request
            # write traffic.
            candidate.last_used_at = datetime.now(timezone.utc)
            await db.flush()
            _validation_cache.put(full_key, candidate.id)
            return candidate

    return None
