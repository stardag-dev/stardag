"""Centralized configuration for Stardag SDK.

This module provides a unified configuration system that consolidates:
- Target factory settings (target roots)
- API registry settings (URL, timeout, environment)
- Active context (registry, workspace, environment)

Configuration is loaded from multiple sources with the following priority:
1. Environment variables (STARDAG_*)
2. Project config (.stardag/config.toml in working directory or parents)
3. User config (~/.stardag/config.toml)
4. Defaults

Usage:
    from stardag.config import get_config

    config = get_config()
    print(config.api.url)
    print(config.target.roots)

Environment Variables (highest priority):
    STARDAG_PROFILE          - Profile name to use (looks up in config.toml)
    STARDAG_REGISTRY_URL     - Direct registry URL override
    STARDAG_WORKSPACE_ID     - Direct workspace ID override
    STARDAG_ENVIRONMENT_ID     - Direct environment ID override
    STARDAG_API_KEY          - API key for authentication
    STARDAG_TARGET_ROOTS     - JSON dict of target roots (override)
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from stardag.utils.resource_provider import resource_provider

logger = logging.getLogger(__name__)

# --- Constants ---

DEFAULT_TARGET_ROOT_KEY = "default"
DEFAULT_TARGET_ROOT = str(
    Path("~/.stardag/local-target-roots/default/default").expanduser().absolute()
)
DEFAULT_API_URL = "http://localhost:8000"
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
    - @ → _at_ (email separator)
    - / → _ (Unix path separator)
    - \\ → _ (Windows path separator)
    - : → _ (Windows drive separator, macOS resource fork)

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


# --- TOML loading ---


def load_toml_file(path: Path) -> dict[str, Any]:
    """Load a TOML config file, returning empty dict if not found or invalid."""
    if not path.exists():
        return {}
    try:
        # Python 3.11+ has tomllib built-in
        if sys.version_info >= (3, 11):
            import tomllib

            with open(path, "rb") as f:
                return tomllib.load(f)
        else:
            # Fall back to tomli for older Python
            try:
                import tomli

                with open(path, "rb") as f:
                    return tomli.load(f)
            except ImportError:
                logger.warning(
                    f"tomli not installed, cannot load {path}. "
                    "Install with: pip install tomli"
                )
                return {}
    except Exception as e:
        logger.debug(f"Could not load {path}: {e}")
        return {}


def save_toml_file(path: Path, data: dict[str, Any]) -> None:
    """Save data to a TOML file."""
    try:
        import tomli_w  # type: ignore[import-not-found]

        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            tomli_w.dump(data, f)
    except ImportError:
        raise ImportError(
            "tomli-w is required to write TOML files. Install with: pip install tomli-w"
        )


def load_json_file(path: Path) -> dict[str, Any]:
    """Load a JSON config file, returning empty dict if not found or invalid."""
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.debug(f"Could not load {path}: {e}")
        return {}


def save_json_file(path: Path, data: dict[str, Any]) -> None:
    """Save data to a JSON file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


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
    import re

    uuid_pattern = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
    return bool(re.match(uuid_pattern, value.lower()))


# --- Pydantic Config Models ---


class RegistryConfig(BaseModel):
    """Registry configuration from TOML.

    Attributes:
        url: Base URL of the Stardag API registry.
    """

    url: str = DEFAULT_API_URL


class ProfileConfig(BaseModel):
    """Profile configuration from TOML.

    A profile defines the (registry, user, workspace, environment) tuple.

    Attributes:
        registry: Name of the registry to use.
        user: User identifier (email) for credential lookup. Optional for
            backward compatibility - if not set, uses registry-level credentials.
        workspace: Workspace ID or slug.
        environment: Environment ID or slug.
    """

    registry: str
    user: str | None = None
    workspace: str
    environment: str


class TomlConfig(BaseModel):
    """Parsed TOML configuration.

    Attributes:
        registry: Dict of registry name to RegistryConfig.
        profile: Dict of profile name to ProfileConfig.
        default: Default settings (e.g., default profile).
    """

    registry: dict[str, RegistryConfig] = Field(default_factory=dict)
    profile: dict[str, ProfileConfig] = Field(default_factory=dict)
    default: dict[str, str] = Field(default_factory=dict)

    @classmethod
    def from_toml_dict(cls, data: dict[str, Any]) -> "TomlConfig":
        """Parse a TOML dict into a TomlConfig."""
        registries = {}
        profiles = {}

        for key, value in data.get("registry", {}).items():
            if isinstance(value, dict) and "url" in value:
                registries[key] = RegistryConfig(url=value["url"])

        for key, value in data.get("profile", {}).items():
            if isinstance(value, dict):
                # Support both "workspace" and legacy "organization" keys
                workspace_value = value.get("workspace") or value.get("organization")
                if (
                    all(k in value for k in ("registry", "environment"))
                    and workspace_value
                ):
                    profiles[key] = ProfileConfig(
                        registry=value["registry"],
                        user=value.get("user"),  # Optional user field
                        workspace=workspace_value,
                        environment=value["environment"],
                    )

        default = data.get("default", {})
        if not isinstance(default, dict):
            default = {}

        return cls(registry=registries, profile=profiles, default=default)


def _expand_tilde_in_roots(roots: dict[str, str]) -> dict[str, str]:
    """Expand ~ to user home directory in target root paths."""
    return {
        name: os.path.expanduser(uri) if uri.startswith("~/") else uri
        for name, uri in roots.items()
    }


TargetRoots = Annotated[dict[str, str], AfterValidator(_expand_tilde_in_roots)]


class TargetConfig(BaseModel):
    """Target factory configuration.

    Attributes:
        roots: Mapping of target root names to URI prefixes.
            Example: {"default": "/path/to/root", "s3": "s3://bucket/prefix"}
            Paths starting with ~/ are automatically expanded to the user's home directory.
    """

    roots: TargetRoots = {DEFAULT_TARGET_ROOT_KEY: DEFAULT_TARGET_ROOT}


class APIConfig(BaseModel):
    """API registry configuration.

    Attributes:
        url: Base URL of the Stardag API.
        timeout: Request timeout in seconds.
    """

    url: str = DEFAULT_API_URL
    timeout: float = DEFAULT_API_TIMEOUT


class ContextConfig(BaseModel):
    """Active context configuration.

    Attributes:
        profile: Active profile name (if using profile-based config).
        registry_name: Registry name from config (for credential lookup).
        user: User identifier (email) for credential lookup.
        workspace_id: Active workspace ID.
        environment_id: Active environment ID.
    """

    profile: str | None = None
    registry_name: str | None = None
    user: str | None = None
    workspace_id: str | None = None
    environment_id: str | None = None


class StardagSettings(BaseSettings):
    """Top-level settings loaded from environment variables.

    This uses pydantic-settings to read from STARDAG_* environment variables.
    """

    # Profile (looks up registry/workspace/environment from config.toml)
    profile: str | None = None

    # Direct overrides (bypass profile)
    registry_url: str | None = None
    workspace_id: str | None = None
    environment_id: str | None = None

    # Target settings
    target_roots: dict[str, str] | None = None

    # API settings
    api_timeout: float | None = None

    # API key
    api_key: str | None = None

    model_config = SettingsConfigDict(
        env_prefix="STARDAG_",
        env_nested_delimiter="__",
        extra="ignore",
    )


class StardagConfig(BaseModel):
    """Unified Stardag configuration.

    This is the main configuration object that combines settings from
    all sources (env vars, project config, user config, defaults).
    """

    target: TargetConfig = Field(default_factory=TargetConfig)
    api: APIConfig = Field(default_factory=APIConfig)
    context: ContextConfig = Field(default_factory=ContextConfig)

    # Credentials (loaded separately, not from env vars)
    access_token: str | None = None
    api_key: str | None = None


# --- Config loading ---


def _merge_toml_configs(
    user_config: dict[str, Any], project_config: dict[str, Any]
) -> TomlConfig:
    """Merge user and project TOML configs.

    Project config takes precedence over user config.
    """
    # Start with user config
    merged_registries = dict(user_config.get("registry", {}))
    merged_profiles = dict(user_config.get("profile", {}))
    merged_default = dict(user_config.get("default", {}))

    # Override with project config
    merged_registries.update(project_config.get("registry", {}))
    merged_profiles.update(project_config.get("profile", {}))
    merged_default.update(project_config.get("default", {}))

    return TomlConfig.from_toml_dict(
        {
            "registry": merged_registries,
            "profile": merged_profiles,
            "default": merged_default,
        }
    )


def load_config(
    use_project_config: bool = True,
) -> StardagConfig:
    """Load configuration from all sources.

    Priority (highest to lowest):
    1. Environment variables (STARDAG_*)
    2. Project config (.stardag/config.toml in repo)
    3. User config (~/.stardag/config.toml)
    4. Defaults

    Args:
        use_project_config: Whether to load .stardag/config.toml from project.

    Returns:
        Fully resolved StardagConfig.
    """
    # 1. Load env vars first (highest priority)
    env_settings = StardagSettings()

    # 2. Load user and project TOML configs
    user_toml = load_toml_file(get_user_config_path())
    project_toml = {}
    if use_project_config:
        project_path = find_project_config()
        if project_path:
            project_toml = load_toml_file(project_path)

    # Merge configs (project overrides user)
    toml_config = _merge_toml_configs(user_toml, project_toml)

    # 3. Resolve profile → (registry, user, workspace, environment)
    profile_name: str | None = None
    registry_name: str | None = None
    registry_url: str | None = None
    user: str | None = None
    workspace_id: str | None = None
    environment_id: str | None = None

    # Check for direct env var overrides first
    if env_settings.registry_url:
        registry_url = env_settings.registry_url
        workspace_id = env_settings.workspace_id
        environment_id = env_settings.environment_id
    # Then check for profile-based config
    elif env_settings.profile:
        profile_name = env_settings.profile
    # Fall back to default profile from config
    elif toml_config.default.get("profile"):
        profile_name = toml_config.default["profile"]

    # If we have a profile, look it up
    if profile_name and not registry_url:
        profile = toml_config.profile.get(profile_name)
        if profile:
            registry_name = profile.registry
            user = profile.user  # Optional user for multi-user support
            workspace_value = profile.workspace  # Could be slug or ID
            environment_value = profile.environment  # Could be slug or ID

            # Look up registry URL from registry name
            registry_config = toml_config.registry.get(registry_name)
            if registry_config:
                registry_url = registry_config.url
            else:
                logger.warning(
                    f"Profile '{profile_name}' references unknown registry '{registry_name}'"
                )

            # Resolve workspace slug to ID if needed
            if _looks_like_uuid(workspace_value):
                workspace_id = workspace_value
            else:
                # Try to resolve from cache
                cached_workspace_id = get_cached_workspace_id(
                    registry_name, workspace_value
                )
                if cached_workspace_id:
                    workspace_id = cached_workspace_id
                else:
                    # Store the slug - will need to be resolved at runtime
                    workspace_id = workspace_value
                    logger.debug(
                        f"Workspace '{workspace_value}' is a slug, not cached. "
                        "Run 'stardag auth refresh' to resolve."
                    )

            # Resolve environment slug to ID if needed
            if _looks_like_uuid(environment_value):
                environment_id = environment_value
            elif workspace_id and _looks_like_uuid(workspace_id):
                # Can only resolve environment if we have a resolved workspace ID
                cached_env_id = get_cached_environment_id(
                    registry_name, workspace_id, environment_value
                )
                if cached_env_id:
                    environment_id = cached_env_id
                else:
                    # Store the slug - will need to be resolved at runtime
                    environment_id = environment_value
                    logger.debug(
                        f"Environment '{environment_value}' is a slug, not cached. "
                        "Run 'stardag auth refresh' to resolve."
                    )
            else:
                # Workspace is not resolved, can't resolve environment either
                environment_id = environment_value
        else:
            logger.warning(f"Profile '{profile_name}' not found in config")

    # Apply defaults
    if not registry_url:
        registry_url = DEFAULT_API_URL

    # 4. Resolve target roots
    # Priority: env > cached > default
    target_roots: dict[str, str]
    if env_settings.target_roots:
        target_roots = env_settings.target_roots
    elif registry_url and workspace_id and environment_id:
        cached_roots = get_cached_target_roots(
            registry_url, workspace_id, environment_id
        )
        if cached_roots:
            target_roots = cached_roots
        else:
            target_roots = {DEFAULT_TARGET_ROOT_KEY: DEFAULT_TARGET_ROOT}
    else:
        target_roots = {DEFAULT_TARGET_ROOT_KEY: DEFAULT_TARGET_ROOT}

    # 5. Load access token from cache (if we have profile info)
    access_token: str | None = None
    if registry_name and workspace_id and user:
        token_cache_path = get_access_token_cache_path(
            registry_name, workspace_id, user
        )
        if token_cache_path.exists():
            token_data = load_json_file(token_cache_path)
            # Check if token is still valid
            import time

            expires_at = token_data.get("expires_at", 0)
            if expires_at > time.time():
                access_token = token_data.get("access_token")

    # 6. Get API key from env
    api_key = env_settings.api_key or os.environ.get("STARDAG_API_KEY")

    return StardagConfig(
        target=TargetConfig(roots=target_roots),
        api=APIConfig(
            url=registry_url,
            timeout=env_settings.api_timeout or DEFAULT_API_TIMEOUT,
        ),
        context=ContextConfig(
            profile=profile_name,
            registry_name=registry_name,
            user=user,
            workspace_id=workspace_id,
            environment_id=environment_id,
        ),
        access_token=access_token,
        api_key=api_key,
    )


config_provider = resource_provider(StardagConfig, default_factory=load_config)


def get_config() -> StardagConfig:
    """Get the cached global configuration.

    This loads configuration once and caches it. Use clear_config_cache()
    to force a reload.

    Returns:
        The global StardagConfig instance.
    """
    return config_provider.get()


def clear_config_cache() -> None:
    """Clear the cached configuration, forcing reload on next get_config()."""
    config_provider.clear()
