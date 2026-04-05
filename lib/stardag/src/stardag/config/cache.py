"""Target root cache and ID cache management."""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, Field

from stardag.config.io import load_json_file, save_json_file
from stardag.config.paths import get_id_cache_path, get_target_root_cache_path


# --- Target root cache ---


def load_target_root_cache() -> list[dict[str, Any]]:
    """Load the target root cache from disk."""
    path = get_target_root_cache_path()
    if not path.exists():
        return []
    try:
        with open(path) as f:
            data = json.load(f)
            if isinstance(data, list):
                return data
            return []
    except (json.JSONDecodeError, OSError):
        return []


def save_target_root_cache(cache: list[dict[str, Any]]) -> None:
    """Save the target root cache to disk."""
    path = get_target_root_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(cache, f, indent=2)


def get_cached_target_roots(
    registry_url: str, workspace_id: str, environment_id: str
) -> dict[str, str] | None:
    """Get cached target roots for a specific context.

    Returns:
        Dict of target root name to URI prefix, or None if not cached.
    """
    cache = load_target_root_cache()
    for entry in cache:
        if (
            entry.get("registry_url") == registry_url
            and entry.get("workspace_id") == workspace_id
            and entry.get("environment_id") == environment_id
        ):
            return entry.get("target_roots", {})
    return None


def update_cached_target_roots(
    registry_url: str,
    workspace_id: str,
    environment_id: str,
    target_roots: dict[str, str],
) -> None:
    """Update cached target roots for a specific context."""
    cache = load_target_root_cache()

    # Find and update existing entry or add new one
    for entry in cache:
        if (
            entry.get("registry_url") == registry_url
            and entry.get("workspace_id") == workspace_id
            and entry.get("environment_id") == environment_id
        ):
            entry["target_roots"] = target_roots
            save_target_root_cache(cache)
            return

    # Add new entry
    cache.append(
        {
            "registry_url": registry_url,
            "workspace_id": workspace_id,
            "environment_id": environment_id,
            "target_roots": target_roots,
        }
    )
    save_target_root_cache(cache)


# --- ID cache (slug -> UUID mappings) ---


class IdCache(BaseModel):
    """Cache for slug to ID mappings.

    Structure:
        workspaces: {registry_name: {workspace_slug: workspace_id}}
        environments: {registry_name: {workspace_id: {environment_slug: environment_id}}}
    """

    workspaces: dict[str, dict[str, str]] = Field(default_factory=dict)
    environments: dict[str, dict[str, dict[str, str]]] = Field(default_factory=dict)


def load_id_cache() -> IdCache:
    """Load the ID cache from disk."""
    data = load_json_file(get_id_cache_path())
    if not data:
        return IdCache()
    try:
        return IdCache(**data)
    except Exception:
        return IdCache()


def save_id_cache(cache: IdCache) -> None:
    """Save the ID cache to disk."""
    save_json_file(get_id_cache_path(), cache.model_dump())


def get_cached_workspace_id(registry: str, workspace_slug: str) -> str | None:
    """Get cached workspace ID for a slug.

    Args:
        registry: Registry name.
        workspace_slug: Workspace slug.

    Returns:
        Workspace ID if cached, None otherwise.
    """
    cache = load_id_cache()
    return cache.workspaces.get(registry, {}).get(workspace_slug)


def cache_workspace_id(registry: str, workspace_slug: str, workspace_id: str) -> None:
    """Cache a workspace slug to ID mapping.

    Args:
        registry: Registry name.
        workspace_slug: Workspace slug.
        workspace_id: Workspace ID (UUID).
    """
    cache = load_id_cache()
    if registry not in cache.workspaces:
        cache.workspaces[registry] = {}
    cache.workspaces[registry][workspace_slug] = workspace_id
    save_id_cache(cache)


def get_cached_environment_id(
    registry: str, workspace_id: str, environment_slug: str
) -> str | None:
    """Get cached environment ID for a slug.

    Args:
        registry: Registry name.
        workspace_id: Workspace ID (must be resolved).
        environment_slug: Environment slug.

    Returns:
        Environment ID if cached, None otherwise.
    """
    cache = load_id_cache()
    return (
        cache.environments.get(registry, {}).get(workspace_id, {}).get(environment_slug)
    )


def cache_environment_id(
    registry: str, workspace_id: str, environment_slug: str, environment_id: str
) -> None:
    """Cache an environment slug to ID mapping.

    Args:
        registry: Registry name.
        workspace_id: Workspace ID (must be resolved).
        environment_slug: Environment slug.
        environment_id: Environment ID (UUID).
    """
    cache = load_id_cache()
    if registry not in cache.environments:
        cache.environments[registry] = {}
    if workspace_id not in cache.environments[registry]:
        cache.environments[registry][workspace_id] = {}
    cache.environments[registry][workspace_id][environment_slug] = environment_id
    save_id_cache(cache)


def _looks_like_uuid(value: str) -> bool:
    """Check if a string looks like a UUID."""
    uuid_pattern = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
    return bool(re.match(uuid_pattern, value.lower()))
