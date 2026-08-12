"""Stardag profile configuration for Modal deployments.

Turns the active (or a named) stardag profile into the environment variables
that configure the SDK *inside* Modal containers, and into the
``modal.Secret`` that carries them there.
"""

from __future__ import annotations

import json
import os
import typing

import modal

from stardag.config import clear_config_cache, config_provider, load_config
from stardag.registry._base import get_git_commit_hash


def get_profile_env_vars(profile: str | None = None) -> dict[str, str]:
    """Get environment variables from a stardag profile for Modal deployment.

    These environment variables configure the stardag SDK inside Modal containers
    to connect to the correct registry, workspace, and environment.

    Args:
        profile: Profile name to use. If None, uses the active profile
            (from STARDAG_PROFILE env var or default profile in config).

    Returns:
        Dict of environment variables to inject into Modal functions:
        - STARDAG_API_URL: API endpoint
        - STARDAG_WORKSPACE_ID: Workspace UUID
        - STARDAG_ENVIRONMENT_ID: Environment UUID
        - STARDAG_TARGET_ROOTS: JSON dict of target roots (pydantic-settings parses this)
        - COMMIT_HASH: Current git commit (for traceability)

    Example:
        >>> env_vars = get_profile_env_vars("production")
        >>> print(env_vars)
        {
            'STARDAG_API_URL': 'https://api.stardag.com',
            'STARDAG_WORKSPACE_ID': '...',
            'STARDAG_ENVIRONMENT_ID': '...',
            'STARDAG_TARGET_ROOTS': '{"default": "s3://bucket/prefix"}',
            'COMMIT_HASH': 'abc123...'
        }
    """
    # Load config for specific profile if provided
    if profile:
        # Temporarily set STARDAG_PROFILE to load that profile's config
        old_profile = os.environ.get("STARDAG_PROFILE")
        os.environ["STARDAG_PROFILE"] = profile
        try:
            clear_config_cache()
            config = load_config()
        finally:
            if old_profile is not None:
                os.environ["STARDAG_PROFILE"] = old_profile
            else:
                os.environ.pop("STARDAG_PROFILE", None)
            clear_config_cache()
    else:
        config = config_provider.get()

    env_vars: dict[str, str] = {}

    reg = config.registry
    if reg:
        env_vars["STARDAG_API_URL"] = reg.url
        if reg.workspace_id:
            env_vars["STARDAG_WORKSPACE_ID"] = reg.workspace_id
        if reg.environment_id:
            env_vars["STARDAG_ENVIRONMENT_ID"] = reg.environment_id

    # Add target roots as JSON (pydantic-settings parses JSON for nested fields)
    if config.target.roots:
        env_vars["STARDAG_TARGET_ROOTS"] = json.dumps(config.target.roots)

    # Add git commit for traceability
    commit_hash = get_git_commit_hash()
    if commit_hash:
        env_vars["COMMIT_HASH"] = commit_hash

    return env_vars


def get_profile_secret(profile: str | None = None) -> modal.Secret:
    """Create a Modal secret from a stardag profile's environment variables.

    This is the recommended way to inject profile configuration into Modal
    functions at runtime, rather than baking them into the image.

    Args:
        profile: Profile name to use. If None, uses the active profile.

    Returns:
        A modal.Secret that can be passed to FunctionSettings.secrets.

    Example:
        >>> secret = get_profile_secret("production")
        >>> stardag_app = StardagApp(
        ...     "my-app",
        ...     builder_settings=FunctionSettings(
        ...         image=my_image,
        ...         secrets=[secret],  # Injected at runtime
        ...     ),
        ...     ...
        ... )
    """
    env_vars = get_profile_env_vars(profile)
    return modal.Secret.from_dict(typing.cast(dict[str, str | None], env_vars))
