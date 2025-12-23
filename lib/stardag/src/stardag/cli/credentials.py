"""Credential and configuration storage for Stardag CLI.

Stores credentials and config per profile:
- Credentials (OAuth tokens) in ~/.stardag/profiles/{profile}/credentials.json
- Config (active context) in ~/.stardag/profiles/{profile}/config.json
"""

import json
from pathlib import Path
from typing import TypedDict

from stardag.config import (
    get_profile_config_path,
    get_profile_credentials_path,
    get_profile_dir,
    load_active_profile,
    load_active_workspace,
    load_workspace_target_roots,
    save_active_profile,
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


def load_credentials(profile: str | None = None) -> Credentials | None:
    """Load credentials from disk.

    Args:
        profile: Profile name. If None, uses active profile.

    Returns:
        Credentials dict if file exists and is valid, None otherwise.
    """
    if profile is None:
        profile = load_active_profile()

    path = get_profile_credentials_path(profile)
    if not path.exists():
        return None

    try:
        with open(path) as f:
            data = json.load(f)
        return Credentials(**data)
    except (json.JSONDecodeError, TypeError, KeyError):
        return None


def save_credentials(credentials: Credentials, profile: str | None = None) -> None:
    """Save credentials to disk.

    Args:
        credentials: Credentials dict to save.
        profile: Profile name. If None, uses active profile.
    """
    if profile is None:
        profile = load_active_profile()

    path = get_profile_credentials_path(profile)
    path.parent.mkdir(parents=True, exist_ok=True)

    with open(path, "w") as f:
        json.dump(credentials, f, indent=2)

    # Make file readable only by owner (0600)
    path.chmod(0o600)


def clear_credentials(profile: str | None = None) -> bool:
    """Clear stored credentials.

    Args:
        profile: Profile name. If None, uses active profile.

    Returns:
        True if credentials were cleared, False if no credentials existed.
    """
    if profile is None:
        profile = load_active_profile()

    path = get_profile_credentials_path(profile)
    if path.exists():
        path.unlink()
        return True
    return False


def get_access_token(profile: str | None = None) -> str | None:
    """Get the stored access token."""
    creds = load_credentials(profile)
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


def load_config(profile: str | None = None) -> Config:
    """Load config from disk.

    Args:
        profile: Profile name. If None, uses active profile.

    Returns:
        Config dict (empty dict if file doesn't exist).
    """
    if profile is None:
        profile = load_active_profile()

    path = get_profile_config_path(profile)
    if not path.exists():
        return Config()

    try:
        with open(path) as f:
            data = json.load(f)
        return Config(**data)
    except (json.JSONDecodeError, TypeError, KeyError):
        return Config()


def save_config(config: Config, profile: str | None = None) -> None:
    """Save config to disk.

    Args:
        config: Config dict to save.
        profile: Profile name. If None, uses active profile.
    """
    if profile is None:
        profile = load_active_profile()

    path = get_profile_config_path(profile)
    path.parent.mkdir(parents=True, exist_ok=True)

    with open(path, "w") as f:
        json.dump(config, f, indent=2)


def clear_config(profile: str | None = None) -> bool:
    """Clear stored config.

    Args:
        profile: Profile name. If None, uses active profile.

    Returns:
        True if config was cleared, False if no config existed.
    """
    if profile is None:
        profile = load_active_profile()

    path = get_profile_config_path(profile)
    if path.exists():
        path.unlink()
        return True
    return False


# --- Convenience functions ---


def get_api_url(profile: str | None = None) -> str | None:
    """Get the stored API URL from config."""
    config = load_config(profile)
    return config.get("api_url")


def set_api_url(api_url: str, profile: str | None = None) -> None:
    """Set the API URL."""
    config = load_config(profile)
    config["api_url"] = api_url.rstrip("/")
    save_config(config, profile)


def get_organization_id(profile: str | None = None) -> str | None:
    """Get the active organization ID."""
    config = load_config(profile)
    return config.get("organization_id")


def set_organization_id(
    org_id: str, org_slug: str | None = None, profile: str | None = None
) -> None:
    """Set the active organization ID and slug.

    Note: This does NOT clear the active workspace. Call clear_workspace()
    separately if you want to clear it when switching organizations.
    """
    config = load_config(profile)
    config["organization_id"] = org_id
    if org_slug:
        config["organization_slug"] = org_slug
    save_config(config, profile)


def get_workspace_id(profile: str | None = None) -> str | None:
    """Get the active workspace ID from the active_workspace file."""
    if profile is None:
        profile = load_active_profile()
    return load_active_workspace(profile)


def set_workspace_id(workspace_id: str, profile: str | None = None) -> None:
    """Set the active workspace ID in the active_workspace file."""
    if profile is None:
        profile = load_active_profile()
    save_active_workspace(profile, workspace_id)


def clear_workspace(profile: str | None = None) -> bool:
    """Clear the active workspace for a profile.

    Args:
        profile: Profile name. If None, uses active profile.

    Returns:
        True if workspace was cleared, False if no workspace was set.
    """
    if profile is None:
        profile = load_active_profile()

    from stardag.config import get_profile_active_workspace_path

    path = get_profile_active_workspace_path(profile)
    if path.exists():
        path.unlink()
        return True
    return False


def get_timeout(profile: str | None = None) -> float | None:
    """Get the stored timeout from config."""
    config = load_config(profile)
    return config.get("timeout")


def set_timeout(timeout: float, profile: str | None = None) -> None:
    """Set the timeout."""
    config = load_config(profile)
    config["timeout"] = timeout
    save_config(config, profile)


def get_target_roots(profile: str | None = None) -> dict[str, str]:
    """Get the target roots for the active workspace."""
    if profile is None:
        profile = load_active_profile()
    workspace_id = load_active_workspace(profile)
    if not workspace_id:
        return {}
    return load_workspace_target_roots(profile, workspace_id)


def set_target_roots(target_roots: dict[str, str], profile: str | None = None) -> None:
    """Set the target roots for the active workspace."""
    if profile is None:
        profile = load_active_profile()
    workspace_id = load_active_workspace(profile)
    if not workspace_id:
        raise ValueError("No active workspace. Set a workspace first.")
    save_workspace_target_roots(profile, workspace_id, target_roots)


# --- Path convenience functions (for CLI display) ---


def get_credentials_path(profile: str | None = None) -> Path:
    """Get the path to the credentials file for display purposes."""
    if profile is None:
        profile = load_active_profile()
    return get_profile_credentials_path(profile)


def get_config_path(profile: str | None = None) -> Path:
    """Get the path to the config file for display purposes."""
    if profile is None:
        profile = load_active_profile()
    return get_profile_config_path(profile)


# --- Profile management ---


def list_profiles() -> list[str]:
    """List all available profiles."""
    from stardag.config import get_profiles_dir

    profiles_dir = get_profiles_dir()
    if not profiles_dir.exists():
        return []

    return [p.name for p in profiles_dir.iterdir() if p.is_dir()]


def get_active_profile() -> str:
    """Get the active profile name."""
    return load_active_profile()


def set_active_profile(profile: str) -> None:
    """Set the active profile."""
    save_active_profile(profile)


def create_profile(profile: str, api_url: str) -> None:
    """Create a new profile with the given API URL.

    Args:
        profile: Profile name.
        api_url: API URL for this profile.
    """
    # Ensure profile directory exists
    profile_dir = get_profile_dir(profile)
    profile_dir.mkdir(parents=True, exist_ok=True)

    # Set API URL in config
    set_api_url(api_url, profile)


def delete_profile(profile: str) -> bool:
    """Delete a profile and all its data.

    Args:
        profile: Profile name.

    Returns:
        True if profile was deleted, False if it didn't exist.
    """
    import shutil

    profile_dir = get_profile_dir(profile)
    if profile_dir.exists():
        shutil.rmtree(profile_dir)
        return True
    return False
