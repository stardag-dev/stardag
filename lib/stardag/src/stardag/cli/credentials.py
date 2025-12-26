"""Credential and configuration storage for Stardag CLI.

New storage model:
- Credentials (refresh tokens) in ~/.stardag/credentials/{registry}.json
- Access token cache in ~/.stardag/access-token-cache/{registry}__{org}.json
- Config in ~/.stardag/config.toml (TOML format)
- Target root cache in ~/.stardag/target-root-cache.json
"""

import json
import time
from pathlib import Path
from typing import TypedDict

from stardag.config import (
    TomlConfig,
    get_access_token_cache_path,
    get_config,
    get_registry_credentials_path,
    get_user_config_path,
    load_toml_file,
    save_toml_file,
    update_cached_target_roots,
)


# --- Credentials (OAuth refresh tokens - per registry) ---


class Credentials(TypedDict, total=False):
    """Stored credentials structure (OAuth tokens only)."""

    refresh_token: str  # Refresh token for getting new access tokens
    token_endpoint: str  # Token endpoint for refresh
    client_id: str  # OIDC client ID


def load_credentials(registry: str | None = None) -> Credentials | None:
    """Load credentials from disk.

    Args:
        registry: Registry name. If None, uses active registry from config.

    Returns:
        Credentials dict if file exists and is valid, None otherwise.
    """
    if registry is None:
        config = get_config()
        registry = config.context.registry_name
        if not registry:
            return None

    path = get_registry_credentials_path(registry)
    if not path.exists():
        return None

    try:
        with open(path) as f:
            data = json.load(f)
        return Credentials(**data)
    except (json.JSONDecodeError, TypeError, KeyError):
        return None


def save_credentials(credentials: Credentials, registry: str | None = None) -> None:
    """Save credentials to disk.

    Args:
        credentials: Credentials dict to save.
        registry: Registry name. If None, uses active registry from config.
    """
    if registry is None:
        config = get_config()
        registry = config.context.registry_name
        if not registry:
            raise ValueError("No registry specified and no active profile")

    path = get_registry_credentials_path(registry)
    path.parent.mkdir(parents=True, exist_ok=True)

    with open(path, "w") as f:
        json.dump(credentials, f, indent=2)

    # Make file readable only by owner (0600)
    path.chmod(0o600)


def clear_credentials(registry: str | None = None) -> bool:
    """Clear stored credentials.

    Args:
        registry: Registry name. If None, uses active registry from config.

    Returns:
        True if credentials were cleared, False if no credentials existed.
    """
    if registry is None:
        config = get_config()
        registry = config.context.registry_name
        if not registry:
            return False

    path = get_registry_credentials_path(registry)
    if path.exists():
        path.unlink()
        return True
    return False


def get_refresh_token(registry: str | None = None) -> str | None:
    """Get the stored refresh token."""
    creds = load_credentials(registry)
    if creds is None:
        return None
    return creds.get("refresh_token")


# --- Access Token Cache (short-lived, per registry+org) ---


class AccessTokenCache(TypedDict, total=False):
    """Cached access token structure."""

    access_token: str  # JWT access token
    expires_at: float  # Unix timestamp when token expires


def load_access_token_cache(registry: str, org_id: str) -> AccessTokenCache | None:
    """Load access token from cache.

    Args:
        registry: Registry name.
        org_id: Organization ID.

    Returns:
        AccessTokenCache dict if file exists and token is valid, None otherwise.
    """
    path = get_access_token_cache_path(registry, org_id)
    if not path.exists():
        return None

    try:
        with open(path) as f:
            data = json.load(f)
        cache = AccessTokenCache(**data)

        # Check if token is expired
        expires_at = cache.get("expires_at", 0)
        if expires_at <= time.time():
            return None

        return cache
    except (json.JSONDecodeError, TypeError, KeyError):
        return None


def save_access_token_cache(
    registry: str, org_id: str, access_token: str, expires_in: int
) -> None:
    """Save access token to cache.

    Args:
        registry: Registry name.
        org_id: Organization ID.
        access_token: JWT access token.
        expires_in: Token TTL in seconds.
    """
    path = get_access_token_cache_path(registry, org_id)
    path.parent.mkdir(parents=True, exist_ok=True)

    # Calculate expiration time (subtract 30s buffer)
    expires_at = time.time() + expires_in - 30

    cache = AccessTokenCache(
        access_token=access_token,
        expires_at=expires_at,
    )

    with open(path, "w") as f:
        json.dump(cache, f, indent=2)

    # Make file readable only by owner (0600)
    path.chmod(0o600)


def clear_access_token_cache(registry: str, org_id: str) -> bool:
    """Clear cached access token.

    Args:
        registry: Registry name.
        org_id: Organization ID.

    Returns:
        True if cache was cleared, False if no cache existed.
    """
    path = get_access_token_cache_path(registry, org_id)
    if path.exists():
        path.unlink()
        return True
    return False


def get_access_token(
    registry: str | None = None, org_id: str | None = None
) -> str | None:
    """Get a valid access token, loading from cache.

    Args:
        registry: Registry name. If None, uses active registry.
        org_id: Organization ID. If None, uses active org.

    Returns:
        Access token if available and valid, None otherwise.
    """
    if registry is None or org_id is None:
        config = get_config()
        registry = registry or config.context.registry_name
        org_id = org_id or config.context.organization_id

    if not registry or not org_id:
        return None

    cache = load_access_token_cache(registry, org_id)
    if cache:
        return cache.get("access_token")

    return None


# --- TOML Config Management ---


def load_toml_config() -> TomlConfig:
    """Load the user's TOML config."""
    data = load_toml_file(get_user_config_path())
    return TomlConfig.from_toml_dict(data)


def save_toml_config(config: TomlConfig) -> None:
    """Save the user's TOML config."""
    # Convert back to dict format
    data: dict = {}

    if config.registry:
        data["registry"] = {
            name: {"url": reg.url} for name, reg in config.registry.items()
        }

    if config.profile:
        data["profile"] = {
            name: {
                "registry": prof.registry,
                "organization": prof.organization,
                "workspace": prof.workspace,
            }
            for name, prof in config.profile.items()
        }

    if config.default:
        data["default"] = config.default

    save_toml_file(get_user_config_path(), data)


def get_registry_url(registry: str | None = None) -> str | None:
    """Get the URL for a registry from config."""
    config = load_toml_config()
    if registry is None:
        # Get from active profile
        stardag_config = get_config()
        return stardag_config.api.url

    reg_config = config.registry.get(registry)
    if reg_config:
        return reg_config.url
    return None


def add_registry(name: str, url: str) -> None:
    """Add or update a registry in config.

    Args:
        name: Registry name.
        url: Registry URL.
    """
    config = load_toml_config()
    from stardag.config import RegistryConfig

    config.registry[name] = RegistryConfig(url=url.rstrip("/"))
    save_toml_config(config)


def remove_registry(name: str) -> bool:
    """Remove a registry from config.

    Args:
        name: Registry name.

    Returns:
        True if registry was removed, False if it didn't exist.
    """
    config = load_toml_config()
    if name in config.registry:
        del config.registry[name]
        save_toml_config(config)
        return True
    return False


def list_registries() -> dict[str, str]:
    """List all registries from config.

    Returns:
        Dict of registry name to URL.
    """
    config = load_toml_config()
    return {name: reg.url for name, reg in config.registry.items()}


def add_profile(name: str, registry: str, organization: str, workspace: str) -> None:
    """Add or update a profile in config.

    Args:
        name: Profile name.
        registry: Registry name.
        organization: Organization ID or slug.
        workspace: Workspace ID or slug.
    """
    config = load_toml_config()
    from stardag.config import ProfileConfig

    config.profile[name] = ProfileConfig(
        registry=registry,
        organization=organization,
        workspace=workspace,
    )
    save_toml_config(config)


def remove_profile(name: str) -> bool:
    """Remove a profile from config.

    Args:
        name: Profile name.

    Returns:
        True if profile was removed, False if it didn't exist.
    """
    config = load_toml_config()
    if name in config.profile:
        del config.profile[name]
        save_toml_config(config)
        return True
    return False


def list_profiles() -> dict[str, dict[str, str]]:
    """List all profiles from config.

    Returns:
        Dict of profile name to profile details.
    """
    config = load_toml_config()
    return {
        name: {
            "registry": prof.registry,
            "organization": prof.organization,
            "workspace": prof.workspace,
        }
        for name, prof in config.profile.items()
    }


def get_default_profile() -> str | None:
    """Get the default profile name from config."""
    config = load_toml_config()
    return config.default.get("profile")


def set_default_profile(profile: str) -> None:
    """Set the default profile in config.

    Args:
        profile: Profile name.
    """
    config = load_toml_config()
    config.default["profile"] = profile
    save_toml_config(config)


# --- Target Roots ---


def get_target_roots(
    registry_url: str | None = None,
    organization_id: str | None = None,
    workspace_id: str | None = None,
) -> dict[str, str]:
    """Get target roots from cache.

    Args:
        registry_url: Registry URL. If None, uses active config.
        organization_id: Organization ID. If None, uses active config.
        workspace_id: Workspace ID. If None, uses active config.

    Returns:
        Dict of target root name to URI prefix.
    """
    config = get_config()
    registry_url = registry_url or config.api.url
    organization_id = organization_id or config.context.organization_id
    workspace_id = workspace_id or config.context.workspace_id

    if not registry_url or not organization_id or not workspace_id:
        return {}

    from stardag.config import get_cached_target_roots

    return get_cached_target_roots(registry_url, organization_id, workspace_id) or {}


def set_target_roots(
    target_roots: dict[str, str],
    registry_url: str | None = None,
    organization_id: str | None = None,
    workspace_id: str | None = None,
) -> None:
    """Update target roots in cache.

    Args:
        target_roots: Dict of target root name to URI prefix.
        registry_url: Registry URL. If None, uses active config.
        organization_id: Organization ID. If None, uses active config.
        workspace_id: Workspace ID. If None, uses active config.
    """
    config = get_config()
    registry_url = registry_url or config.api.url
    organization_id = organization_id or config.context.organization_id
    workspace_id = workspace_id or config.context.workspace_id

    if not registry_url or not organization_id or not workspace_id:
        raise ValueError("Registry URL, organization ID, and workspace ID are required")

    update_cached_target_roots(
        registry_url,
        organization_id,
        workspace_id,
        target_roots,
    )


# --- Path convenience functions (for CLI display) ---


def get_credentials_path(registry: str | None = None) -> Path:
    """Get the path to the credentials file for display purposes."""
    if registry is None:
        config = get_config()
        registry = config.context.registry_name or "local"
    return get_registry_credentials_path(registry)


def get_config_path() -> Path:
    """Get the path to the config file for display purposes."""
    return get_user_config_path()
