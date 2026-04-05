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


class TomlRegistryEntry(BaseModel):
    """Registry entry from TOML config.

    Attributes:
        url: Base URL of the Stardag API registry.
    """

    url: str


class ProfileConfig(BaseModel):
    """Profile configuration from TOML.

    A profile defines the (registry, user, workspace, environment) tuple.

    Attributes:
        registry: Name of the registry to use.
        user: User identifier (email) for credential and token cache lookup.
            Required for browser-login (OIDC) authentication. When not set,
            token refresh and credential operations will be skipped.
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
        registry: Dict of registry name to TomlRegistryEntry.
        profile: Dict of profile name to ProfileConfig.
        default: Default settings (e.g., default profile).
    """

    registry: dict[str, TomlRegistryEntry] = Field(default_factory=dict)
    profile: dict[str, ProfileConfig] = Field(default_factory=dict)
    default: dict[str, str] = Field(default_factory=dict)

    @classmethod
    def from_toml_dict(cls, data: dict[str, Any]) -> "TomlConfig":
        """Parse a TOML dict into a TomlConfig."""
        registries = {}
        profiles = {}

        for key, value in data.get("registry", {}).items():
            if isinstance(value, dict) and "url" in value:
                registries[key] = TomlRegistryEntry(url=value["url"])

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


# --- Canonical Registry Config ---


class RegistryAuth(BaseModel):
    """Authentication configuration for the registry.

    Attributes:
        api_key: API key for authentication (production/CI).
        user_email: User identifier (email) for OIDC credential lookup + refresh.
        access_token: JWT access token from browser login (local dev).
    """

    api_key: str | None = None
    user_email: str | None = None
    access_token: str | None = None


class RegistryConfig(BaseModel):
    """Canonical runtime registry configuration.

    This is the canonical representation of the effective registry config,
    combining URL, workspace/environment context, auth, and timeout.

    Attributes:
        url: Base URL of the Stardag API registry.
        workspace_id: Active workspace ID.
        environment_id: Active environment ID.
        auth: Authentication configuration.
        timeout: Request timeout in seconds.
    """

    url: str
    workspace_id: str
    environment_id: str
    auth: RegistryAuth = Field(default_factory=RegistryAuth)
    timeout: float = DEFAULT_API_TIMEOUT


# --- Context (provenance info, not canonical config) ---


class ConfigContext(BaseModel):
    """Config provenance/context -- where the canonical config came from.

    This is not part of the canonical config, but tells you which profile
    and registry name were used to resolve it (useful for credential lookup
    and display).

    Attributes:
        profile: Active profile name (if using profile-based config).
        registry_name: Registry name from config (for credential file lookup).
    """

    profile: str | None = None
    registry_name: str | None = None


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

    # Force no registry (offline/local mode)
    no_registry: bool = False

    model_config = SettingsConfigDict(
        env_prefix="STARDAG_",
        extra="ignore",
    )


# --- Unified Config ---


class StardagConfig(BaseModel):
    """Unified Stardag configuration.

    This is the canonical configuration object that combines settings from
    all sources (env vars, project config, user config, defaults).

    ``registry`` is ``None`` when running in offline/local mode (no registry
    configured, or ``STARDAG_NO_REGISTRY=1``).
    """

    registry: RegistryConfig | None = None
    target: TargetConfig = Field(default_factory=TargetConfig)


class StardagConfigWithContext(StardagConfig):
    """StardagConfig with additional provenance context.

    This is the actual object returned by ``load_config()`` / ``config_provider.get()``,
    but the return type annotation is ``StardagConfig`` so that programmatic overrides
    only need to provide the canonical fields.
    """

    context: ConfigContext = Field(default_factory=ConfigContext)


# --- Deprecated aliases for backward compatibility ---

# These are kept importable but should not be used in new code.
# The old RegistryConfig(url: str) from TOML is now TomlRegistryEntry.


class APIConfig(BaseModel):
    """Deprecated: Use ``StardagConfig.registry`` instead.

    API registry configuration.
    """

    url: str | None = None
    timeout: float = DEFAULT_API_TIMEOUT


class ContextConfig(BaseModel):
    """Deprecated: Use ``StardagConfigWithContext.context`` instead.

    Active context configuration.
    """

    profile: str | None = None
    registry_name: str | None = None
    user: str | None = None
    workspace_id: str | None = None
    environment_id: str | None = None
