"""Credential storage for Stardag CLI.

Stores tokens in ~/.stardag/credentials.json
"""

import json
from pathlib import Path
from typing import TypedDict


class Credentials(TypedDict, total=False):
    """Stored credentials structure."""

    # API configuration
    api_url: str  # Base URL of the Stardag API

    # OAuth tokens (from browser login)
    access_token: str  # JWT access token
    refresh_token: str  # Refresh token for getting new access tokens
    token_endpoint: str  # Token endpoint for refresh
    client_id: str  # OIDC client ID


def get_credentials_path() -> Path:
    """Get the path to the credentials file."""
    return Path.home() / ".stardag" / "credentials.json"


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

    # Set restrictive permissions on the file
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
    """Get the stored access token.

    Returns:
        The access token if stored, None otherwise.
    """
    creds = load_credentials()
    if creds is None:
        return None
    return creds.get("access_token")


def get_api_url() -> str | None:
    """Get the stored API URL.

    Returns:
        The API URL if stored, None otherwise.
    """
    creds = load_credentials()
    if creds is None:
        return None
    return creds.get("api_url")


def get_workspace_id() -> str | None:
    """Get the stored workspace ID.

    Returns:
        The workspace ID if stored, None otherwise.
    """
    creds = load_credentials()
    if creds is None:
        return None
    return creds.get("workspace_id")
