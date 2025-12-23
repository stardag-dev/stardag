"""Centralized configuration for Stardag SDK.

This module provides a unified configuration system that consolidates:
- Target factory settings (target roots)
- API registry settings (URL, timeout, workspace)
- Active context (registry, organization, workspace)

Configuration is loaded from multiple sources with the following priority:
1. Environment variables (STARDAG_*)
2. Project config (.stardag/config.json in working directory or parents)
3. Registry config (~/.stardag/registries/{registry}/config.json)
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
DEFAULT_REGISTRY = "local"


# --- Path utilities ---


def get_stardag_dir() -> Path:
    """Get the user's stardag config directory (~/.stardag)."""
    return Path.home() / ".stardag"


def get_registries_dir() -> Path:
    """Get the registries directory (~/.stardag/registries)."""
    return get_stardag_dir() / "registries"


def get_active_registry_path() -> Path:
    """Get the path to the active registry file."""
    return get_stardag_dir() / "active_registry"


def get_registry_dir(registry: str) -> Path:
    """Get the directory for a specific registry."""
    return get_registries_dir() / registry


def get_registry_config_path(registry: str) -> Path:
    """Get the config file path for a specific registry."""
    return get_registry_dir(registry) / "config.json"


def get_registry_credentials_path(registry: str) -> Path:
    """Get the credentials file path for a specific registry."""
    return get_registry_dir(registry) / "credentials.json"


def get_registry_active_workspace_path(registry: str) -> Path:
    """Get the active_workspace file path for a specific registry."""
    return get_registry_dir(registry) / "active_workspace"


def get_registry_workspaces_dir(registry: str) -> Path:
    """Get the workspaces directory for a specific registry."""
    return get_registry_dir(registry) / "workspaces"


def get_workspace_dir(registry: str, workspace_id: str) -> Path:
    """Get the directory for a specific workspace within a registry."""
    return get_registry_workspaces_dir(registry) / workspace_id


def get_workspace_target_roots_path(registry: str, workspace_id: str) -> Path:
    """Get the target_roots.json path for a specific workspace."""
    return get_workspace_dir(registry, workspace_id) / "target_roots.json"


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


def load_active_registry() -> str:
    """Load the active registry name from ~/.stardag/active_registry."""
    path = get_active_registry_path()
    if path.exists():
        try:
            return path.read_text().strip()
        except OSError:
            pass
    return DEFAULT_REGISTRY


def save_active_registry(registry: str) -> None:
    """Save the active registry name."""
    path = get_active_registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(registry)


def load_active_workspace(registry: str) -> str | None:
    """Load the active workspace ID for a registry.

    Args:
        registry: Registry name.

    Returns:
        Workspace ID if set, None otherwise.
    """
    path = get_registry_active_workspace_path(registry)
    if path.exists():
        try:
            return path.read_text().strip() or None
        except OSError:
            pass
    return None


def save_active_workspace(registry: str, workspace_id: str) -> None:
    """Save the active workspace ID for a registry.

    Args:
        registry: Registry name.
        workspace_id: Workspace ID to save.
    """
    path = get_registry_active_workspace_path(registry)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(workspace_id)


def load_workspace_target_roots(registry: str, workspace_id: str) -> dict[str, str]:
    """Load target roots for a workspace from registry config.

    Note: Project-level overrides are handled in load_config() via
    ProjectConfig.get_workspace_target_roots().

    Args:
        registry: Registry name.
        workspace_id: Workspace ID.

    Returns:
        Dict of target root name to URI prefix. Empty dict if not found.
    """
    registry_path = get_workspace_target_roots_path(registry, workspace_id)
    return load_json_file(registry_path)


def save_workspace_target_roots(
    registry: str, workspace_id: str, target_roots: dict[str, str]
) -> None:
    """Save target roots for a workspace.

    Args:
        registry: Registry name.
        workspace_id: Workspace ID.
        target_roots: Dict of target root name to URI prefix.
    """
    path = get_workspace_target_roots_path(registry, workspace_id)
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
        registry: Active registry name.
        organization_id: Active organization ID.
        organization_slug: Active organization slug (for validation).
        workspace_id: Active workspace ID.
    """

    registry: str = DEFAULT_REGISTRY
    organization_id: str | None = None
    organization_slug: str | None = None
    workspace_id: str | None = None


class ProjectWorkspaceConfig(BaseModel):
    """Project-level workspace configuration.

    Attributes:
        target_roots: Target roots for this workspace.
    """

    target_roots: dict[str, str] | None = None


class ProjectRegistryConfig(BaseModel):
    """Project-level registry configuration.

    Attributes:
        organization_id: Organization for this registry.
        default_workspace: Default workspace for this registry.
        workspaces: Per-workspace settings.
    """

    organization_id: str | None = None
    default_workspace: str | None = None
    workspaces: dict[str, ProjectWorkspaceConfig] = Field(default_factory=dict)


class ProjectConfig(BaseModel):
    """Project-level configuration (.stardag/config.json in repo).

    Example:
        {
            "default_registry": "central",
            "allowed_organizations": ["my-org"],
            "registries": {
                "local": {
                    "organization_id": "local-org",
                    "default_workspace": "dev",
                    "workspaces": {
                        "dev": {"target_roots": {"default": "/local/data"}}
                    }
                },
                "central": {
                    "organization_id": "my-org",
                    "workspaces": {
                        "prod": {"target_roots": {"default": "s3://bucket/prod/"}}
                    }
                }
            }
        }
    """

    default_registry: str | None = None
    registries: dict[str, ProjectRegistryConfig] = Field(default_factory=dict)
    allowed_organizations: list[str] | None = None

    def get_registry_config(self, registry: str) -> ProjectRegistryConfig | None:
        """Get registry-specific config if it exists."""
        return self.registries.get(registry)

    def get_organization_id(self, registry: str) -> str | None:
        """Get organization ID for a registry."""
        registry_config = self.registries.get(registry)
        if registry_config:
            return registry_config.organization_id
        return None

    def get_workspace_id(self, registry: str) -> str | None:
        """Get default workspace ID for a registry."""
        registry_config = self.registries.get(registry)
        if registry_config:
            return registry_config.default_workspace
        return None

    def get_workspace_target_roots(
        self, registry: str, workspace_id: str
    ) -> dict[str, str] | None:
        """Get target roots for a specific workspace in a registry."""
        registry_config = self.registries.get(registry)
        if registry_config:
            ws_config = registry_config.workspaces.get(workspace_id)
            if ws_config and ws_config.target_roots:
                return ws_config.target_roots
        return None


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
    registry: str | None = None
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
    all sources (env vars, project config, registry config, defaults).
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
    registry: str | None = None,
    use_project_config: bool = True,
) -> StardagConfig:
    """Load configuration from all sources.

    Priority (highest to lowest):
    1. Environment variables (STARDAG_*)
    2. Project config (.stardag/config.json in repo)
    3. Registry config (~/.stardag/registries/{registry}/...)
    4. Defaults

    Args:
        registry: Registry name to load. If None, uses active registry or default.
        use_project_config: Whether to load .stardag/config.json from project.

    Returns:
        Fully resolved StardagConfig.
    """
    # 1. Load env vars first (highest priority for determining registry)
    env_settings = StardagSettings()

    # 2. Determine active registry
    effective_registry = (
        env_settings.registry  # Env var override
        or registry  # Explicit argument
        or load_active_registry()  # From ~/.stardag/active_registry
    )

    # 3. Load project config (if enabled)
    project_config = ProjectConfig()
    if use_project_config:
        project_path = find_project_config()
        if project_path:
            project_data = load_json_file(project_path)
            project_config = ProjectConfig.model_validate(project_data)
            # Project can override registry
            if project_config.default_registry:
                effective_registry = project_config.default_registry

    # 4. Load registry config and credentials
    registry_config_path = get_registry_config_path(effective_registry)
    registry_data = load_json_file(registry_config_path)

    registry_creds_path = get_registry_credentials_path(effective_registry)
    registry_creds = load_json_file(registry_creds_path)

    # 5. Merge everything (env vars > project > registry > defaults)

    # API settings
    api_url = (
        env_settings.api_url
        or registry_data.get("api_url")
        or registry_data.get("api", {}).get("url")
        or DEFAULT_API_URL
    )

    api_timeout = (
        env_settings.api_timeout
        or registry_data.get("api_timeout")
        or registry_data.get("api", {}).get("timeout")
        or DEFAULT_API_TIMEOUT
    )

    # Context (org/workspace) - use project config methods for nested support
    organization_id = (
        env_settings.organization_id
        or project_config.get_organization_id(effective_registry)
        or registry_data.get("organization_id")
    )

    organization_slug = registry_data.get("organization_slug")

    # Workspace from: env > project config > active_workspace file > registry config (legacy)
    workspace_id = (
        env_settings.workspace_id
        or project_config.get_workspace_id(effective_registry)
        or load_active_workspace(effective_registry)
        or registry_data.get("workspace_id")  # Legacy fallback
    )

    # Target roots resolution order:
    # 1. Environment variables
    # 2. Project config nested structure (registries.{registry}.workspaces.{ws}.target_roots)
    # 3. Registry workspace file (~/.stardag/registries/{registry}/workspaces/{ws}/target_roots.json)
    # 4. Legacy registry config (target_roots in config.json)
    # 5. Defaults
    target_roots: dict[str, str]
    if env_settings.target_roots:
        target_roots = env_settings.target_roots
    elif workspace_id:
        # Check nested project config first
        project_target_roots = project_config.get_workspace_target_roots(
            effective_registry, workspace_id
        )
        if project_target_roots:
            target_roots = project_target_roots
        else:
            # Check registry workspace file
            target_roots = load_workspace_target_roots(effective_registry, workspace_id)
            if not target_roots:
                # Legacy fallback: check registry config
                target_roots = (
                    registry_data.get("target_roots")
                    or registry_data.get("target", {}).get("roots")
                    or {DEFAULT_TARGET_ROOT_KEY: DEFAULT_TARGET_ROOT}
                )
    else:
        # No workspace - use legacy config or defaults
        target_roots = (
            registry_data.get("target_roots")
            or registry_data.get("target", {}).get("roots")
            or {DEFAULT_TARGET_ROOT_KEY: DEFAULT_TARGET_ROOT}
        )

    # Credentials
    access_token = registry_creds.get("access_token")
    api_key = env_settings.api_key or os.environ.get("STARDAG_API_KEY")

    return StardagConfig(
        target=TargetConfig(roots=target_roots),
        api=APIConfig(url=api_url, timeout=api_timeout),
        context=ContextConfig(
            registry=effective_registry,
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
