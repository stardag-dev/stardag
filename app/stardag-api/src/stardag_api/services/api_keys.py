"""API Key service for generating, validating, and managing API keys."""

import secrets
from datetime import datetime, timezone

import bcrypt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.models import ApiKey

# Key format: sk_<workspace_prefix>_<random_bytes>
# Total length: ~40 characters
KEY_PREFIX = "sk_"
KEY_RANDOM_BYTES = 24  # 32 chars in base64


def generate_api_key(workspace_id: str) -> tuple[str, str, str]:
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
    # Use first 6 chars of workspace_id for namespacing
    workspace_prefix = workspace_id[:6]
    full_key = f"{KEY_PREFIX}{workspace_prefix}_{random_part}"

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


async def create_api_key(
    db: AsyncSession,
    workspace_id: str,
    name: str,
    created_by_id: str | None = None,
) -> tuple[ApiKey, str]:
    """Create a new API key for a workspace.

    Args:
        db: Database session
        workspace_id: The workspace to create the key for
        name: Human-readable name for the key
        created_by_id: User ID of the creator (optional)

    Returns:
        Tuple of (ApiKey model, full_key)
        The full_key is only returned once and should be shown to the user.
    """
    full_key, key_prefix, key_hash = generate_api_key(workspace_id)

    api_key = ApiKey(
        workspace_id=workspace_id,
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
    workspace_id: str,
    include_revoked: bool = False,
) -> list[ApiKey]:
    """List API keys for a workspace.

    Args:
        db: Database session
        workspace_id: The workspace to list keys for
        include_revoked: Whether to include revoked keys

    Returns:
        List of ApiKey models (without the actual key values)
    """
    query = select(ApiKey).where(ApiKey.workspace_id == workspace_id)

    if not include_revoked:
        query = query.where(ApiKey.revoked_at.is_(None))

    query = query.order_by(ApiKey.created_at.desc())

    result = await db.execute(query)
    return list(result.scalars().all())


async def get_api_key_by_id(
    db: AsyncSession,
    key_id: str,
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
    key_id: str,
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

    This is the main authentication function for API key auth.

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

    # Find potential matches by prefix
    candidates = await find_api_key_by_prefix(db, key_prefix)

    # Verify against each candidate's hash
    for candidate in candidates:
        if verify_api_key(full_key, candidate.key_hash):
            # Update last_used_at
            candidate.last_used_at = datetime.now(timezone.utc)
            await db.flush()
            return candidate

    return None
