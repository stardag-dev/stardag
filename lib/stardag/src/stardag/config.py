"""Centralized configuration for Stardag SDK.

This module provides a unified configuration system that consolidates:
- Target factory settings (target roots)
- API registry settings (URL, timeout, workspace)
- Active context (profile, organization, workspace)

Configuration is loaded from multiple sources with the following priority:
1. Environment variables (STARDAG_*)
2. Project config (.stardag/config.json in working directory or parents)
3. Profile config (~/.stardag/profiles/{profile}/config.json)
4. Defaults

Usage:
    from stardag.config import get_config

    config = get_config()
    print(config.api.url)
    print(config.target.roots)
"""

from __future__ import annotations

import json
import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

# --- Constants ---

DEFAULT_TARGET_ROOT_KEY = "default"
DEFAULT_TARGET_ROOT = str(
    Path("~/.stardag/target-roots/default").expanduser().absolute()
)
DEFAULT_API_URL = "http://localhost:8000"
DEFAULT_API_TIMEOUT = 30.0
DEFAULT_PROFILE = "local"


# --- Path utilities ---


def get_stardag_dir() -> Path:
    """Get the user's stardag config directory (~/.stardag)."""
    return Path.home() / ".stardag"


def get_profiles_dir() -> Path:
    """Get the profiles directory (~/.stardag/profiles)."""
    return get_stardag_dir() / "profiles"


def get_active_profile_path() -> Path:
    """Get the path to the active profile file."""
    return get_stardag_dir() / "active_profile"


def get_profile_dir(profile: str) -> Path:
    """Get the directory for a specific profile."""
    return get_profiles_dir() / profile


def get_profile_config_path(profile: str) -> Path:
    """Get the config file path for a specific profile."""
    return get_profile_dir(profile) / "config.json"


def get_profile_credentials_path(profile: str) -> Path:
    """Get the credentials file path for a specific profile."""
    return get_profile_dir(profile) / "credentials.json"


def get_profile_active_workspace_path(profile: str) -> Path:
    """Get the active_workspace file path for a specific profile."""
    return get_profile_dir(profile) / "active_workspace"


def get_profile_workspaces_dir(profile: str) -> Path:
    """Get the workspaces directory for a specific profile."""
    return get_profile_dir(profile) / "workspaces"


def get_workspace_dir(profile: str, workspace_id: str) -> Path:
    """Get the directory for a specific workspace within a profile."""
    return get_profile_workspaces_dir(profile) / workspace_id


def get_workspace_target_roots_path(profile: str, workspace_id: str) -> Path:
    """Get the target_roots.json path for a specific workspace."""
    return get_workspace_dir(profile, workspace_id) / "target_roots.json"


def find_project_config() -> Path | None:
    """Find .stardag/config.json in current directory or parents.

    Returns:
        Path to project config if found, None otherwise.
    """
    current = Path.cwd()
    for directory in [current, *current.parents]:
        config_path = directory / ".stardag" / "config.json"
        if config_path.exists():
            return config_path
    return None


# --- Config file loaders ---


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


def load_active_profile() -> str:
    """Load the active profile name from ~/.stardag/active_profile."""
    path = get_active_profile_path()
    if path.exists():
        try:
            return path.read_text().strip()
        except OSError:
            pass
    return DEFAULT_PROFILE


def save_active_profile(profile: str) -> None:
    """Save the active profile name."""
    path = get_active_profile_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(profile)


def load_active_workspace(profile: str) -> str | None:
    """Load the active workspace ID for a profile.

    Args:
        profile: Profile name.

    Returns:
        Workspace ID if set, None otherwise.
    """
    path = get_profile_active_workspace_path(profile)
    if path.exists():
        try:
            return path.read_text().strip() or None
        except OSError:
            pass
    return None


def save_active_workspace(profile: str, workspace_id: str) -> None:
    """Save the active workspace ID for a profile.

    Args:
        profile: Profile name.
        workspace_id: Workspace ID to save.
    """
    path = get_profile_active_workspace_path(profile)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(workspace_id)


def load_workspace_target_roots(
    profile: str, workspace_id: str, project_dir: Path | None = None
) -> dict[str, str]:
    """Load target roots for a workspace.

    Checks project config first (if project_dir provided), then profile config.

    Args:
        profile: Profile name.
        workspace_id: Workspace ID.
        project_dir: Optional project directory to check for overrides.

    Returns:
        Dict of target root name to URI prefix. Empty dict if not found.
    """
    # Check project-level override first
    if project_dir:
        project_path = (
            project_dir / ".stardag" / "workspaces" / workspace_id / "target_roots.json"
        )
        if project_path.exists():
            return load_json_file(project_path)

    # Fall back to profile config
    profile_path = get_workspace_target_roots_path(profile, workspace_id)
    return load_json_file(profile_path)


def save_workspace_target_roots(
    profile: str, workspace_id: str, target_roots: dict[str, str]
) -> None:
    """Save target roots for a workspace.

    Args:
        profile: Profile name.
        workspace_id: Workspace ID.
        target_roots: Dict of target root name to URI prefix.
    """
    path = get_workspace_target_roots_path(profile, workspace_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(target_roots, f, indent=2)


# --- Pydantic Config Models ---


class TargetConfig(BaseModel):
    """Target factory configuration.

    Attributes:
        roots: Mapping of target root names to URI prefixes.
            Example: {"default": "/path/to/root", "s3": "s3://bucket/prefix"}
    """

    roots: dict[str, str] = Field(
        default_factory=lambda: {DEFAULT_TARGET_ROOT_KEY: DEFAULT_TARGET_ROOT}
    )


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
        profile: Active profile name.
        organization_id: Active organization ID.
        organization_slug: Active organization slug (for validation).
        workspace_id: Active workspace ID.
    """

    profile: str = DEFAULT_PROFILE
    organization_id: str | None = None
    organization_slug: str | None = None
    workspace_id: str | None = None


class ProjectConfig(BaseModel):
    """Project-level configuration (.stardag/config.json in repo).

    Attributes:
        profile: Override default profile for this project.
        organization_id: Override organization for this project.
        workspace_id: Override workspace for this project.
        allowed_organizations: Restrict which orgs can be used (safety check).
    """

    profile: str | None = None
    organization_id: str | None = None
    workspace_id: str | None = None
    allowed_organizations: list[str] | None = None


class StardagSettings(BaseSettings):
    """Top-level settings loaded from environment variables.

    This uses pydantic-settings to read from STARDAG_* environment variables.
    """

    # API settings
    api_url: str | None = None
    api_timeout: float | None = None

    # Target settings
    target_roots: dict[str, str] | None = None

    # Context settings
    profile: str | None = None
    organization_id: str | None = None
    workspace_id: str | None = None

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
    all sources (env vars, project config, profile config, defaults).
    """

    target: TargetConfig = Field(default_factory=TargetConfig)
    api: APIConfig = Field(default_factory=APIConfig)
    context: ContextConfig = Field(default_factory=ContextConfig)

    # Credentials (loaded separately, not from env vars)
    access_token: str | None = None
    api_key: str | None = None

    # Project restrictions (from .stardag/config.json)
    allowed_organizations: list[str] | None = None

    def validate_organization(self, org_id: str | None = None) -> None:
        """Validate that the organization is allowed by project config.

        Args:
            org_id: Organization ID to validate. Uses context.organization_id if None.

        Raises:
            ValueError: If organization is not in allowed_organizations.
        """
        org = org_id or self.context.organization_id
        org_slug = self.context.organization_slug
        if self.allowed_organizations and org:
            # Check if org ID or slug matches any allowed organization
            if org not in self.allowed_organizations and (
                not org_slug or org_slug not in self.allowed_organizations
            ):
                raise ValueError(
                    f"Organization '{org}' is not allowed by project config. "
                    f"Allowed: {self.allowed_organizations}"
                )


# --- Config loading ---


def load_config(
    profile: str | None = None,
    use_project_config: bool = True,
) -> StardagConfig:
    """Load configuration from all sources.

    Priority (highest to lowest):
    1. Environment variables (STARDAG_*)
    2. Project config (.stardag/config.json in repo)
    3. Profile config (~/.stardag/profiles/{profile}/...)
    4. Defaults

    Args:
        profile: Profile name to load. If None, uses active profile or default.
        use_project_config: Whether to load .stardag/config.json from project.

    Returns:
        Fully resolved StardagConfig.
    """
    # 1. Load env vars first (highest priority for determining profile)
    env_settings = StardagSettings()

    # 2. Determine active profile
    effective_profile = (
        env_settings.profile  # Env var override
        or profile  # Explicit argument
        or load_active_profile()  # From ~/.stardag/active_profile
    )

    # 3. Load project config (if enabled)
    project_config = ProjectConfig()
    project_dir: Path | None = None
    if use_project_config:
        project_path = find_project_config()
        if project_path:
            project_dir = (
                project_path.parent.parent
            )  # .stardag/config.json -> project root
            project_data = load_json_file(project_path)
            project_config = ProjectConfig.model_validate(project_data)
            # Project can override profile
            if project_config.profile:
                effective_profile = project_config.profile

    # 4. Load profile config and credentials
    profile_config_path = get_profile_config_path(effective_profile)
    profile_data = load_json_file(profile_config_path)

    profile_creds_path = get_profile_credentials_path(effective_profile)
    profile_creds = load_json_file(profile_creds_path)

    # 5. Merge everything (env vars > project > profile > defaults)

    # API settings
    api_url = (
        env_settings.api_url
        or profile_data.get("api_url")
        or profile_data.get("api", {}).get("url")
        or DEFAULT_API_URL
    )

    api_timeout = (
        env_settings.api_timeout
        or profile_data.get("api_timeout")
        or profile_data.get("api", {}).get("timeout")
        or DEFAULT_API_TIMEOUT
    )

    # Context (org/workspace)
    organization_id = (
        env_settings.organization_id
        or project_config.organization_id
        or profile_data.get("organization_id")
    )

    organization_slug = profile_data.get("organization_slug")

    # Workspace from: env > project config > active_workspace file > profile config (legacy)
    workspace_id = (
        env_settings.workspace_id
        or project_config.workspace_id
        or load_active_workspace(effective_profile)
        or profile_data.get("workspace_id")  # Legacy fallback
    )

    # Target roots from workspace-specific file (with project override)
    target_roots: dict[str, str]
    if env_settings.target_roots:
        target_roots = env_settings.target_roots
    elif workspace_id:
        target_roots = load_workspace_target_roots(
            effective_profile, workspace_id, project_dir
        )
        if not target_roots:
            # Legacy fallback: check profile config
            target_roots = (
                profile_data.get("target_roots")
                or profile_data.get("target", {}).get("roots")
                or {DEFAULT_TARGET_ROOT_KEY: DEFAULT_TARGET_ROOT}
            )
    else:
        # No workspace - use legacy config or defaults
        target_roots = (
            profile_data.get("target_roots")
            or profile_data.get("target", {}).get("roots")
            or {DEFAULT_TARGET_ROOT_KEY: DEFAULT_TARGET_ROOT}
        )

    # Credentials
    access_token = profile_creds.get("access_token")
    api_key = env_settings.api_key or os.environ.get("STARDAG_API_KEY")

    return StardagConfig(
        target=TargetConfig(roots=target_roots),
        api=APIConfig(url=api_url, timeout=api_timeout),
        context=ContextConfig(
            profile=effective_profile,
            organization_id=organization_id,
            organization_slug=organization_slug,
            workspace_id=workspace_id,
        ),
        access_token=access_token,
        api_key=api_key,
        allowed_organizations=project_config.allowed_organizations,
    )


@lru_cache(maxsize=1)
def get_config() -> StardagConfig:
    """Get the cached global configuration.

    This loads configuration once and caches it. Use clear_config_cache()
    to force a reload.

    Returns:
        The global StardagConfig instance.
    """
    return load_config()


def clear_config_cache() -> None:
    """Clear the cached configuration, forcing reload on next get_config()."""
    get_config.cache_clear()


# --- Config provider for dependency injection ---


class ConfigProvider:
    """Provider for StardagConfig that supports dependency injection.

    This allows tests and advanced use cases to override the config.
    """

    def __init__(self) -> None:
        self._override: StardagConfig | None = None

    def get(self) -> StardagConfig:
        """Get the current configuration."""
        if self._override is not None:
            return self._override
        return get_config()

    def set(self, config: StardagConfig) -> None:
        """Override the configuration."""
        self._override = config

    def reset(self) -> None:
        """Reset to default configuration loading."""
        self._override = None
        clear_config_cache()


# Global config provider instance
config_provider = ConfigProvider()
