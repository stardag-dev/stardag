"""Configuration loading and merging logic."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from stardag.config.cache import (
    _looks_like_uuid,
    get_cached_environment_id,
    get_cached_target_roots,
    get_cached_workspace_id,
)
from stardag.config.io import load_json_file, load_toml_file
from stardag.config.models import (
    ConfigContext,
    RegistryAuth,
    RegistryConfig,
    StardagConfig,
    StardagSettings,
    TargetConfig,
    TomlConfig,
)
from stardag.config.paths import (
    DEFAULT_API_TIMEOUT,
    DEFAULT_TARGET_ROOT,
    DEFAULT_TARGET_ROOT_KEY,
    find_project_config,
    get_access_token_cache_path,
    get_user_config_path,
)
from stardag.utils.resource_provider import resource_provider

logger = logging.getLogger(__name__)


def _parse_target_roots_from_env() -> dict[str, str] | None:
    """Parse target roots from environment variables.

    Supports two formats (JSON takes precedence):
      STARDAG_TARGET_ROOTS='{"default": "/path", "s3": "s3://bucket"}'
      STARDAG_TARGET_ROOTS__DEFAULT=/path
      STARDAG_TARGET_ROOTS__S3=s3://bucket

    Returns:
        Parsed target roots dict, or None if no env vars are set.
    """
    # JSON format takes precedence
    json_value = os.environ.get("STARDAG_TARGET_ROOTS")
    if json_value:
        try:
            roots = json.loads(json_value)
            if isinstance(roots, dict):
                return roots
        except json.JSONDecodeError:
            logger.warning(
                f"Could not parse STARDAG_TARGET_ROOTS as JSON: {json_value}"
            )

    # Fall back to STARDAG_TARGET_ROOTS__<KEY>=<value> format
    prefix = "STARDAG_TARGET_ROOTS__"
    roots: dict[str, str] = {}
    for key, value in os.environ.items():
        if key.startswith(prefix):
            root_name = key[len(prefix) :].lower()
            if root_name:
                roots[root_name] = value
    return roots if roots else None


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
        Fully resolved StardagConfig (actual type is StardagConfig).
    """
    # 1. Load env vars first (highest priority)
    env_settings = StardagSettings()

    # Short-circuit: STARDAG_NO_REGISTRY forces offline/local mode
    if env_settings.no_registry:
        env_target_roots = _parse_target_roots_from_env()
        target_roots = env_target_roots or {
            DEFAULT_TARGET_ROOT_KEY: DEFAULT_TARGET_ROOT
        }
        return StardagConfig(
            registry=None,
            target=TargetConfig(roots=target_roots),
        )

    # 2. Load user and project TOML configs
    user_toml = load_toml_file(get_user_config_path())
    project_toml = {}
    if use_project_config:
        project_path = find_project_config()
        if project_path:
            project_toml = load_toml_file(project_path)

    # Merge configs (project overrides user)
    toml_config = _merge_toml_configs(user_toml, project_toml)

    # 3. Resolve profile -> (registry, user, workspace, environment)
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

    # 4. Resolve target roots
    # Priority: env > cached > default
    target_roots: dict[str, str]
    env_target_roots = _parse_target_roots_from_env()
    if env_target_roots:
        target_roots = env_target_roots
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
    # If token is expired, try to refresh it automatically
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

        # If no valid token in cache, try to refresh it
        if not access_token:
            try:
                from stardag.registry._auth import (
                    ensure_access_token as _ensure_token,
                )

                access_token = _ensure_token(
                    registry_name, workspace_id, user, registry_url=registry_url
                )
            except Exception:
                # Silently fail - user can manually refresh with `stardag auth refresh`
                pass

    # 6. Get API key from env
    api_key = env_settings.api_key or os.environ.get("STARDAG_API_KEY")

    # 7. Build canonical RegistryConfig (or None for offline mode)
    registry_cfg: RegistryConfig | None = None
    if registry_url:
        registry_cfg = RegistryConfig(
            url=registry_url,
            workspace_id=workspace_id or "",
            environment_id=environment_id or "",
            auth=RegistryAuth(
                api_key=api_key,
                user_email=user,
                access_token=access_token,
            ),
            timeout=env_settings.api_timeout or DEFAULT_API_TIMEOUT,
        )

    return StardagConfig(
        registry=registry_cfg,
        target=TargetConfig(roots=target_roots),
        context=ConfigContext(
            profile=profile_name,
            registry_name=registry_name,
        ),
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
