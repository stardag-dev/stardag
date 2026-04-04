"""Tests for stardag.registry._auth module."""

import json
import time
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from stardag.registry._auth import (
    StardagAPIKeyAuth,
    StardagTokenAuth,
    ensure_access_token,
    load_access_token_cache,
    load_credentials,
    save_access_token_cache,
    save_credentials,
)


@pytest.fixture
def temp_stardag_dir(tmp_path, monkeypatch):
    """Patch Path.home() to use tmp_path for credential storage."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    stardag_dir = tmp_path / ".stardag"
    stardag_dir.mkdir()
    return stardag_dir


class TestStardagAPIKeyAuth:
    def test_sets_api_key_header(self):
        """API key auth sets X-API-Key header."""
        auth = StardagAPIKeyAuth("test-key-123")
        request = httpx.Request("GET", "https://example.com/api")

        flow = auth.auth_flow(request)
        modified_request = next(flow)

        assert modified_request.headers["X-API-Key"] == "test-key-123"


class TestStardagTokenAuth:
    def test_sets_bearer_header_with_valid_token(self, temp_stardag_dir):
        """Token auth sets Bearer header when token is available."""
        # Create a valid token cache
        save_access_token_cache("reg", "ws-1", "valid-token", 3600, "user@test.com")

        auth = StardagTokenAuth(
            access_token="valid-token",
            registry_name="reg",
            workspace_id="ws-1",
            user_email="user@test.com",
        )
        request = httpx.Request("GET", "https://example.com/api")

        flow = auth.auth_flow(request)
        modified_request = next(flow)

        assert modified_request.headers["Authorization"] == "Bearer valid-token"

    def test_no_header_when_no_token(self):
        """Token auth sets no header when no token available and can't refresh."""
        auth = StardagTokenAuth(
            access_token=None,
            registry_name=None,
            workspace_id=None,
            user_email=None,
        )
        request = httpx.Request("GET", "https://example.com/api")

        flow = auth.auth_flow(request)
        modified_request = next(flow)

        assert "Authorization" not in modified_request.headers

    def test_refresh_called_when_token_expired(self, temp_stardag_dir):
        """Token auth calls refresh when cached token is expired."""
        # Create an expired token cache
        cache_path = temp_stardag_dir / "access-token-cache"
        cache_path.mkdir(parents=True, exist_ok=True)
        token_file = cache_path / "reg__user_at_test.com__ws-1.json"
        token_file.write_text(
            json.dumps(
                {"access_token": "expired-token", "expires_at": time.time() - 60}
            )
        )

        auth = StardagTokenAuth(
            access_token="expired-token",
            registry_name="reg",
            workspace_id="ws-1",
            user_email="user@test.com",
            registry_url="http://localhost:8000",
        )

        with patch(
            "stardag.registry._auth.ensure_access_token", return_value="fresh-token"
        ) as mock_refresh:
            request = httpx.Request("GET", "https://example.com/api")
            flow = auth.auth_flow(request)
            modified_request = next(flow)

            assert modified_request.headers["Authorization"] == "Bearer fresh-token"
            mock_refresh.assert_called_once()

    @pytest.mark.asyncio
    async def test_async_auth_flow_sets_header(self, temp_stardag_dir):
        """Async auth flow sets Bearer header."""
        save_access_token_cache("reg", "ws-1", "async-token", 3600, "user@test.com")

        auth = StardagTokenAuth(
            access_token="async-token",
            registry_name="reg",
            workspace_id="ws-1",
            user_email="user@test.com",
        )
        request = httpx.Request("GET", "https://example.com/api")

        async for modified_request in auth.async_auth_flow(request):
            assert modified_request.headers["Authorization"] == "Bearer async-token"


class TestCredentialIO:
    def test_save_and_load_credentials(self, temp_stardag_dir):
        """Credentials can be saved and loaded."""
        from stardag.registry._auth import Credentials

        creds = Credentials(
            refresh_token="rt-123",
            token_endpoint="https://auth.example.com/token",
            client_id="client-1",
        )
        save_credentials(creds, "my-registry", "user@test.com")

        loaded = load_credentials("my-registry", "user@test.com")
        assert loaded is not None
        assert loaded.get("refresh_token") == "rt-123"

    def test_load_missing_credentials(self, temp_stardag_dir):
        """Loading non-existent credentials returns None."""
        loaded = load_credentials("nonexistent", "nobody@test.com")
        assert loaded is None


class TestAccessTokenCache:
    def test_save_and_load_valid_cache(self, temp_stardag_dir):
        """Valid token cache can be saved and loaded."""
        save_access_token_cache("reg", "ws-1", "my-token", 3600, "user@test.com")

        cached = load_access_token_cache("reg", "ws-1", "user@test.com")
        assert cached is not None
        assert cached.get("access_token") == "my-token"

    def test_expired_cache_returns_none(self, temp_stardag_dir):
        """Expired token cache returns None."""
        save_access_token_cache("reg", "ws-1", "old-token", -100, "user@test.com")

        cached = load_access_token_cache("reg", "ws-1", "user@test.com")
        assert cached is None


class TestEnsureAccessToken:
    def test_returns_cached_token(self, temp_stardag_dir):
        """Returns cached token when still valid."""
        save_access_token_cache("reg", "ws-1", "cached-token", 3600, "user@test.com")

        token = ensure_access_token("reg", "ws-1", "user@test.com")
        assert token == "cached-token"

    def test_returns_none_without_credentials(self, temp_stardag_dir):
        """Returns None when no credentials exist."""
        token = ensure_access_token("reg", "ws-1", "nobody@test.com")
        assert token is None
