"""Credential and configuration storage for Stardag CLI.

Storage model:
- Credentials (refresh tokens): ~/.stardag/credentials/{registry}__{user}.json
- Access token cache: ~/.stardag/access-token-cache/{registry}__{user}__{workspace}.json
- Config in ~/.stardag/config.toml (TOML format)
- Target root cache in ~/.stardag/target-root-cache.json

Multi-user support:
- Profiles include a 'user' field (email) identifying the logged-in user
- Credentials are stored per (registry, user) to support multiple identities
- This allows switching between personal and work accounts on the same machine
"""

from pathlib import Path
from typing import TypedDict

from stardag.config import (
    TomlConfig,
    TomlRegistryEntry,
    _looks_like_uuid,
    cache_environment_id,
    cache_workspace_id,
    get_config,
    get_config_context,
    get_registry_credentials_path,
    get_user_config_path,
    load_toml_file,
    save_toml_file,
    update_cached_target_roots,
)

# Re-export from registry._auth for backward compatibility
from stardag.registry._auth import (
    AccessTokenCache as AccessTokenCache,
    Credentials as Credentials,
    ensure_access_token as _ensure_access_token_core,
    exchange_for_internal_token as exchange_for_internal_token,
    get_environments as get_environments,
    get_fresh_oidc_token as get_fresh_oidc_token,
    get_user_workspaces as get_user_workspaces,
    load_access_token_cache as load_access_token_cache,
    load_credentials as _load_credentials_core,
    save_access_token_cache as save_access_token_cache,
    save_credentials as _save_credentials_core,
)


def _resolve_registry_user(
    registry: str | None, user: str | None
) -> tuple[str | None, str | None]:
    """Resolve registry and user from config defaults when not provided."""
    if registry is None or user is None:
        config = get_config()
        ctx = get_config_context()
        registry = registry or ctx.registry_name
        user = user or (config.registry.auth.user_email if config.registry else None)
    return registry, user


def load_credentials(
    registry: str | None = None, user: str | None = None
) -> Credentials | None:
    """Load credentials from disk.

    Args:
        registry: Registry name. If None, uses active registry from config.
        user: User identifier (email). If None, uses user from active profile.

    Returns:
        Credentials dict if file exists and is valid, None otherwise.
    """
    registry, user = _resolve_registry_user(registry, user)
    if not registry or not user:
        return None
    return _load_credentials_core(registry, user)


def save_credentials(
    credentials: Credentials, registry: str | None = None, user: str | None = None
) -> None:
    """Save credentials to disk.

    Args:
        credentials: Credentials dict to save.
        registry: Registry name. If None, uses active registry from config.
        user: User identifier (email). If None, uses user from active profile.
    """
    registry, user = _resolve_registry_user(registry, user)
    if not registry or not user:
        raise ValueError("No registry/user specified and no active profile")
    _save_credentials_core(credentials, registry, user)


def clear_credentials(registry: str | None = None, user: str | None = None) -> bool:
    """Clear stored credentials.

    Args:
        registry: Registry name. If None, uses active registry from config.
        user: User identifier (email). If None, uses user from active profile.

    Returns:
        True if credentials were cleared, False if no credentials existed.
    """
    registry, user = _resolve_registry_user(registry, user)
    if not registry or not user:
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


# --- Access Token Cache ---

# load_access_token_cache, save_access_token_cache, clear_access_token_cache,
# AccessTokenCache are re-exported from registry._auth at the top of this file.


def clear_access_token_cache(registry: str, workspace_id: str, user: str) -> bool:
    """Clear cached access token."""
    from stardag.config import get_access_token_cache_path

    path = get_access_token_cache_path(registry, workspace_id, user)
    if path.exists():
        path.unlink()
        return True
    return False


def get_access_token(
    registry: str | None = None,
    workspace_id: str | None = None,
    user: str | None = None,
) -> str | None:
    """Get a valid access token, loading from cache.

    Args:
        registry: Registry name. If None, uses active registry.
        workspace_id: Workspace ID. If None, uses active workspace.
        user: User identifier (email). If None, uses user from active profile.

    Returns:
        Access token if available and valid, None otherwise.
    """
    if registry is None or workspace_id is None or user is None:
        config = get_config()
        ctx = get_config_context()
        registry = registry or ctx.registry_name
        workspace_id = workspace_id or (
            config.registry.workspace_id if config.registry else None
        )
        user = user or (config.registry.auth.user_email if config.registry else None)

    if not registry or not workspace_id or not user:
        return None

    cache = load_access_token_cache(registry, workspace_id, user)
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
                "workspace": prof.workspace,
                "environment": prof.environment,
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
        return stardag_config.registry.url if stardag_config.registry else None

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
    config.registry[name] = TomlRegistryEntry(url=url.rstrip("/"))
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
    workspace: str,
    environment: str,
    user: str | None = None,
) -> None:
    """Add or update a profile in config.

    Args:
        name: Profile name.
        registry: Registry name.
        workspace: Workspace ID or slug.
        environment: Environment ID or slug.
        user: User identifier (email). Optional for multi-user support.
    """
    config = load_toml_config()
    from stardag.config import ProfileConfig

    config.profile[name] = ProfileConfig(
        registry=registry,
        user=user,
        workspace=workspace,
        environment=environment,
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
        # Unset default if the removed profile was the default
        if config.default.get("profile") == name:
            del config.default["profile"]
        save_toml_config(config)
        return True
    return False


class ProfileDetails(TypedDict):
    """Profile details structure returned by list_profiles."""

    registry: str
    user: str | None
    workspace: str
    environment: str


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
            workspace=prof.workspace,
            environment=prof.environment,
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
    workspace: str,
    environment: str,
    user: str | None = None,
) -> str | None:
    """Find a profile that matches the given settings.

    Args:
        registry: Registry name.
        workspace: Workspace slug/ID.
        environment: Environment slug/ID.
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
            and details["workspace"] == workspace
            and details["environment"] == environment
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
    workspace_id: str | None = None,
    environment_id: str | None = None,
) -> dict[str, str]:
    """Get target roots from cache.

    Args:
        registry_url: Registry URL. If None, uses active config.
        workspace_id: Workspace ID. If None, uses active config.
        environment_id: Environment ID. If None, uses active config.

    Returns:
        Dict of target root name to URI prefix.
    """
    config = get_config()
    reg = config.registry
    registry_url = registry_url or (reg.url if reg else None)
    workspace_id = workspace_id or (reg.workspace_id if reg else None)
    environment_id = environment_id or (reg.environment_id if reg else None)

    if not registry_url or not workspace_id or not environment_id:
        return {}

    from stardag.config import get_cached_target_roots

    return get_cached_target_roots(registry_url, workspace_id, environment_id) or {}


def set_target_roots(
    target_roots: dict[str, str],
    registry_url: str | None = None,
    workspace_id: str | None = None,
    environment_id: str | None = None,
) -> None:
    """Update target roots in cache.

    Args:
        target_roots: Dict of target root name to URI prefix.
        registry_url: Registry URL. If None, uses active config.
        workspace_id: Workspace ID. If None, uses active config.
        environment_id: Environment ID. If None, uses active config.
    """
    config = get_config()
    reg = config.registry
    registry_url = registry_url or (reg.url if reg else None)
    workspace_id = workspace_id or (reg.workspace_id if reg else None)
    environment_id = environment_id or (reg.environment_id if reg else None)

    if not registry_url or not workspace_id or not environment_id:
        raise ValueError("Registry URL, workspace ID, and environment ID are required")

    update_cached_target_roots(
        registry_url,
        workspace_id,
        environment_id,
        target_roots,
    )


# --- Path convenience functions (for CLI display) ---


def get_credentials_path(registry: str | None = None, user: str | None = None) -> Path:
    """Get the path to the credentials file for display purposes."""
    if registry is None or user is None:
        config = get_config()
        ctx = get_config_context()
        registry = registry or ctx.registry_name or "local"
        user = (
            user
            or (config.registry.auth.user_email if config.registry else None)
            or "unknown"
        )
    return get_registry_credentials_path(registry, user)


def get_config_path() -> Path:
    """Get the path to the config file for display purposes."""
    return get_user_config_path()


# --- Token Refresh Helpers ---

# Core functions (refresh_oidc_token, exchange_for_internal_token,
# ensure_access_token, get_fresh_oidc_token, get_user_workspaces,
# get_environments) are re-exported from registry._auth at the top
# of this file.


def ensure_access_token(
    registry: str,
    workspace_id: str,
    user: str,
    quiet: bool = False,
) -> str | None:
    """Ensure we have a valid access token, refreshing if needed.

    Thin wrapper around registry._auth.ensure_access_token that resolves
    the registry URL from TOML config.

    Args:
        registry: Registry name.
        workspace_id: Workspace ID.
        user: User identifier (email).
        quiet: If True, suppress warning messages.

    Returns:
        Access token if available/refreshed successfully, None otherwise.
    """
    return _ensure_access_token_core(
        registry_name=registry,
        workspace_id=workspace_id,
        user=user,
    )


def resolve_workspace_slug_to_id(
    registry: str,
    workspace_slug_or_id: str,
    user: str | None = None,
    oidc_token: str | None = None,
) -> str | None:
    """Resolve a workspace slug to its ID.

    Args:
        registry: Registry name.
        workspace_slug_or_id: Workspace slug or ID.
        user: User identifier (email). Required if oidc_token not provided.
        oidc_token: Optional OIDC token. If not provided, will try to refresh.

    Returns:
        Workspace ID if found, None otherwise.
        If input looks like a UUID, returns it unchanged.

    Side effects:
        Populates the ID cache with all discovered workspace mappings.
    """
    # If it looks like a UUID, assume it's already an ID
    if _looks_like_uuid(workspace_slug_or_id):
        return workspace_slug_or_id

    # Need to resolve slug - get OIDC token if not provided
    if not oidc_token:
        if not user:
            return None
        oidc_token = get_fresh_oidc_token(registry, user)
        if not oidc_token:
            return None

    # Get registry URL
    registry_url = get_registry_url(registry)
    if not registry_url:
        return None

    # Fetch workspaces and find matching slug
    workspaces = get_user_workspaces(registry_url, oidc_token)
    result = None
    for ws in workspaces:
        ws_id = ws.get("id")
        ws_slug = ws.get("slug")
        # Cache all discovered workspaces
        if ws_id and ws_slug:
            cache_workspace_id(registry, ws_slug, ws_id)
        # Check if this is the one we're looking for
        if ws_slug == workspace_slug_or_id or ws_id == workspace_slug_or_id:
            result = ws_id

    return result


def resolve_environment_slug_to_id(
    registry: str,
    workspace_id: str,
    environment_slug_or_id: str,
    user: str | None = None,
    access_token: str | None = None,
) -> str | None:
    """Resolve an environment slug to its ID.

    Args:
        registry: Registry name.
        workspace_id: Workspace ID (must be resolved already).
        environment_slug_or_id: Environment slug or ID.
        user: User identifier (email). Required if access_token not provided.
        access_token: Optional internal access token. If not provided, will try to get one.

    Returns:
        Environment ID if found, None otherwise.
        If input looks like a UUID, returns it unchanged.

    Side effects:
        Populates the ID cache with all discovered environment mappings.
    """
    # If it looks like a UUID, assume it's already an ID
    if _looks_like_uuid(environment_slug_or_id):
        return environment_slug_or_id

    # Need to resolve slug - get access token if not provided
    if not access_token:
        if not user:
            return None
        access_token = ensure_access_token(registry, workspace_id, user, quiet=True)
        if not access_token:
            return None

    # Get registry URL
    registry_url = get_registry_url(registry)
    if not registry_url:
        return None

    # Fetch environments and find matching slug
    environments = get_environments(registry_url, access_token, workspace_id)
    result = None
    for env in environments:
        env_id = env.get("id")
        env_slug = env.get("slug")
        # Cache all discovered environments
        if env_id and env_slug:
            cache_environment_id(registry, workspace_id, env_slug, env_id)
        # Check if this is the one we're looking for
        if env_slug == environment_slug_or_id or env_id == environment_slug_or_id:
            result = env_id

    return result


# get_fresh_oidc_token is re-exported from registry._auth at the top of this file.
