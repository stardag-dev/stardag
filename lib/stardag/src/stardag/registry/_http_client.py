"""Shared HTTP client utilities for registry API calls."""

import platform
from dataclasses import dataclass, field

import httpx

from stardag._version import __version__
from stardag.config import DEFAULT_API_TIMEOUT, config_provider
from stardag.exceptions import APIError, SDKVersionUnsupportedError
from stardag.registry._auth import StardagAPIKeyAuth, StardagTokenAuth

# The header the registry keys on to decide whether this SDK is still
# supported. It deliberately does *not* parse ``User-Agent``: that string is
# for humans reading logs and may be rewritten by proxies, whereas this one
# is a contract.
SDK_VERSION_HEADER = "X-Stardag-SDK-Version"

# Sent on every registry request, from the moment this SDK ships.
#
# Why now, when no server enforces a minimum yet: the check can only ever
# work forwards. A server can tell an SDK "you are too old" only if that SDK
# was already announcing itself when it was released — a release that stays
# silent is permanently un-diagnosable, and no later server change can fix
# that retroactively. So this is deliberately shipped ahead of anything that
# reads it.
#
# The direction that matters is old SDK / new registry: the hosted registry
# always runs the latest API. New SDK against an old registry is not a
# supported combination (self-hosters upgrade both together) — it should
# fail clearly, which is what the missing-route handling elsewhere does, but
# it gets no compatibility machinery.
#
# Computed once at import: the version cannot change within a process, and
# per-request work here would be paid on every single API call.
SDK_CLIENT_HEADERS = {
    SDK_VERSION_HEADER: __version__,
    "User-Agent": (
        f"stardag/{__version__} "
        f"(Python/{platform.python_version()}; httpx/{httpx.__version__})"
    ),
}


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
        httpx.AsyncClient configured with auth, timeout and the SDK
        identification headers.
    """
    return httpx.AsyncClient(
        timeout=config.timeout, auth=config.auth, headers=SDK_CLIENT_HEADERS
    )


def sdk_version_unsupported_from_detail(detail: object) -> SDKVersionUnsupportedError:
    """Turn a ``426`` response's ``detail`` into the typed error.

    The server sends ``{"error_code": "SDK_VERSION_UNSUPPORTED", "message":
    ..., "sdk_version": ..., "minimum_sdk_version": ...}``. Its ``message``
    already names both versions and the exact ``pip install`` line, so it is
    carried through untouched — rewording it here would give the same advice
    two authors and one of them would go stale.

    Shared by both response-error handlers so the registry client and the
    lock client cannot diverge on how a 426 surfaces.
    """
    if isinstance(detail, dict):
        message = detail.get("message")
        return SDKVersionUnsupportedError(
            message=message if isinstance(message, str) else None,
            sdk_version=detail.get("sdk_version"),
            minimum_sdk_version=detail.get("minimum_sdk_version"),
            payload=detail,
        )
    # No structured detail (a proxy's own 426, say) — fall back to whatever
    # text arrived, then to the generic sentence.
    return SDKVersionUnsupportedError(message=str(detail) if detail else None)


def handle_response_error(
    response: httpx.Response, operation: str = "API operation"
) -> None:
    """Check response for errors and raise appropriate exceptions.

    Args:
        response: httpx Response object.
        operation: Description of the operation for error messages.

    Raises:
        SDKVersionUnsupportedError: For 426 (SDK older than the server's
            minimum).
        APIError: For 4xx/5xx errors except 409, 423, 429 which are
            handled by lock-specific logic.
    """
    if response.status_code < 400:
        return  # No error

    # Try to extract detail from response JSON
    detail = None
    raw_detail: object = None
    try:
        data = response.json()
        raw_detail = data.get("detail")
        detail = str(raw_detail) if raw_detail is not None else str(data)
    except Exception:
        detail = response.text[:200] if response.text else None

    status_code = response.status_code

    # These status codes are handled by lock-specific logic, not raised
    if status_code in (409, 423, 429):
        return

    if status_code == 426:
        # `raw_detail` is None when the body was not JSON — a
        # proxy-generated 426 is the realistic case, and it is exactly
        # when the upstream's own words matter most. Fall back to the
        # text detail rather than losing it to the generic message.
        raise sdk_version_unsupported_from_detail(
            raw_detail if raw_detail is not None else detail
        )

    raise APIError(f"{operation} failed", status_code=status_code, detail=detail)
