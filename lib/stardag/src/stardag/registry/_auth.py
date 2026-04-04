"""Authentication utilities for registry API calls.

Provides httpx.Auth subclasses with automatic token refresh for use in
APIRegistry and other HTTP clients. Also contains the core token refresh
logic (OIDC refresh + internal token exchange) extracted from the CLI
credentials module so it can be used without CLI dependencies.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from typing import Generator, TypedDict

import httpx

from stardag.config.io import load_json_file
from stardag.config.paths import (
    get_access_token_cache_path,
    get_registry_credentials_path,
)

logger = logging.getLogger(__name__)


# --- Credential and token cache I/O ---


class Credentials(TypedDict, total=False):
    """Stored credentials structure (OAuth tokens only)."""

    refresh_token: str
    token_endpoint: str
    client_id: str


class AccessTokenCache(TypedDict, total=False):
    """Cached access token structure."""

    access_token: str
    expires_at: float


def load_credentials(registry: str, user: str) -> Credentials | None:
    """Load credentials from disk for a specific registry and user."""
    path = get_registry_credentials_path(registry, user)
    if not path.exists():
        return None
    try:
        with open(path) as f:
            data = json.load(f)
        return Credentials(**data)
    except (json.JSONDecodeError, TypeError, KeyError):
        return None


def save_credentials(credentials: Credentials, registry: str, user: str) -> None:
    """Save credentials to disk for a specific registry and user."""
    path = get_registry_credentials_path(registry, user)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(credentials, f, indent=2)
    path.chmod(0o600)


def load_access_token_cache(
    registry: str, workspace_id: str, user: str
) -> AccessTokenCache | None:
    """Load access token from cache, returning None if expired or missing."""
    path = get_access_token_cache_path(registry, workspace_id, user)
    if not path.exists():
        return None
    try:
        with open(path) as f:
            data = json.load(f)
        cache = AccessTokenCache(**data)
        if cache.get("expires_at", 0) <= time.time():
            return None
        return cache
    except (json.JSONDecodeError, TypeError, KeyError):
        return None


def save_access_token_cache(
    registry: str,
    workspace_id: str,
    access_token: str,
    expires_in: int,
    user: str,
) -> None:
    """Save access token to cache with expiration buffer."""
    path = get_access_token_cache_path(registry, workspace_id, user)
    path.parent.mkdir(parents=True, exist_ok=True)
    expires_at = time.time() + expires_in - 30  # 30s buffer
    cache = AccessTokenCache(access_token=access_token, expires_at=expires_at)
    with open(path, "w") as f:
        json.dump(cache, f, indent=2)
    path.chmod(0o600)


# --- OIDC token refresh helpers ---


def refresh_oidc_token(
    token_endpoint: str,
    refresh_token: str,
    client_id: str,
) -> dict:
    """Refresh OIDC tokens using refresh token.

    Returns the token response dict.
    Raises httpx.HTTPStatusError on failure.
    """
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
    }
    with httpx.Client(timeout=30.0) as client:
        response = client.post(token_endpoint, data=data)
        response.raise_for_status()
        return response.json()


def exchange_for_internal_token(
    api_url: str,
    oidc_token: str,
    workspace_id: str,
) -> dict:
    """Exchange OIDC token for internal workspace-scoped token.

    Returns dict with access_token and expires_in.
    Raises httpx.HTTPStatusError on failure.
    """
    with httpx.Client(timeout=30.0) as client:
        response = client.post(
            f"{api_url}/api/v1/auth/exchange",
            json={"workspace_id": workspace_id},
            headers={"Authorization": f"Bearer {oidc_token}"},
        )
        response.raise_for_status()
        return response.json()


def get_user_workspaces(api_url: str, oidc_token: str) -> list[dict]:
    """Fetch user's workspaces from API using OIDC token.

    Raises on request, HTTP status, or response parsing failures so callers
    can distinguish an empty workspace list from a failed request.
    """
    with httpx.Client(timeout=10.0) as client:
        response = client.get(
            f"{api_url}/api/v1/ui/me",
            headers={"Authorization": f"Bearer {oidc_token}"},
        )
        response.raise_for_status()
        data = response.json()
        return data.get("workspaces", [])


def get_environments(api_url: str, access_token: str, workspace_id: str) -> list[dict]:
    """Fetch environments for a workspace using internal token.

    Raises on request, HTTP status, or response parsing failures so callers
    can distinguish an empty environment list from a failed request.
    """
    with httpx.Client(timeout=10.0) as client:
        response = client.get(
            f"{api_url}/api/v1/ui/workspaces/{workspace_id}/environments",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        response.raise_for_status()
        return response.json()


def ensure_access_token(
    registry_name: str,
    workspace_id: str,
    user: str,
    registry_url: str | None = None,
) -> str | None:
    """Ensure we have a valid access token, refreshing if needed.

    This is the core token refresh orchestration function. It:
    1. Checks the token cache for a valid (non-expired) token
    2. If expired, loads OIDC credentials and refreshes
    3. Exchanges the fresh OIDC token for an internal workspace-scoped token
    4. Caches the new token

    Args:
        registry_name: Registry name (for credential/cache file lookup).
        workspace_id: Workspace ID.
        user: User identifier (email).
        registry_url: Registry URL. If None, looks up from TOML config.

    Returns:
        Access token if available/refreshed successfully, None otherwise.
    """
    # Check for cached valid token first
    cached = load_access_token_cache(registry_name, workspace_id, user)
    if cached:
        cached_token = cached.get("access_token")
        if cached_token:
            return cached_token

    # Need to refresh - get credentials
    creds = load_credentials(registry_name, user)
    if not creds:
        return None

    token_endpoint = creds.get("token_endpoint")
    refresh_token_val = creds.get("refresh_token")
    client_id = creds.get("client_id")

    if not token_endpoint or not client_id:
        return None

    # Resolve registry URL if not provided — read from TOML config directly
    # to avoid importing from the CLI layer.
    if not registry_url:
        from stardag.config.io import load_toml_file
        from stardag.config.models import TomlConfig
        from stardag.config.paths import get_user_config_path

        toml_data = load_toml_file(get_user_config_path())
        toml_config = TomlConfig.from_toml_dict(toml_data)
        reg_entry = toml_config.registry.get(registry_name)
        registry_url = reg_entry.url if reg_entry else None
        if not registry_url:
            return None

    try:
        if refresh_token_val:
            tokens = refresh_oidc_token(token_endpoint, refresh_token_val, client_id)

            # Update stored refresh token if a new one was provided
            if tokens.get("refresh_token"):
                creds["refresh_token"] = tokens["refresh_token"]
                save_credentials(creds, registry_name, user)

            oidc_token = tokens["access_token"]
        else:
            return None

        # Exchange for internal token
        internal_tokens = exchange_for_internal_token(
            registry_url, oidc_token, workspace_id
        )
        access_token = internal_tokens["access_token"]
        expires_in = internal_tokens.get("expires_in", 600)

        # Cache it
        save_access_token_cache(
            registry_name, workspace_id, access_token, expires_in, user
        )

        return access_token

    except Exception:
        logger.debug(
            "Token refresh failed for %s/%s/%s",
            registry_name,
            user,
            workspace_id,
            exc_info=True,
        )
        return None


def get_fresh_oidc_token(registry: str, user: str) -> str | None:
    """Get a fresh OIDC access token by refreshing.

    Args:
        registry: Registry name.
        user: User identifier (email).

    Returns the OIDC access token or None if refresh fails.
    """
    creds = load_credentials(registry, user)
    if not creds:
        return None

    token_endpoint = creds.get("token_endpoint")
    refresh_token_val = creds.get("refresh_token")
    client_id = creds.get("client_id")

    if not token_endpoint or not refresh_token_val or not client_id:
        return None

    try:
        tokens = refresh_oidc_token(token_endpoint, refresh_token_val, client_id)

        if tokens.get("refresh_token"):
            creds["refresh_token"] = tokens["refresh_token"]
            save_credentials(creds, registry, user)

        return tokens.get("access_token")
    except Exception:
        return None


# --- httpx.Auth implementations ---


class StardagAPIKeyAuth(httpx.Auth):
    """Static API key authentication. Sets X-API-Key header on each request."""

    def __init__(self, api_key: str):
        self.api_key = api_key

    def auth_flow(
        self, request: httpx.Request
    ) -> Generator[httpx.Request, httpx.Response, None]:
        request.headers["X-API-Key"] = self.api_key
        yield request


class StardagTokenAuth(httpx.Auth):
    """JWT token authentication with automatic refresh.

    Proactively checks token expiry before each request and refreshes
    if needed. Thread-safe for concurrent sync/async usage.

    Similar to how boto3 handles credential refresh -- the auth flow
    checks validity and refreshes transparently before each request.
    """

    def __init__(
        self,
        access_token: str | None,
        registry_name: str | None,
        workspace_id: str | None,
        user_email: str | None,
        registry_url: str | None = None,
    ):
        self._access_token = access_token
        self._registry_name = registry_name
        self._workspace_id = workspace_id
        self._user_email = user_email
        self._registry_url = registry_url
        self._sync_lock = threading.Lock()
        self._async_lock: asyncio.Lock | None = None

    def auth_flow(
        self, request: httpx.Request
    ) -> Generator[httpx.Request, httpx.Response, None]:
        token = self._get_or_refresh_token_sync()
        if token:
            request.headers["Authorization"] = f"Bearer {token}"
        yield request

    async def async_auth_flow(self, request: httpx.Request):  # type: ignore[override]
        token = await self._get_or_refresh_token_async()
        if token:
            request.headers["Authorization"] = f"Bearer {token}"
        yield request

    def _get_or_refresh_token_sync(self) -> str | None:
        """Get token, refreshing if needed. Thread-safe."""
        with self._sync_lock:
            if self._is_cached_token_valid():
                return self._access_token
            refreshed = self._refresh()
            if refreshed:
                self._access_token = refreshed
            return self._access_token

    async def _get_or_refresh_token_async(self) -> str | None:
        """Get token, refreshing if needed. Async-safe."""
        if self._async_lock is None:
            self._async_lock = asyncio.Lock()
        async with self._async_lock:
            if self._is_cached_token_valid():
                return self._access_token
            # Run sync refresh in executor to avoid blocking the event loop
            loop = asyncio.get_running_loop()
            refreshed = await loop.run_in_executor(None, self._refresh)
            if refreshed:
                self._access_token = refreshed
            return self._access_token

    def _is_cached_token_valid(self) -> bool:
        """Check if the cached token is still valid, syncing self._access_token."""
        if not all([self._registry_name, self._workspace_id, self._user_email]):
            # Can't check cache without these identifiers
            return self._access_token is not None
        cache_path = get_access_token_cache_path(
            self._registry_name,  # type: ignore[arg-type]
            self._workspace_id,  # type: ignore[arg-type]
            self._user_email,  # type: ignore[arg-type]
        )
        if cache_path.exists():
            data = load_json_file(cache_path)
            cached_token = data.get("access_token")
            if cached_token and data.get("expires_at", 0) > time.time():
                self._access_token = cached_token
                return True
        return False

    def _refresh(self) -> str | None:
        """Attempt to refresh the token. Returns new token or None."""
        if not all([self._registry_name, self._workspace_id, self._user_email]):
            return None
        try:
            return ensure_access_token(
                registry_name=self._registry_name,  # type: ignore[arg-type]
                workspace_id=self._workspace_id,  # type: ignore[arg-type]
                user=self._user_email,  # type: ignore[arg-type]
                registry_url=self._registry_url,
            )
        except Exception:
            logger.debug("Token refresh failed in StardagTokenAuth", exc_info=True)
            return None
