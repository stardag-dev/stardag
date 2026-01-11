"""Shared HTTP client factory for Registry API communication.

This module provides typed HTTP clients for communicating with the stardag-api
service. It consolidates authentication and configuration logic shared between
APIRegistry and RegistryGlobalConcurrencyLockManager.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, NewType

from stardag.config import config_provider

if TYPE_CHECKING:
    import httpx

logger = logging.getLogger(__name__)

# NewType wrappers for type safety - these are httpx clients configured for the Registry API
RegistryAPIAsyncHTTPClient = NewType("RegistryAPIAsyncHTTPClient", "httpx.AsyncClient")
RegistryAPISyncHTTPClient = NewType("RegistryAPISyncHTTPClient", "httpx.Client")


@dataclass
class RegistryAPIClientConfig:
    """Configuration for Registry API HTTP clients.

    This consolidates all configuration needed to create an authenticated
    HTTP client for the Registry API.

    Attributes:
        api_url: Base URL for the Registry API (without trailing slash).
        timeout: HTTP request timeout in seconds.
        workspace_id: Workspace ID for JWT auth (required when using access_token).
        api_key: API key for authentication (takes precedence over access_token).
        access_token: JWT access token from browser login.
    """

    api_url: str
    timeout: float
    workspace_id: str | None
    api_key: str | None
    access_token: str | None

    @classmethod
    def from_config(
        cls,
        api_url: str | None = None,
        timeout: float | None = None,
        workspace_id: str | None = None,
        api_key: str | None = None,
    ) -> RegistryAPIClientConfig:
        """Create config from explicit values and/or central config.

        Args:
            api_url: Override API URL (defaults to central config).
            timeout: Override timeout (defaults to central config).
            workspace_id: Override workspace ID (defaults to central config).
            api_key: Override API key (defaults to central config).

        Returns:
            RegistryAPIClientConfig with resolved values.
        """
        config = config_provider.get()

        # API key: explicit > config (which includes env var)
        resolved_api_key = api_key or config.api_key

        # Access token from config (browser login, only if no API key)
        resolved_access_token = config.access_token if not resolved_api_key else None

        return cls(
            api_url=(api_url or config.api.url).rstrip("/"),
            timeout=timeout if timeout is not None else config.api.timeout,
            workspace_id=workspace_id or config.context.workspace_id,
            api_key=resolved_api_key,
            access_token=resolved_access_token,
        )

    def get_auth_headers(self) -> dict[str, str]:
        """Get authentication headers for HTTP requests.

        Returns:
            Dict with appropriate auth header (X-API-Key or Authorization).
        """
        headers: dict[str, str] = {}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        elif self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        return headers

    def get_workspace_params(self) -> dict[str, str]:
        """Get query params for workspace context.

        When using JWT auth, workspace_id must be passed as a query param.
        """
        if self.access_token and not self.api_key and self.workspace_id:
            return {"workspace_id": self.workspace_id}
        return {}

    def log_auth_status(self, component_name: str = "Registry client") -> None:
        """Log the authentication status for debugging."""
        if self.api_key:
            logger.debug(f"{component_name} initialized with API key authentication")
        elif self.access_token:
            if not self.workspace_id:
                logger.warning(
                    f"{component_name}: JWT auth requires workspace_id. "
                    "Run 'stardag config set workspace <id>' to set it."
                )
            else:
                logger.debug(
                    f"{component_name} initialized with browser login (JWT) authentication"
                )
        else:
            logger.warning(
                f"{component_name} initialized without authentication. "
                "Run 'stardag auth login' or set STARDAG_API_KEY env var."
            )


def _ensure_httpx() -> None:
    """Ensure httpx is installed, raise ImportError with helpful message if not."""
    try:
        import httpx  # noqa: F401
    except ImportError:
        raise ImportError(
            "httpx is required for Registry API communication. "
            "Install it with: pip install stardag[api]"
        )


def get_async_http_client(
    config: RegistryAPIClientConfig,
) -> RegistryAPIAsyncHTTPClient:
    """Create an async HTTP client configured for the Registry API.

    Args:
        config: Client configuration with auth and connection settings.

    Returns:
        Async HTTP client typed as RegistryAPIAsyncHTTPClient.

    Raises:
        ImportError: If httpx is not installed.
    """
    _ensure_httpx()
    import httpx

    client = httpx.AsyncClient(
        timeout=config.timeout,
        headers=config.get_auth_headers(),
    )
    return RegistryAPIAsyncHTTPClient(client)


def get_sync_http_client(
    config: RegistryAPIClientConfig,
) -> RegistryAPISyncHTTPClient:
    """Create a sync HTTP client configured for the Registry API.

    Args:
        config: Client configuration with auth and connection settings.

    Returns:
        Sync HTTP client typed as RegistryAPISyncHTTPClient.

    Raises:
        ImportError: If httpx is not installed.
    """
    _ensure_httpx()
    import httpx

    client = httpx.Client(
        timeout=config.timeout,
        headers=config.get_auth_headers(),
    )
    return RegistryAPISyncHTTPClient(client)
