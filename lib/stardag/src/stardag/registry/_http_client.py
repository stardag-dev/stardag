"""Shared HTTP client utilities for registry API calls."""

from dataclasses import dataclass, field

import httpx

from stardag.config import DEFAULT_API_TIMEOUT, config_provider
from stardag.exceptions import APIError
from stardag.registry._auth import StardagAPIKeyAuth, StardagTokenAuth


@dataclass
class RegistryAPIClientConfig:
    """Configuration for Registry API client."""

    api_url: str
    environment_id: str | None
    timeout: float
    auth: httpx.Auth | None = field(default=None, repr=False)

    @classmethod
    def from_config(
        cls,
        api_url: str | None = None,
        api_key: str | None = None,
        environment_id: str | None = None,
        timeout: float | None = None,
    ) -> "RegistryAPIClientConfig":
        """Create config from central config with optional overrides."""
        config = config_provider.get()
        reg = config.registry

        resolved_api_key = api_key or (
            reg.auth.api_key.get_secret_value() if reg and reg.auth.api_key else None
        )
        resolved_url = api_url or (reg.url if reg else None)
        if not resolved_url:
            raise ValueError(
                "Registry API client requires a URL. "
                "Set STARDAG_API_URL or configure a profile."
            )

        # Build auth object with auto-refresh support
        auth: httpx.Auth | None
        if resolved_api_key:
            auth = StardagAPIKeyAuth(resolved_api_key)
        elif reg and reg.auth.access_token:
            auth = StardagTokenAuth(
                access_token=reg.auth.access_token.get_secret_value(),
                workspace_id=reg.workspace_id,
                user_email=reg.auth.user_email,
                registry_url=reg.url,
                registry_name=config.context.registry_name,
            )
        else:
            auth = None

        return cls(
            api_url=resolved_url.rstrip("/"),
            environment_id=environment_id or (reg.environment_id if reg else None),
            timeout=timeout
            if timeout is not None
            else (reg.timeout if reg else DEFAULT_API_TIMEOUT),
            auth=auth,
        )


def get_async_http_client(config: RegistryAPIClientConfig) -> httpx.AsyncClient:
    """Create an async HTTP client with proper authentication.

    Uses httpx.Auth objects for automatic token refresh support.

    Returns:
        httpx.AsyncClient configured with auth and timeout.
    """
    return httpx.AsyncClient(timeout=config.timeout, auth=config.auth)


def handle_response_error(
    response: httpx.Response, operation: str = "API operation"
) -> None:
    """Check response for errors and raise appropriate exceptions.

    Args:
        response: httpx Response object.
        operation: Description of the operation for error messages.

    Raises:
        APIError: For 4xx/5xx errors except 409, 423, 429 which are
            handled by lock-specific logic.
    """
    if response.status_code < 400:
        return  # No error

    # Try to extract detail from response JSON
    detail = None
    try:
        data = response.json()
        detail = data.get("detail", str(data))
    except Exception:
        detail = response.text[:200] if response.text else None

    status_code = response.status_code

    # These status codes are handled by lock-specific logic, not raised
    if status_code in (409, 423, 429):
        return

    raise APIError(f"{operation} failed", status_code=status_code, detail=detail)
