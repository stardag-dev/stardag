"""Credential and configuration storage for Stardag CLI.

Stores:
- Credentials (OAuth tokens) in ~/.stardag/credentials.json
- Config (active context) in ~/.stardag/config.json
"""

import json
from pathlib import Path
from typing import TypedDict


# --- Credentials (OAuth tokens - user level) ---


class Credentials(TypedDict, total=False):
    """Stored credentials structure (OAuth tokens only)."""

    # OAuth tokens (from browser login)
    access_token: str  # JWT access token
    refresh_token: str  # Refresh token for getting new access tokens
    token_endpoint: str  # Token endpoint for refresh
    client_id: str  # OIDC client ID


def get_stardag_dir() -> Path:
    """Get the stardag config directory."""
    return Path.home() / ".stardag"


def get_credentials_path() -> Path:
    """Get the path to the credentials file."""
    return get_stardag_dir() / "credentials.json"


def load_credentials() -> Credentials | None:
    """Load credentials from disk.

    Returns:
        Credentials dict if file exists and is valid, None otherwise.
    """
    path = get_credentials_path()
    if not path.exists():
        return None

    try:
        with open(path) as f:
            data = json.load(f)
        return Credentials(**data)
    except (json.JSONDecodeError, TypeError, KeyError):
        return None


def save_credentials(credentials: Credentials) -> None:
    """Save credentials to disk.

    Args:
        credentials: Credentials dict to save.
    """
    path = get_credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    with open(path, "w") as f:
        json.dump(credentials, f, indent=2)

    # Make file readable only by owner (0600)
    path.chmod(0o600)


def clear_credentials() -> bool:
    """Clear stored credentials.

    Returns:
        True if credentials were cleared, False if no credentials existed.
    """
    path = get_credentials_path()
    if path.exists():
        path.unlink()
        return True
    return False


def get_access_token() -> str | None:
    """Get the stored access token."""
    creds = load_credentials()
    if creds is None:
        return None
    return creds.get("access_token")


def get_api_url() -> str | None:
    """Get the stored API URL from config."""
    config = load_config()
    return config.get("api_url")


# --- Config (active context - org/workspace selection) ---


class Config(TypedDict, total=False):
    """Stored config structure (settings and active context)."""

    # API settings
    api_url: str  # Base URL of the Stardag API
    timeout: float  # Request timeout in seconds

    # Active context
    organization_id: str  # Active organization ID
    workspace_id: str  # Active workspace ID


def get_config_path() -> Path:
    """Get the path to the config file."""
    return get_stardag_dir() / "config.json"


def load_config() -> Config:
    """Load config from disk.

    Returns:
        Config dict (empty dict if file doesn't exist).
    """
    path = get_config_path()
    if not path.exists():
        return Config()

    try:
        with open(path) as f:
            data = json.load(f)
        return Config(**data)
    except (json.JSONDecodeError, TypeError, KeyError):
        return Config()


def save_config(config: Config) -> None:
    """Save config to disk.

    Args:
        config: Config dict to save.
    """
    path = get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    with open(path, "w") as f:
        json.dump(config, f, indent=2)


def clear_config() -> bool:
    """Clear stored config.

    Returns:
        True if config was cleared, False if no config existed.
    """
    path = get_config_path()
    if path.exists():
        path.unlink()
        return True
    return False


def get_organization_id() -> str | None:
    """Get the active organization ID."""
    config = load_config()
    return config.get("organization_id")


def get_workspace_id() -> str | None:
    """Get the active workspace ID."""
    config = load_config()
    return config.get("workspace_id")


def set_organization_id(org_id: str) -> None:
    """Set the active organization ID."""
    config = load_config()
    config["organization_id"] = org_id
    # Clear workspace when org changes (workspace belongs to old org)
    if "workspace_id" in config:
        del config["workspace_id"]
    save_config(config)


def set_workspace_id(workspace_id: str) -> None:
    """Set the active workspace ID."""
    config = load_config()
    config["workspace_id"] = workspace_id
    save_config(config)


def set_api_url(api_url: str) -> None:
    """Set the API URL."""
    config = load_config()
    config["api_url"] = api_url.rstrip("/")
    save_config(config)


def get_timeout() -> float | None:
    """Get the stored timeout from config."""
    config = load_config()
    return config.get("timeout")


def set_timeout(timeout: float) -> None:
    """Set the timeout."""
    config = load_config()
    config["timeout"] = timeout
    save_config(config)
