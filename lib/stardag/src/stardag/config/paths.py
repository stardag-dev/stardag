"""Path utilities and constants for Stardag configuration."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

# --- Constants ---

DEFAULT_TARGET_ROOT_KEY = "default"
DEFAULT_TARGET_ROOT = str(
    Path("~/.stardag/local-target-roots/default/default").expanduser().absolute()
)
DEFAULT_API_TIMEOUT = 30.0


# --- Path utilities ---


def get_stardag_dir() -> Path:
    """Get the user's stardag config directory (~/.stardag)."""
    return Path.home() / ".stardag"


def get_user_config_path() -> Path:
    """Get the user's config.toml path (~/.stardag/config.toml)."""
    return get_stardag_dir() / "config.toml"


def get_credentials_dir() -> Path:
    """Get the credentials directory (~/.stardag/credentials)."""
    return get_stardag_dir() / "credentials"


def get_access_token_cache_dir() -> Path:
    """Get the access token cache directory (~/.stardag/access-token-cache)."""
    return get_stardag_dir() / "access-token-cache"


def get_target_root_cache_path() -> Path:
    """Get the target root cache file path."""
    return get_stardag_dir() / "target-root-cache.json"


def get_id_cache_path() -> Path:
    """Get the ID cache file path (~/.stardag/id-cache.json)."""
    return get_stardag_dir() / "id-cache.json"


def get_local_target_roots_dir() -> Path:
    """Get the local target roots directory (~/.stardag/local-target-roots)."""
    return get_stardag_dir() / "local-target-roots"


def _sanitize_user_for_path(user: str) -> str:
    """Sanitize user identifier (email) for use in file paths.

    Replaces special characters that are problematic in file paths:
    - @ -> _at_ (email separator)
    - / -> _ (Unix path separator)
    - \\ -> _ (Windows path separator)
    - : -> _ (Windows drive separator, macOS resource fork)

    Args:
        user: User identifier (typically an email address).

    Returns:
        Sanitized string safe for use in file names across platforms.
    """
    return (
        user.replace("@", "_at_").replace("/", "_").replace("\\", "_").replace(":", "_")
    )


def get_registry_credentials_path(registry_name: str, user: str) -> Path:
    """Get the credentials file path for a specific registry and user.

    Args:
        registry_name: Name of the registry.
        user: User identifier (email).

    Returns:
        Path to the credentials file.
    """
    safe_user = _sanitize_user_for_path(user)
    return get_credentials_dir() / f"{registry_name}__{safe_user}.json"


def get_access_token_cache_path(
    registry_name: str, workspace_id: str, user: str
) -> Path:
    """Get the access token cache path for a registry/workspace/user combo.

    Args:
        registry_name: Name of the registry.
        workspace_id: Workspace ID.
        user: User identifier (email).

    Returns:
        Path to the access token cache file.
    """
    safe_user = _sanitize_user_for_path(user)
    return (
        get_access_token_cache_dir()
        / f"{registry_name}__{safe_user}__{workspace_id}.json"
    )


def find_project_config() -> Path | None:
    """Find .stardag/config.toml in current directory or parents.

    Returns:
        Path to project config if found, None otherwise.
    """
    current = Path.cwd()
    for directory in [current, *current.parents]:
        config_path = directory / ".stardag" / "config.toml"
        if config_path.exists():
            return config_path
    return None


def registry_key_from_url(url: str) -> str:
    """Derive a stable credential-storage key from a registry URL.

    Used when no TOML registry name is available (e.g., env-var-only config).
    Returns the hostname (with port if non-standard), sanitised for filenames.

    Examples:
        "https://api.stardag.com"      -> "api.stardag.com"
        "http://localhost:8000"         -> "localhost_8000"
        "https://api.stardag.com:443"   -> "api.stardag.com"
    """
    parsed = urlparse(url)
    host = parsed.hostname or "unknown"
    port = parsed.port
    # Include port only when non-standard
    if port and not (
        (parsed.scheme == "https" and port == 443)
        or (parsed.scheme == "http" and port == 80)
    ):
        return f"{host}_{port}"
    return host


__all__ = [
    "DEFAULT_TARGET_ROOT_KEY",
    "DEFAULT_TARGET_ROOT",
    "DEFAULT_API_TIMEOUT",
    "get_stardag_dir",
    "get_user_config_path",
    "get_credentials_dir",
    "get_access_token_cache_dir",
    "get_target_root_cache_path",
    "get_id_cache_path",
    "get_local_target_roots_dir",
    "_sanitize_user_for_path",
    "get_registry_credentials_path",
    "get_access_token_cache_path",
    "find_project_config",
    "registry_key_from_url",
]
