"""SaaS guardrails: rate limits, payload size limits, and entity creation limits.

All limits default to None (disabled/unlimited) for OSS-safe operation.
Configure via LIMITS_* environment variables in production.
"""

import json
import time
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.ext.asyncio import AsyncSession


class LimitsSettings(BaseSettings):
    """Rate and entity creation limits for SaaS guardrails.

    All limits default to None (disabled). Set to a positive integer to enable.
    """

    model_config = SettingsConfigDict(env_prefix="LIMITS_")

    # Payload size limits (bytes)
    max_task_data_bytes: Annotated[int, Field(ge=1)] | None = None
    max_asset_body_bytes: Annotated[int, Field(ge=1)] | None = None

    # Per-workspace rate limit (requests per minute per instance)
    max_requests_per_minute: Annotated[int, Field(ge=1)] | None = None

    # Per-workspace 24h entity creation limits
    max_builds_per_workspace_24h: Annotated[int, Field(ge=1)] | None = None
    max_tasks_per_workspace_24h: Annotated[int, Field(ge=1)] | None = None
    max_events_per_workspace_24h: Annotated[int, Field(ge=1)] | None = None
    max_assets_per_workspace_24h: Annotated[int, Field(ge=1)] | None = None

    # Structural limits
    max_dependency_ids_per_task: Annotated[int, Field(ge=1)] | None = None
    max_assets_per_task: Annotated[int, Field(ge=1)] | None = None

    # Cache TTL for DB count queries (seconds)
    entity_count_cache_ttl: Annotated[int, Field(ge=1)] = 60


class ErrorCode(StrEnum):
    TASK_DATA_SIZE_LIMIT = "TASK_DATA_SIZE_LIMIT"
    ASSET_BODY_SIZE_LIMIT = "ASSET_BODY_SIZE_LIMIT"
    RATE_LIMIT = "RATE_LIMIT"
    BUILD_CREATION_LIMIT = "BUILD_CREATION_LIMIT"
    TASK_REGISTRATION_LIMIT = "TASK_REGISTRATION_LIMIT"
    EVENT_CREATION_LIMIT = "EVENT_CREATION_LIMIT"
    ASSET_CREATION_LIMIT = "ASSET_CREATION_LIMIT"
    DEPENDENCY_COUNT_LIMIT = "DEPENDENCY_COUNT_LIMIT"
    ASSETS_PER_TASK_LIMIT = "ASSETS_PER_TASK_LIMIT"


class LimitExceededError(BaseModel):
    error_code: ErrorCode
    message: str
    limit: int | None = None
    current: int | None = None
    retry_after: int | None = None


CONTACT_SUFFIX = " Contact info@stardag.com if you need a higher quota."


# ---------------------------------------------------------------------------
# In-memory rate limiter (sliding window per workspace per instance)
# ---------------------------------------------------------------------------


class InMemoryRateLimiter:
    """Sliding window rate limiter keyed by workspace_id.

    Per-instance approximation: with N ECS tasks, effective limit is Nx configured.
    Acceptable for guardrails.

    Thread safety: check() is fully synchronous (no await points), so it runs
    atomically on the asyncio event loop without risk of interleaving.
    """

    def __init__(self) -> None:
        # workspace_id -> list of request timestamps
        self._windows: dict[UUID, list[float]] = {}

    def check(self, workspace_id: UUID, max_rpm: int) -> int | None:
        """Check rate limit. Returns seconds to retry after, or None if allowed."""
        now = time.monotonic()
        cutoff = now - 60.0

        timestamps = self._windows.get(workspace_id, [])
        # Prune old entries
        timestamps = [t for t in timestamps if t > cutoff]

        if len(timestamps) >= max_rpm:
            # Oldest timestamp in window determines when a slot opens
            retry_after = int(timestamps[0] - cutoff) + 1
            self._windows[workspace_id] = timestamps
            return max(retry_after, 1)

        timestamps.append(now)
        self._windows[workspace_id] = timestamps
        return None

    def clear(self) -> None:
        self._windows.clear()


_rate_limiter = InMemoryRateLimiter()


# ---------------------------------------------------------------------------
# Entity count cache (TTL cache for 24h DB counts)
# ---------------------------------------------------------------------------


class _CacheEntry:
    __slots__ = ("db_count", "fetched_at", "local_increment")

    def __init__(self, db_count: int, fetched_at: float) -> None:
        self.db_count = db_count
        self.fetched_at = fetched_at
        self.local_increment = 0

    @property
    def estimated_count(self) -> int:
        return self.db_count + self.local_increment


class EntityCountCache:
    """TTL cache for per-workspace 24h entity counts.

    Between DB refreshes, local increments track creates on this instance.
    """

    def __init__(self) -> None:
        self._cache: dict[str, _CacheEntry] = {}

    def get(self, key: str, ttl: int) -> _CacheEntry | None:
        entry = self._cache.get(key)
        if entry is None:
            return None
        if time.monotonic() - entry.fetched_at > ttl:
            del self._cache[key]
            return None
        return entry

    def put(self, key: str, db_count: int) -> _CacheEntry:
        entry = _CacheEntry(db_count, time.monotonic())
        self._cache[key] = entry
        return entry

    def increment(self, key: str) -> None:
        entry = self._cache.get(key)
        if entry is not None:
            entry.local_increment += 1

    def clear(self) -> None:
        self._cache.clear()


_entity_cache = EntityCountCache()


# ---------------------------------------------------------------------------
# 24h count queries
# ---------------------------------------------------------------------------

EntityType = str  # "builds" | "tasks" | "events" | "assets"

_ENTITY_LIMIT_MAP: dict[EntityType, tuple[str, ErrorCode]] = {
    "builds": ("max_builds_per_workspace_24h", ErrorCode.BUILD_CREATION_LIMIT),
    "tasks": ("max_tasks_per_workspace_24h", ErrorCode.TASK_REGISTRATION_LIMIT),
    "events": ("max_events_per_workspace_24h", ErrorCode.EVENT_CREATION_LIMIT),
    "assets": ("max_assets_per_workspace_24h", ErrorCode.ASSET_CREATION_LIMIT),
}

_ENTITY_DISPLAY_NAMES: dict[EntityType, str] = {
    "builds": "build",
    "tasks": "task registration",
    "events": "event creation",
    "assets": "asset creation",
}


async def _count_entities_24h(
    db: AsyncSession, workspace_id: UUID, entity_type: EntityType
) -> int:
    """Count entities created in the last 24h for a workspace."""
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import func, select

    from stardag_api.models import Build, Environment, Event, Task, TaskRegistryAsset

    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)

    if entity_type == "builds":
        stmt = (
            select(func.count())
            .select_from(Build)
            .join(Environment, Build.environment_id == Environment.id)
            .where(Environment.workspace_id == workspace_id)
            .where(Build.created_at >= cutoff)
        )
    elif entity_type == "tasks":
        stmt = (
            select(func.count())
            .select_from(Task)
            .join(Environment, Task.environment_id == Environment.id)
            .where(Environment.workspace_id == workspace_id)
            .where(Task.created_at >= cutoff)
        )
    elif entity_type == "events":
        stmt = (
            select(func.count())
            .select_from(Event)
            .join(Build, Event.build_id == Build.id)
            .join(Environment, Build.environment_id == Environment.id)
            .where(Environment.workspace_id == workspace_id)
            .where(Event.created_at >= cutoff)
        )
    elif entity_type == "assets":
        stmt = (
            select(func.count())
            .select_from(TaskRegistryAsset)
            .join(Environment, TaskRegistryAsset.environment_id == Environment.id)
            .where(Environment.workspace_id == workspace_id)
            .where(TaskRegistryAsset.created_at >= cutoff)
        )
    else:
        raise ValueError(f"Unknown entity type: {entity_type}")

    result = await db.execute(stmt)
    return result.scalar() or 0


# ---------------------------------------------------------------------------
# Enforcement functions
# ---------------------------------------------------------------------------


def check_rate_limit(
    workspace_id: UUID, settings: LimitsSettings
) -> LimitExceededError | None:
    """Check per-workspace rate limit. Returns error or None."""
    max_rpm = settings.max_requests_per_minute
    if max_rpm is None:
        return None

    retry_after = _rate_limiter.check(workspace_id, max_rpm)
    if retry_after is not None:
        return LimitExceededError(
            error_code=ErrorCode.RATE_LIMIT,
            message=f"Rate limit exceeded ({max_rpm} requests/minute).{CONTACT_SUFFIX}",
            limit=max_rpm,
            retry_after=retry_after,
        )
    return None


async def check_entity_creation_limit(
    db: AsyncSession,
    workspace_id: UUID,
    entity_type: EntityType,
    settings: LimitsSettings,
    amount: int = 1,
) -> LimitExceededError | None:
    """Check 24h entity creation limit. Returns error or None.

    Args:
        amount: Number of entities about to be created (for batch operations).
    """
    setting_attr, error_code = _ENTITY_LIMIT_MAP[entity_type]
    limit_value: int | None = getattr(settings, setting_attr)
    if limit_value is None:
        return None

    cache_key = f"{workspace_id}:{entity_type}"
    ttl = settings.entity_count_cache_ttl
    entry = _entity_cache.get(cache_key, ttl)

    if entry is None:
        db_count = await _count_entities_24h(db, workspace_id, entity_type)
        entry = _entity_cache.put(cache_key, db_count)

    current = entry.estimated_count
    if current + amount - 1 >= limit_value:
        display_name = _ENTITY_DISPLAY_NAMES[entity_type]
        return LimitExceededError(
            error_code=error_code,
            message=(
                f"24-hour {display_name} limit exceeded "
                f"({current}/{limit_value}).{CONTACT_SUFFIX}"
            ),
            limit=limit_value,
            current=current,
        )
    return None


def record_entity_created(workspace_id: UUID, entity_type: EntityType) -> None:
    """Record that an entity was created, incrementing the in-memory count cache."""
    _entity_cache.increment(f"{workspace_id}:{entity_type}")


def check_payload_size(
    data: object,
    max_bytes: int | None,
    error_code: ErrorCode,
    label: str,
) -> LimitExceededError | None:
    """Check JSON-serialized size of data against a byte limit."""
    if max_bytes is None:
        return None

    size = len(json.dumps(data, separators=(",", ":")).encode())
    if size > max_bytes:
        return LimitExceededError(
            error_code=error_code,
            message=(
                f"{label} size ({size} bytes) exceeds limit "
                f"({max_bytes} bytes).{CONTACT_SUFFIX}"
            ),
            limit=max_bytes,
            current=size,
        )
    return None


def check_structural_limit(
    count: int,
    max_count: int | None,
    error_code: ErrorCode,
    label: str,
) -> LimitExceededError | None:
    """Check a count against a structural limit."""
    if max_count is None:
        return None

    if count > max_count:
        return LimitExceededError(
            error_code=error_code,
            message=(
                f"{label} count ({count}) exceeds limit ({max_count}).{CONTACT_SUFFIX}"
            ),
            limit=max_count,
            current=count,
        )
    return None
