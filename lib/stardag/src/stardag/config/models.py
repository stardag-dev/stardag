"""Pydantic configuration models for Stardag."""

from __future__ import annotations

import os
from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from stardag.config.paths import (
    DEFAULT_API_TIMEOUT,
    DEFAULT_TARGET_ROOT,
    DEFAULT_TARGET_ROOT_KEY,
)


# --- TOML Config Models ---


class RegistryConfig(BaseModel):
    """Registry configuration from TOML.

    Attributes:
        url: Base URL of the Stardag API registry.
    """

    url: str


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


# --- Target Config ---


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


# --- API Config ---


class APIConfig(BaseModel):
    """API registry configuration.

    Attributes:
        url: Base URL of the Stardag API.
        timeout: Request timeout in seconds.
    """

    url: str | None = None
    timeout: float = DEFAULT_API_TIMEOUT


# --- Context Config ---


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


# --- Environment Settings ---


class StardagSettings(BaseSettings):
    """Top-level settings loaded from environment variables.

    This uses pydantic-settings to read from STARDAG_* environment variables.

    Note: target_roots is handled manually in load_config() to support both
    STARDAG_TARGET_ROOTS='{"key": "val"}' (JSON) and
    STARDAG_TARGET_ROOTS__KEY=val (per-key) formats without relying on
    pydantic-settings' env_nested_delimiter (which has compatibility issues).
    """

    # Profile (looks up registry/workspace/environment from config.toml)
    profile: str | None = None

    # Direct overrides (bypass profile)
    registry_url: str | None = None
    workspace_id: str | None = None
    environment_id: str | None = None

    # API settings
    api_timeout: float | None = None

    # API key
    api_key: str | None = None

    model_config = SettingsConfigDict(
        env_prefix="STARDAG_",
        extra="ignore",
    )


# --- Unified Config ---


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
