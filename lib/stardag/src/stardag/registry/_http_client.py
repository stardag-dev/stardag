"""Shared HTTP client utilities for registry API calls."""

from dataclasses import dataclass

from stardag.config import config_provider
from stardag.exceptions import APIError


@dataclass
class RegistryAPIClientConfig:
    """Configuration for Registry API client."""

    api_url: str
    api_key: str | None
    access_token: str | None
    workspace_id: str | None
    timeout: float

    @classmethod
    def from_config(
        cls,
        api_url: str | None = None,
        api_key: str | None = None,
        workspace_id: str | None = None,
        timeout: float | None = None,
    ) -> "RegistryAPIClientConfig":
        """Create config from central config with optional overrides."""
        config = config_provider.get()

        # API key: explicit > config (which includes env var)
        resolved_api_key = api_key or config.api_key

        # Access token from config (browser login, only if no API key)
        resolved_access_token = config.access_token if not resolved_api_key else None

        return cls(
            api_url=(api_url or config.api.url).rstrip("/"),
            api_key=resolved_api_key,
            access_token=resolved_access_token,
            workspace_id=workspace_id or config.context.workspace_id,
            timeout=timeout if timeout is not None else config.api.timeout,
        )


def get_async_http_client(config: RegistryAPIClientConfig):
    """Create an async HTTP client with proper authentication headers.

    Returns:
        httpx.AsyncClient configured with auth headers and timeout.

    Raises:
        ImportError: If httpx is not installed.
    """
    try:
        import httpx
    except ImportError:
        raise ImportError(
            "httpx is required for Registry API calls. "
            "Install it with: pip install stardag[api]"
        )

    headers = {}
    if config.api_key:
        headers["X-API-Key"] = config.api_key
    elif config.access_token:
        headers["Authorization"] = f"Bearer {config.access_token}"

    return httpx.AsyncClient(timeout=config.timeout, headers=headers)


def handle_response_error(response, operation: str = "API operation") -> None:
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
