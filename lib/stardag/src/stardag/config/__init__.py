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

# Re-export everything from submodules for backward compatibility.
# All existing `from stardag.config import X` imports continue to work.

from stardag.config.cache import (
    IdCache as IdCache,
    _looks_like_uuid as _looks_like_uuid,
    cache_environment_id as cache_environment_id,
    cache_workspace_id as cache_workspace_id,
    get_cached_environment_id as get_cached_environment_id,
    get_cached_target_roots as get_cached_target_roots,
    get_cached_workspace_id as get_cached_workspace_id,
    load_id_cache as load_id_cache,
    load_target_root_cache as load_target_root_cache,
    save_id_cache as save_id_cache,
    save_target_root_cache as save_target_root_cache,
    update_cached_target_roots as update_cached_target_roots,
)
from stardag.config.io import (
    load_json_file as load_json_file,
    load_toml_file as load_toml_file,
    save_json_file as save_json_file,
    save_toml_file as save_toml_file,
)
from stardag.config.loader import (
    _merge_toml_configs as _merge_toml_configs,
    _parse_target_roots_from_env as _parse_target_roots_from_env,
    clear_config_cache as clear_config_cache,
    config_provider as config_provider,
    get_config as get_config,
    load_config as load_config,
)
from stardag.config.models import (
    APIConfig as APIConfig,
    ContextConfig as ContextConfig,
    ProfileConfig as ProfileConfig,
    RegistryConfig as RegistryConfig,
    StardagConfig as StardagConfig,
    StardagSettings as StardagSettings,
    TargetConfig as TargetConfig,
    TargetRoots as TargetRoots,
    TomlConfig as TomlConfig,
)
from stardag.config.paths import (
    DEFAULT_API_TIMEOUT as DEFAULT_API_TIMEOUT,
    DEFAULT_API_URL as DEFAULT_API_URL,
    DEFAULT_TARGET_ROOT as DEFAULT_TARGET_ROOT,
    DEFAULT_TARGET_ROOT_KEY as DEFAULT_TARGET_ROOT_KEY,
    _sanitize_user_for_path as _sanitize_user_for_path,
    find_project_config as find_project_config,
    get_access_token_cache_dir as get_access_token_cache_dir,
    get_access_token_cache_path as get_access_token_cache_path,
    get_credentials_dir as get_credentials_dir,
    get_id_cache_path as get_id_cache_path,
    get_local_target_roots_dir as get_local_target_roots_dir,
    get_registry_credentials_path as get_registry_credentials_path,
    get_stardag_dir as get_stardag_dir,
    get_target_root_cache_path as get_target_root_cache_path,
    get_user_config_path as get_user_config_path,
)
