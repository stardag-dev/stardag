"""Credential and configuration storage for Stardag CLI.

Stores credentials and config per registry:
- Credentials (OAuth tokens) in ~/.stardag/registries/{registry}/credentials.json
- Config (active context) in ~/.stardag/registries/{registry}/config.json
"""

import json
from pathlib import Path
from typing import TypedDict

from stardag.config import (
    get_registry_config_path,
    get_registry_credentials_path,
    get_registry_dir,
    load_active_registry,
    load_active_workspace,
    load_workspace_target_roots,
    save_active_registry,
    save_active_workspace,
    save_workspace_target_roots,
)


# --- Credentials (OAuth tokens) ---


class Credentials(TypedDict, total=False):
    """Stored credentials structure (OAuth tokens only)."""

    access_token: str  # JWT access token
    refresh_token: str  # Refresh token for getting new access tokens
    token_endpoint: str  # Token endpoint for refresh
    client_id: str  # OIDC client ID


def load_credentials(registry: str | None = None) -> Credentials | None:
    """Load credentials from disk.

    Args:
        registry: Registry name. If None, uses active registry.

    Returns:
        Credentials dict if file exists and is valid, None otherwise.
    """
    if registry is None:
        registry = load_active_registry()

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
        registry: Registry name. If None, uses active registry.
    """
    if registry is None:
        registry = load_active_registry()

    path = get_registry_credentials_path(registry)
    path.parent.mkdir(parents=True, exist_ok=True)

    with open(path, "w") as f:
        json.dump(credentials, f, indent=2)

    # Make file readable only by owner (0600)
    path.chmod(0o600)


def clear_credentials(registry: str | None = None) -> bool:
    """Clear stored credentials.

    Args:
        registry: Registry name. If None, uses active registry.

    Returns:
        True if credentials were cleared, False if no credentials existed.
    """
    if registry is None:
        registry = load_active_registry()

    path = get_registry_credentials_path(registry)
    if path.exists():
        path.unlink()
        return True
    return False


def get_access_token(registry: str | None = None) -> str | None:
    """Get the stored access token."""
    creds = load_credentials(registry)
    if creds is None:
        return None
    return creds.get("access_token")


# --- Config (active context - org/workspace selection) ---


class Config(TypedDict, total=False):
    """Stored config structure (settings and active context).

    Note: workspace_id is stored in active_workspace file, not here.
    Note: target_roots are stored in workspaces/{workspace_id}/target_roots.json.
    """

    # API settings
    api_url: str  # Base URL of the Stardag API
    timeout: float  # Request timeout in seconds

    # Active context (organization only - workspace is in active_workspace file)
    organization_id: str  # Active organization ID
    organization_slug: str  # Active organization slug (for validation)


def load_config(registry: str | None = None) -> Config:
    """Load config from disk.

    Args:
        registry: Registry name. If None, uses active registry.

    Returns:
        Config dict (empty dict if file doesn't exist).
    """
    if registry is None:
        registry = load_active_registry()

    path = get_registry_config_path(registry)
    if not path.exists():
        return Config()

    try:
        with open(path) as f:
            data = json.load(f)
        return Config(**data)
    except (json.JSONDecodeError, TypeError, KeyError):
        return Config()


def save_config(config: Config, registry: str | None = None) -> None:
    """Save config to disk.

    Args:
        config: Config dict to save.
        registry: Registry name. If None, uses active registry.
    """
    if registry is None:
        registry = load_active_registry()

    path = get_registry_config_path(registry)
    path.parent.mkdir(parents=True, exist_ok=True)

    with open(path, "w") as f:
        json.dump(config, f, indent=2)


def clear_config(registry: str | None = None) -> bool:
    """Clear stored config.

    Args:
        registry: Registry name. If None, uses active registry.

    Returns:
        True if config was cleared, False if no config existed.
    """
    if registry is None:
        registry = load_active_registry()

    path = get_registry_config_path(registry)
    if path.exists():
        path.unlink()
        return True
    return False


# --- Convenience functions ---


def get_api_url(registry: str | None = None) -> str | None:
    """Get the stored API URL from config."""
    config = load_config(registry)
    return config.get("api_url")


def set_api_url(api_url: str, registry: str | None = None) -> None:
    """Set the API URL."""
    config = load_config(registry)
    config["api_url"] = api_url.rstrip("/")
    save_config(config, registry)


def get_organization_id(registry: str | None = None) -> str | None:
    """Get the active organization ID."""
    config = load_config(registry)
    return config.get("organization_id")


def set_organization_id(
    org_id: str, org_slug: str | None = None, registry: str | None = None
) -> None:
    """Set the active organization ID and slug.

    Note: This does NOT clear the active workspace. Call clear_workspace()
    separately if you want to clear it when switching organizations.
    """
    config = load_config(registry)
    config["organization_id"] = org_id
    if org_slug:
        config["organization_slug"] = org_slug
    save_config(config, registry)


def get_workspace_id(registry: str | None = None) -> str | None:
    """Get the active workspace ID from the active_workspace file."""
    if registry is None:
        registry = load_active_registry()
    return load_active_workspace(registry)


def set_workspace_id(workspace_id: str, registry: str | None = None) -> None:
    """Set the active workspace ID in the active_workspace file."""
    if registry is None:
        registry = load_active_registry()
    save_active_workspace(registry, workspace_id)


def clear_workspace(registry: str | None = None) -> bool:
    """Clear the active workspace for a registry.

    Args:
        registry: Registry name. If None, uses active registry.

    Returns:
        True if workspace was cleared, False if no workspace was set.
    """
    if registry is None:
        registry = load_active_registry()

    from stardag.config import get_registry_active_workspace_path

    path = get_registry_active_workspace_path(registry)
    if path.exists():
        path.unlink()
        return True
    return False


def get_timeout(registry: str | None = None) -> float | None:
    """Get the stored timeout from config."""
    config = load_config(registry)
    return config.get("timeout")


def set_timeout(timeout: float, registry: str | None = None) -> None:
    """Set the timeout."""
    config = load_config(registry)
    config["timeout"] = timeout
    save_config(config, registry)


def get_target_roots(registry: str | None = None) -> dict[str, str]:
    """Get the target roots for the active workspace."""
    if registry is None:
        registry = load_active_registry()
    workspace_id = load_active_workspace(registry)
    if not workspace_id:
        return {}
    return load_workspace_target_roots(registry, workspace_id)


def set_target_roots(target_roots: dict[str, str], registry: str | None = None) -> None:
    """Set the target roots for the active workspace."""
    if registry is None:
        registry = load_active_registry()
    workspace_id = load_active_workspace(registry)
    if not workspace_id:
        raise ValueError("No active workspace. Set a workspace first.")
    save_workspace_target_roots(registry, workspace_id, target_roots)


# --- Path convenience functions (for CLI display) ---


def get_credentials_path(registry: str | None = None) -> Path:
    """Get the path to the credentials file for display purposes."""
    if registry is None:
        registry = load_active_registry()
    return get_registry_credentials_path(registry)


def get_config_path(registry: str | None = None) -> Path:
    """Get the path to the config file for display purposes."""
    if registry is None:
        registry = load_active_registry()
    return get_registry_config_path(registry)


# --- Registry management ---


def list_registries() -> list[str]:
    """List all available registries."""
    from stardag.config import get_registries_dir

    registries_dir = get_registries_dir()
    if not registries_dir.exists():
        return []

    return [r.name for r in registries_dir.iterdir() if r.is_dir()]


def get_active_registry() -> str:
    """Get the active registry name."""
    return load_active_registry()


def set_active_registry(registry: str) -> None:
    """Set the active registry."""
    save_active_registry(registry)


def create_registry(registry: str, api_url: str) -> None:
    """Create a new registry with the given API URL.

    Args:
        registry: Registry name.
        api_url: API URL for this registry.
    """
    # Ensure registry directory exists
    registry_dir = get_registry_dir(registry)
    registry_dir.mkdir(parents=True, exist_ok=True)

    # Set API URL in config
    set_api_url(api_url, registry)


def delete_registry(registry: str) -> bool:
    """Delete a registry and all its data.

    Args:
        registry: Registry name.

    Returns:
        True if registry was deleted, False if it didn't exist.
    """
    import shutil

    registry_dir = get_registry_dir(registry)
    if registry_dir.exists():
        shutil.rmtree(registry_dir)
        return True
    return False
