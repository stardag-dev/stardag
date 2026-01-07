"""Credential and configuration storage for Stardag CLI.

Storage model:
- Credentials (refresh tokens):
  - Per-user: ~/.stardag/credentials/{registry}__{user}.json
  - Legacy (no user): ~/.stardag/credentials/{registry}.json
- Access token cache:
  - Per-user: ~/.stardag/access-token-cache/{registry}__{user}__{org}.json
  - Legacy (no user): ~/.stardag/access-token-cache/{registry}__{org}.json
- Config in ~/.stardag/config.toml (TOML format)
- Target root cache in ~/.stardag/target-root-cache.json

Multi-user support:
- Profiles can optionally specify a 'user' field (email)
- Credentials are stored per (registry, user) to support multiple identities
- This allows switching between personal and work accounts on the same machine
"""

import json
import time
from pathlib import Path
from typing import TypedDict

from stardag.config import (
    TomlConfig,
    _looks_like_uuid,
    cache_org_id,
    cache_workspace_id,
    get_access_token_cache_path,
    get_config,
    get_registry_credentials_path,
    get_user_config_path,
    load_toml_file,
    save_toml_file,
    update_cached_target_roots,
)


# --- Credentials (OAuth refresh tokens - per registry/user) ---


class Credentials(TypedDict, total=False):
    """Stored credentials structure (OAuth tokens only)."""

    refresh_token: str  # Refresh token for getting new access tokens
    token_endpoint: str  # Token endpoint for refresh
    client_id: str  # OIDC client ID


def load_credentials(
    registry: str | None = None, user: str | None = None
) -> Credentials | None:
    """Load credentials from disk.

    Args:
        registry: Registry name. If None, uses active registry from config.
        user: User identifier (email). If None, uses user from active profile
            or falls back to legacy per-registry credentials.

    Returns:
        Credentials dict if file exists and is valid, None otherwise.
    """
    if registry is None or user is None:
        config = get_config()
        registry = registry or config.context.registry_name
        user = user or config.context.user
        if not registry:
            return None

    path = get_registry_credentials_path(registry, user)
    if not path.exists():
        # Fall back to legacy per-registry credentials if user-specific not found
        if user:
            legacy_path = get_registry_credentials_path(registry, None)
            if legacy_path.exists():
                path = legacy_path
            else:
                return None
        else:
            return None

    try:
        with open(path) as f:
            data = json.load(f)
        return Credentials(**data)
    except (json.JSONDecodeError, TypeError, KeyError):
        return None


def save_credentials(
    credentials: Credentials, registry: str | None = None, user: str | None = None
) -> None:
    """Save credentials to disk.

    Args:
        credentials: Credentials dict to save.
        registry: Registry name. If None, uses active registry from config.
        user: User identifier (email). If provided, saves user-specific credentials.
    """
    if registry is None:
        config = get_config()
        registry = config.context.registry_name
        user = user or config.context.user
        if not registry:
            raise ValueError("No registry specified and no active profile")

    path = get_registry_credentials_path(registry, user)
    path.parent.mkdir(parents=True, exist_ok=True)

    with open(path, "w") as f:
        json.dump(credentials, f, indent=2)

    # Make file readable only by owner (0600)
    path.chmod(0o600)


def clear_credentials(registry: str | None = None, user: str | None = None) -> bool:
    """Clear stored credentials.

    Args:
        registry: Registry name. If None, uses active registry from config.
        user: User identifier (email). If provided, clears user-specific credentials.

    Returns:
        True if credentials were cleared, False if no credentials existed.
    """
    if registry is None:
        config = get_config()
        registry = config.context.registry_name
        user = user or config.context.user
        if not registry:
            return False

    path = get_registry_credentials_path(registry, user)
    if path.exists():
        path.unlink()
        return True
    return False


def list_registries_with_credentials() -> list[str]:
    """List registries that have stored credentials.

    Returns:
        List of registry names with credentials files.
    """
    from stardag.config import get_credentials_dir

    creds_dir = get_credentials_dir()
    if not creds_dir.exists():
        return []

    registries = []
    for path in creds_dir.glob("*.json"):
        registries.append(path.stem)
    return sorted(registries)


def get_refresh_token(
    registry: str | None = None, user: str | None = None
) -> str | None:
    """Get the stored refresh token."""
    creds = load_credentials(registry, user)
    if creds is None:
        return None
    return creds.get("refresh_token")


# --- Access Token Cache (short-lived, per registry+user+org) ---


class AccessTokenCache(TypedDict, total=False):
    """Cached access token structure."""

    access_token: str  # JWT access token
    expires_at: float  # Unix timestamp when token expires


def load_access_token_cache(
    registry: str, org_id: str, user: str | None = None
) -> AccessTokenCache | None:
    """Load access token from cache.

    Args:
        registry: Registry name.
        org_id: Organization ID.
        user: User identifier (email). If provided, loads user-specific token.

    Returns:
        AccessTokenCache dict if file exists and token is valid, None otherwise.
    """
    path = get_access_token_cache_path(registry, org_id, user)
    if not path.exists():
        # Fall back to legacy path if user-specific not found
        if user:
            legacy_path = get_access_token_cache_path(registry, org_id, None)
            if legacy_path.exists():
                path = legacy_path
            else:
                return None
        else:
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
    registry: str,
    org_id: str,
    access_token: str,
    expires_in: int,
    user: str | None = None,
) -> None:
    """Save access token to cache.

    Args:
        registry: Registry name.
        org_id: Organization ID.
        access_token: JWT access token.
        expires_in: Token TTL in seconds.
        user: User identifier (email). If provided, saves user-specific token.
    """
    path = get_access_token_cache_path(registry, org_id, user)
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


def clear_access_token_cache(
    registry: str, org_id: str, user: str | None = None
) -> bool:
    """Clear cached access token.

    Args:
        registry: Registry name.
        org_id: Organization ID.
        user: User identifier (email). If provided, clears user-specific token.

    Returns:
        True if cache was cleared, False if no cache existed.
    """
    path = get_access_token_cache_path(registry, org_id, user)
    if path.exists():
        path.unlink()
        return True
    return False


def get_access_token(
    registry: str | None = None,
    org_id: str | None = None,
    user: str | None = None,
) -> str | None:
    """Get a valid access token, loading from cache.

    Args:
        registry: Registry name. If None, uses active registry.
        org_id: Organization ID. If None, uses active org.
        user: User identifier (email). If None, uses user from active profile.

    Returns:
        Access token if available and valid, None otherwise.
    """
    if registry is None or org_id is None or user is None:
        config = get_config()
        registry = registry or config.context.registry_name
        org_id = org_id or config.context.organization_id
        user = user or config.context.user

    if not registry or not org_id:
        return None

    cache = load_access_token_cache(registry, org_id, user)
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
        profiles_data: dict = {}
        for name, prof in config.profile.items():
            profile_dict: dict[str, str] = {
                "registry": prof.registry,
                "organization": prof.organization,
                "workspace": prof.workspace,
            }
            # Only include user if set (backward compatible)
            if prof.user:
                profile_dict["user"] = prof.user
            profiles_data[name] = profile_dict
        data["profile"] = profiles_data

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


def add_profile(
    name: str,
    registry: str,
    organization: str,
    workspace: str,
    user: str | None = None,
) -> None:
    """Add or update a profile in config.

    Args:
        name: Profile name.
        registry: Registry name.
        organization: Organization ID or slug.
        workspace: Workspace ID or slug.
        user: User identifier (email). Optional for multi-user support.
    """
    config = load_toml_config()
    from stardag.config import ProfileConfig

    config.profile[name] = ProfileConfig(
        registry=registry,
        user=user,
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


class ProfileDetails(TypedDict):
    """Profile details structure returned by list_profiles."""

    registry: str
    user: str | None
    organization: str
    workspace: str


def list_profiles() -> dict[str, ProfileDetails]:
    """List all profiles from config.

    Returns:
        Dict of profile name to profile details (including optional user).
    """
    config = load_toml_config()
    return {
        name: ProfileDetails(
            registry=prof.registry,
            user=prof.user,
            organization=prof.organization,
            workspace=prof.workspace,
        )
        for name, prof in config.profile.items()
    }


def get_default_profile() -> str | None:
    """Get the default profile name from config."""
    config = load_toml_config()
    return config.default.get("profile")


def get_active_profile() -> tuple[str | None, str | None]:
    """Get the currently active profile name and source.

    Returns:
        Tuple of (profile_name, source) where source is one of:
        - "env" if set via STARDAG_PROFILE environment variable
        - "default" if set via [default] in config
        - None if no active profile
    """
    import os

    # Check env var first
    env_profile = os.environ.get("STARDAG_PROFILE")
    if env_profile:
        return env_profile, "env"

    # Check config default
    default = get_default_profile()
    if default:
        return default, "default"

    return None, None


def find_matching_profile(
    registry: str,
    organization: str,
    workspace: str,
    user: str | None = None,
) -> str | None:
    """Find a profile that matches the given settings.

    Args:
        registry: Registry name.
        organization: Organization slug/ID.
        workspace: Workspace slug/ID.
        user: User identifier (email). If provided, matches profiles with same user.
            If None, matches profiles with no user set.

    Returns:
        Profile name if a matching profile exists, None otherwise.
    """
    profiles = list_profiles()
    for name, details in profiles.items():
        if (
            details["registry"] == registry
            and details["user"] == user
            and details["organization"] == organization
            and details["workspace"] == workspace
        ):
            return name
    return None


class InvalidProfileError(Exception):
    """Raised when STARDAG_PROFILE is set to a non-existent profile."""

    def __init__(self, profile_name: str, available_profiles: list[str], source: str):
        self.profile_name = profile_name
        self.available_profiles = available_profiles
        self.source = source
        super().__init__(self._format_message())

    def _format_message(self) -> str:
        if self.source == "env":
            source_hint = f"STARDAG_PROFILE={self.profile_name}"
        else:
            source_hint = f'[default] profile = "{self.profile_name}" in config'

        msg = f"Profile '{self.profile_name}' not found ({source_hint})."

        if self.available_profiles:
            msg += "\n\nAvailable profiles:"
            for name in self.available_profiles:
                msg += f"\n  - {name}"
            msg += "\n\nTo fix:"
            msg += "\n  - Set a valid profile: export STARDAG_PROFILE=<name>"
            msg += "\n  - Or create the missing profile: stardag config profile add ..."
        else:
            msg += "\n\nNo profiles configured."
            msg += "\n\nTo fix:"
            msg += "\n  - Unset the env var: unset STARDAG_PROFILE"
            msg += "\n  - Or create a profile: stardag config profile add ..."
            msg += "\n  - Or run: stardag auth login"

        return msg


def validate_active_profile() -> tuple[str, str] | tuple[None, None]:
    """Validate that the active profile exists in the config.

    Returns:
        Tuple of (profile_name, source) if valid or no profile set.

    Raises:
        InvalidProfileError: If STARDAG_PROFILE is set to a non-existent profile.
    """
    profile_name, source = get_active_profile()

    if profile_name is None:
        return None, None

    # When profile_name is set, source is always "env" or "default"
    assert source is not None

    profiles = list_profiles()
    if profile_name not in profiles:
        raise InvalidProfileError(
            profile_name=profile_name,
            available_profiles=list(profiles.keys()),
            source=source,
        )

    return profile_name, source


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


def get_credentials_path(registry: str | None = None, user: str | None = None) -> Path:
    """Get the path to the credentials file for display purposes."""
    if registry is None or user is None:
        config = get_config()
        registry = registry or config.context.registry_name or "local"
        user = user or config.context.user
    return get_registry_credentials_path(registry, user)


def get_config_path() -> Path:
    """Get the path to the config file for display purposes."""
    return get_user_config_path()


# --- Token Refresh Helpers ---


def _refresh_oidc_token(
    token_endpoint: str,
    refresh_token: str,
    client_id: str,
) -> dict:
    """Refresh OIDC tokens using refresh token.

    Returns the token response dict.
    Raises httpx.HTTPStatusError on failure.
    """
    try:
        import httpx
    except ImportError:
        raise ImportError("httpx is required. Install with: pip install stardag[cli]")

    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
    }

    with httpx.Client(timeout=30.0) as client:
        response = client.post(token_endpoint, data=data)
        response.raise_for_status()
        return response.json()


def _exchange_for_internal_token(
    api_url: str,
    oidc_token: str,
    org_id: str,
) -> dict:
    """Exchange OIDC token for internal org-scoped token.

    Returns dict with access_token and expires_in.
    Raises httpx.HTTPStatusError on failure.
    """
    try:
        import httpx
    except ImportError:
        raise ImportError("httpx is required. Install with: pip install stardag[cli]")

    with httpx.Client(timeout=30.0) as client:
        response = client.post(
            f"{api_url}/api/v1/auth/exchange",
            json={"org_id": org_id},
            headers={"Authorization": f"Bearer {oidc_token}"},
        )
        response.raise_for_status()
        return response.json()


def _get_user_organizations(api_url: str, oidc_token: str) -> list[dict]:
    """Fetch user's organizations from API using OIDC token."""
    try:
        import httpx
    except ImportError:
        return []

    try:
        with httpx.Client(timeout=10.0) as client:
            response = client.get(
                f"{api_url}/api/v1/ui/me",
                headers={"Authorization": f"Bearer {oidc_token}"},
            )
            if response.status_code == 200:
                data = response.json()
                return data.get("organizations", [])
    except Exception:
        pass
    return []


def _get_workspaces(api_url: str, access_token: str, org_id: str) -> list[dict]:
    """Fetch workspaces for an organization using internal token."""
    try:
        import httpx
    except ImportError:
        return []

    try:
        with httpx.Client(timeout=10.0) as client:
            response = client.get(
                f"{api_url}/api/v1/ui/organizations/{org_id}/workspaces",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if response.status_code == 200:
                return response.json()
    except Exception:
        pass
    return []


def ensure_access_token(
    registry: str,
    org_id: str,
    quiet: bool = False,
) -> str | None:
    """Ensure we have a valid access token, refreshing if needed.

    Args:
        registry: Registry name.
        org_id: Organization ID.
        quiet: If True, suppress warning messages.

    Returns:
        Access token if available/refreshed successfully, None otherwise.
    """
    # Check for cached valid token first
    cached = load_access_token_cache(registry, org_id)
    if cached:
        cached_token = cached.get("access_token")
        if cached_token:
            return cached_token

    # Need to refresh - get credentials
    creds = load_credentials(registry)
    if not creds:
        return None

    token_endpoint = creds.get("token_endpoint")
    refresh_token = creds.get("refresh_token")
    client_id = creds.get("client_id")

    if not token_endpoint or not client_id:
        return None

    # Get registry URL
    registry_url = get_registry_url(registry)
    if not registry_url:
        return None

    try:
        # If we have a refresh token, use it
        if refresh_token:
            tokens = _refresh_oidc_token(token_endpoint, refresh_token, client_id)

            # Update stored refresh token if a new one was provided
            if tokens.get("refresh_token"):
                creds["refresh_token"] = tokens["refresh_token"]
                save_credentials(creds, registry)

            oidc_token = tokens["access_token"]
        else:
            # No refresh token - can't refresh
            return None

        # Exchange for internal token
        internal_tokens = _exchange_for_internal_token(registry_url, oidc_token, org_id)
        access_token = internal_tokens["access_token"]
        expires_in = internal_tokens.get("expires_in", 600)

        # Cache it
        save_access_token_cache(registry, org_id, access_token, expires_in)

        return access_token

    except Exception:
        return None


def resolve_org_slug_to_id(
    registry: str,
    org_slug_or_id: str,
    oidc_token: str | None = None,
) -> str | None:
    """Resolve an organization slug to its ID.

    Args:
        registry: Registry name.
        org_slug_or_id: Organization slug or ID.
        oidc_token: Optional OIDC token. If not provided, will try to refresh.

    Returns:
        Organization ID if found, None otherwise.
        If input looks like a UUID, returns it unchanged.

    Side effects:
        Populates the ID cache with all discovered org mappings.
    """
    # If it looks like a UUID, assume it's already an ID
    if _looks_like_uuid(org_slug_or_id):
        return org_slug_or_id

    # Need to resolve slug - get OIDC token if not provided
    if not oidc_token:
        oidc_token = _get_fresh_oidc_token(registry)
        if not oidc_token:
            return None

    # Get registry URL
    registry_url = get_registry_url(registry)
    if not registry_url:
        return None

    # Fetch organizations and find matching slug
    orgs = _get_user_organizations(registry_url, oidc_token)
    result = None
    for org in orgs:
        org_id = org.get("id")
        org_slug = org.get("slug")
        # Cache all discovered orgs
        if org_id and org_slug:
            cache_org_id(registry, org_slug, org_id)
        # Check if this is the one we're looking for
        if org_slug == org_slug_or_id or org_id == org_slug_or_id:
            result = org_id

    return result


def resolve_workspace_slug_to_id(
    registry: str,
    org_id: str,
    workspace_slug_or_id: str,
    access_token: str | None = None,
) -> str | None:
    """Resolve a workspace slug to its ID.

    Args:
        registry: Registry name.
        org_id: Organization ID (must be resolved already).
        workspace_slug_or_id: Workspace slug or ID.
        access_token: Optional internal access token. If not provided, will try to get one.

    Returns:
        Workspace ID if found, None otherwise.
        If input looks like a UUID, returns it unchanged.

    Side effects:
        Populates the ID cache with all discovered workspace mappings.
    """
    # If it looks like a UUID, assume it's already an ID
    if _looks_like_uuid(workspace_slug_or_id):
        return workspace_slug_or_id

    # Need to resolve slug - get access token if not provided
    if not access_token:
        access_token = ensure_access_token(registry, org_id, quiet=True)
        if not access_token:
            return None

    # Get registry URL
    registry_url = get_registry_url(registry)
    if not registry_url:
        return None

    # Fetch workspaces and find matching slug
    workspaces = _get_workspaces(registry_url, access_token, org_id)
    result = None
    for ws in workspaces:
        ws_id = ws.get("id")
        ws_slug = ws.get("slug")
        # Cache all discovered workspaces
        if ws_id and ws_slug:
            cache_workspace_id(registry, org_id, ws_slug, ws_id)
        # Check if this is the one we're looking for
        if ws_slug == workspace_slug_or_id or ws_id == workspace_slug_or_id:
            result = ws_id

    return result


def _get_fresh_oidc_token(registry: str) -> str | None:
    """Get a fresh OIDC access token by refreshing.

    Returns the OIDC access token or None if refresh fails.
    """
    creds = load_credentials(registry)
    if not creds:
        return None

    token_endpoint = creds.get("token_endpoint")
    refresh_token = creds.get("refresh_token")
    client_id = creds.get("client_id")

    if not token_endpoint or not refresh_token or not client_id:
        return None

    try:
        tokens = _refresh_oidc_token(token_endpoint, refresh_token, client_id)

        # Update stored refresh token if a new one was provided
        if tokens.get("refresh_token"):
            creds["refresh_token"] = tokens["refresh_token"]
            save_credentials(creds, registry)

        return tokens.get("access_token")
    except Exception:
        return None
