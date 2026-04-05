"""Centralized configuration for Stardag SDK.

This module provides a unified configuration system that consolidates:
- Target factory settings (target roots)
- Registry settings (URL, workspace, environment, auth, timeout)
- Config context (provenance: which profile/registry name was used)

Configuration is loaded from multiple sources with the following priority:
1. Environment variables (STARDAG_*)
2. Project config (.stardag/config.toml in working directory or parents)
3. User config (~/.stardag/config.toml)
4. Defaults

Usage:
    from stardag.config import get_config

    config = get_config()
    if config.registry:
        print(config.registry.url)
    print(config.target.roots)

Environment Variables (highest priority):
    STARDAG_PROFILE          - Profile name to use (looks up in config.toml)
    STARDAG_API_URL          - Registry API URL override
    STARDAG_REGISTRY_URL     - Deprecated alias for STARDAG_API_URL
    STARDAG_WORKSPACE_ID     - Direct workspace ID override
    STARDAG_ENVIRONMENT_ID   - Direct environment ID override
    STARDAG_API_KEY          - API key for authentication
    STARDAG_TARGET_ROOTS     - JSON dict of target roots (override)
    STARDAG_NO_REGISTRY      - Set to 1/true to force offline/local mode
"""

# Public API — only symbols that external consumers should use.
# Internal code imports from submodules directly (config.paths, config.cache, etc.)

from stardag.config.loader import (
    clear_config_cache as clear_config_cache,
    config_provider as config_provider,
    get_config as get_config,
    load_config as load_config,
)
from stardag.config.models import (
    ConfigContext as ConfigContext,
    RegistryAuth as RegistryAuth,
    RegistryConfig as RegistryConfig,
    StardagConfig as StardagConfig,
    TargetConfig as TargetConfig,
)
from stardag.config.paths import (
    DEFAULT_API_TIMEOUT as DEFAULT_API_TIMEOUT,
    DEFAULT_TARGET_ROOT as DEFAULT_TARGET_ROOT,
    DEFAULT_TARGET_ROOT_KEY as DEFAULT_TARGET_ROOT_KEY,
)
