"""Tests for stardag.registry._auth module."""

import json
import time
from pathlib import Path
from threading import Barrier, Thread
from unittest.mock import patch

import httpx
import pytest

from stardag.config.paths import registry_key_from_url
from stardag.registry._auth import (
    Credentials,
    StardagAPIKeyAuth,
    StardagTokenAuth,
    _resolve_credential_key,
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


def _write_credentials(
    stardag_dir: Path,
    registry: str,
    user: str,
    *,
    refresh_token: str = "rt-123",
    token_endpoint: str = "https://auth.example.com/token",
    client_id: str = "client-1",
) -> None:
    """Write a credentials file directly to disk."""
    creds = Credentials(
        refresh_token=refresh_token,
        token_endpoint=token_endpoint,
        client_id=client_id,
    )
    save_credentials(creds, registry, user)


def _write_token_cache(
    registry: str,
    workspace_id: str,
    user: str,
    *,
    token: str = "cached-token",
    expires_in: int = 3600,
) -> None:
    """Write a token cache file."""
    save_access_token_cache(registry, workspace_id, token, expires_in, user)


def _write_expired_token_cache(
    stardag_dir: Path,
    registry: str,
    workspace_id: str,
    user: str,
) -> None:
    """Write an expired token cache file directly (bypassing the 30s buffer)."""
    from stardag.config.paths import (
        _sanitize_user_for_path,
        get_access_token_cache_dir,
    )

    safe_user = _sanitize_user_for_path(user)
    cache_dir = get_access_token_cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{registry}__{safe_user}__{workspace_id}.json"
    path.write_text(
        json.dumps({"access_token": "expired-token", "expires_at": time.time() - 60})
    )
    path.chmod(0o600)


# ---------------------------------------------------------------------------
# StardagAPIKeyAuth
# ---------------------------------------------------------------------------


class TestStardagAPIKeyAuth:
    def test_sets_api_key_header(self):
        auth = StardagAPIKeyAuth("test-key-123")
        request = httpx.Request("GET", "https://example.com/api")
        modified = next(auth.auth_flow(request))
        assert modified.headers["X-API-Key"] == "test-key-123"

    def test_header_set_on_every_request(self):
        """Each call to auth_flow sets the header (stateless)."""
        auth = StardagAPIKeyAuth("key")
        for _ in range(3):
            req = httpx.Request("POST", "https://example.com")
            modified = next(auth.auth_flow(req))
            assert modified.headers["X-API-Key"] == "key"


# ---------------------------------------------------------------------------
# StardagTokenAuth – valid token path
# ---------------------------------------------------------------------------


class TestStardagTokenAuthValidToken:
    def test_sets_bearer_header_with_valid_cached_token(self, temp_stardag_dir):
        _write_token_cache("reg", "ws-1", "u@t.com", token="good-token")

        auth = StardagTokenAuth(
            access_token="good-token",
            registry_name="reg",
            workspace_id="ws-1",
            user_email="u@t.com",
        )
        req = httpx.Request("GET", "https://example.com/api")
        modified = next(auth.auth_flow(req))
        assert modified.headers["Authorization"] == "Bearer good-token"

    def test_no_header_when_no_token_and_no_credentials(self):
        """No auth header when no token and can't identify credentials."""
        auth = StardagTokenAuth(
            access_token=None,
            registry_name=None,
            workspace_id=None,
            user_email=None,
        )
        req = httpx.Request("GET", "https://example.com/api")
        modified = next(auth.auth_flow(req))
        assert "Authorization" not in modified.headers

    def test_uses_initial_token_when_no_cache_identifiers(self):
        """Falls back to initial access_token when cache can't be checked."""
        auth = StardagTokenAuth(
            access_token="initial-token",
            registry_name=None,
            workspace_id=None,
            user_email=None,
        )
        req = httpx.Request("GET", "https://example.com/api")
        modified = next(auth.auth_flow(req))
        assert modified.headers["Authorization"] == "Bearer initial-token"


# ---------------------------------------------------------------------------
# StardagTokenAuth – refresh path
# ---------------------------------------------------------------------------


class TestStardagTokenAuthRefresh:
    def test_refresh_called_when_token_expired(self, temp_stardag_dir):
        _write_expired_token_cache(temp_stardag_dir, "reg", "ws-1", "u@t.com")

        auth = StardagTokenAuth(
            access_token="expired-token",
            registry_name="reg",
            workspace_id="ws-1",
            user_email="u@t.com",
            registry_url="http://localhost:8000",
        )

        with patch(
            "stardag.registry._auth.ensure_access_token",
            return_value="fresh-token",
        ) as mock_refresh:
            req = httpx.Request("GET", "https://example.com/api")
            modified = next(auth.auth_flow(req))

            assert modified.headers["Authorization"] == "Bearer fresh-token"
            mock_refresh.assert_called_once_with(
                registry_name="reg",
                workspace_id="ws-1",
                user="u@t.com",
                registry_url="http://localhost:8000",
            )

    def test_keeps_old_token_when_refresh_fails(self, temp_stardag_dir):
        _write_expired_token_cache(temp_stardag_dir, "reg", "ws-1", "u@t.com")

        auth = StardagTokenAuth(
            access_token="old-token",
            registry_name="reg",
            workspace_id="ws-1",
            user_email="u@t.com",
        )

        with patch(
            "stardag.registry._auth.ensure_access_token",
            return_value=None,
        ):
            req = httpx.Request("GET", "https://example.com/api")
            modified = next(auth.auth_flow(req))
            # Falls back to old token since refresh returned None
            assert modified.headers["Authorization"] == "Bearer old-token"

    def test_keeps_old_token_when_refresh_raises(self, temp_stardag_dir):
        _write_expired_token_cache(temp_stardag_dir, "reg", "ws-1", "u@t.com")

        auth = StardagTokenAuth(
            access_token="old-token",
            registry_name="reg",
            workspace_id="ws-1",
            user_email="u@t.com",
        )

        with patch(
            "stardag.registry._auth.ensure_access_token",
            side_effect=RuntimeError("network error"),
        ):
            req = httpx.Request("GET", "https://example.com/api")
            modified = next(auth.auth_flow(req))
            assert modified.headers["Authorization"] == "Bearer old-token"

    def test_updates_internal_token_after_refresh(self, temp_stardag_dir):
        """After refresh, subsequent requests use the new token without re-refreshing."""
        _write_expired_token_cache(temp_stardag_dir, "reg", "ws-1", "u@t.com")

        auth = StardagTokenAuth(
            access_token="expired-token",
            registry_name="reg",
            workspace_id="ws-1",
            user_email="u@t.com",
        )

        # First request: triggers refresh
        with patch(
            "stardag.registry._auth.ensure_access_token",
            return_value="new-token",
        ):
            req = httpx.Request("GET", "https://example.com/api")
            next(auth.auth_flow(req))

        # Write a valid cache entry so the second request doesn't refresh
        _write_token_cache("reg", "ws-1", "u@t.com", token="new-token")

        # Second request: should use new-token without calling refresh
        with patch(
            "stardag.registry._auth.ensure_access_token",
        ) as mock_refresh:
            req = httpx.Request("GET", "https://example.com/api")
            modified = next(auth.auth_flow(req))
            assert modified.headers["Authorization"] == "Bearer new-token"
            mock_refresh.assert_not_called()


# ---------------------------------------------------------------------------
# StardagTokenAuth – async
# ---------------------------------------------------------------------------


class TestStardagTokenAuthAsync:
    @pytest.mark.asyncio
    async def test_async_sets_bearer_header(self, temp_stardag_dir):
        _write_token_cache("reg", "ws-1", "u@t.com", token="async-token")

        auth = StardagTokenAuth(
            access_token="async-token",
            registry_name="reg",
            workspace_id="ws-1",
            user_email="u@t.com",
        )
        req = httpx.Request("GET", "https://example.com/api")
        async for modified in auth.async_auth_flow(req):
            assert modified.headers["Authorization"] == "Bearer async-token"

    @pytest.mark.asyncio
    async def test_async_refresh_when_expired(self, temp_stardag_dir):
        _write_expired_token_cache(temp_stardag_dir, "reg", "ws-1", "u@t.com")

        auth = StardagTokenAuth(
            access_token="expired",
            registry_name="reg",
            workspace_id="ws-1",
            user_email="u@t.com",
        )

        with patch(
            "stardag.registry._auth.ensure_access_token",
            return_value="async-fresh",
        ):
            req = httpx.Request("GET", "https://example.com/api")
            async for modified in auth.async_auth_flow(req):
                assert modified.headers["Authorization"] == "Bearer async-fresh"


# ---------------------------------------------------------------------------
# StardagTokenAuth – thread safety
# ---------------------------------------------------------------------------


class TestStardagTokenAuthThreadSafety:
    def test_concurrent_refresh_only_refreshes_once(self, temp_stardag_dir):
        """Multiple threads hitting auth_flow simultaneously should only
        trigger one refresh (the lock serialises them)."""
        _write_expired_token_cache(temp_stardag_dir, "reg", "ws-1", "u@t.com")

        auth = StardagTokenAuth(
            access_token="expired",
            registry_name="reg",
            workspace_id="ws-1",
            user_email="u@t.com",
        )

        call_count = 0

        def mock_ensure(**kwargs):
            nonlocal call_count
            call_count += 1
            # After the first refresh, write a valid cache so subsequent
            # lock holders see a valid token and skip refreshing
            _write_token_cache("reg", "ws-1", "u@t.com", token="refreshed")
            return "refreshed"

        barrier = Barrier(4)
        results: list[str | None] = [None] * 4

        def worker(idx):
            barrier.wait()  # all threads start at the same time
            with patch(
                "stardag.registry._auth.ensure_access_token",
                side_effect=mock_ensure,
            ):
                req = httpx.Request("GET", "https://example.com")
                modified = next(auth.auth_flow(req))
                results[idx] = modified.headers.get("Authorization")

        threads = [Thread(target=worker, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All threads got a Bearer header
        for r in results:
            assert r == "Bearer refreshed"

        # Refresh was called only once (subsequent lock holders see valid cache)
        assert call_count == 1


# ---------------------------------------------------------------------------
# Credential and token cache I/O
# ---------------------------------------------------------------------------


class TestCredentialIO:
    def test_roundtrip(self, temp_stardag_dir):
        creds = Credentials(
            refresh_token="rt-abc",
            token_endpoint="https://auth.example.com/token",
            client_id="my-client",
        )
        save_credentials(creds, "my-reg", "user@example.com")

        loaded = load_credentials("my-reg", "user@example.com")
        assert loaded is not None
        assert loaded.get("refresh_token") == "rt-abc"
        assert loaded.get("token_endpoint") == "https://auth.example.com/token"
        assert loaded.get("client_id") == "my-client"

    def test_returns_none_for_missing(self, temp_stardag_dir):
        assert load_credentials("nope", "nobody@example.com") is None

    def test_file_permissions(self, temp_stardag_dir):
        creds = Credentials(refresh_token="rt", token_endpoint="ep", client_id="c")
        save_credentials(creds, "reg", "u@t.com")

        from stardag.config.paths import get_registry_credentials_path

        path = get_registry_credentials_path("reg", "u@t.com")
        assert path.stat().st_mode & 0o777 == 0o600


class TestAccessTokenCache:
    def test_roundtrip_valid(self, temp_stardag_dir):
        save_access_token_cache("reg", "ws-1", "my-token", 3600, "u@t.com")
        cached = load_access_token_cache("reg", "ws-1", "u@t.com")
        assert cached is not None
        assert cached.get("access_token") == "my-token"

    def test_expired_returns_none(self, temp_stardag_dir):
        save_access_token_cache("reg", "ws-1", "old-token", -100, "u@t.com")
        assert load_access_token_cache("reg", "ws-1", "u@t.com") is None

    def test_missing_returns_none(self, temp_stardag_dir):
        assert load_access_token_cache("reg", "ws-1", "nobody@t.com") is None

    def test_file_permissions(self, temp_stardag_dir):
        save_access_token_cache("reg", "ws-1", "tok", 3600, "u@t.com")

        from stardag.config.paths import get_access_token_cache_path

        path = get_access_token_cache_path("reg", "ws-1", "u@t.com")
        assert path.stat().st_mode & 0o777 == 0o600


# ---------------------------------------------------------------------------
# ensure_access_token – full refresh orchestration
# ---------------------------------------------------------------------------


class TestEnsureAccessToken:
    def test_returns_cached_token_without_refresh(self, temp_stardag_dir):
        _write_token_cache("reg", "ws-1", "u@t.com", token="cached")
        assert ensure_access_token("reg", "ws-1", "u@t.com") == "cached"

    def test_returns_none_without_credentials(self, temp_stardag_dir):
        assert ensure_access_token("reg", "ws-1", "nobody@t.com") is None

    def test_returns_none_when_missing_token_endpoint(self, temp_stardag_dir):
        """Credentials without token_endpoint can't refresh."""
        creds = Credentials(refresh_token="rt", client_id="c")
        save_credentials(creds, "reg", "u@t.com")
        assert (
            ensure_access_token(
                "reg", "ws-1", "u@t.com", registry_url="http://localhost"
            )
            is None
        )

    def test_returns_none_when_no_refresh_token(self, temp_stardag_dir):
        """Credentials without refresh_token can't refresh."""
        creds = Credentials(token_endpoint="https://auth/token", client_id="c")
        save_credentials(creds, "reg", "u@t.com")
        assert (
            ensure_access_token(
                "reg", "ws-1", "u@t.com", registry_url="http://localhost"
            )
            is None
        )

    def test_full_refresh_flow(self, temp_stardag_dir):
        """Test the complete OIDC refresh -> exchange -> cache flow."""
        _write_credentials(temp_stardag_dir, "reg", "u@t.com")

        with (
            patch(
                "stardag.registry._auth.refresh_oidc_token",
                return_value={
                    "access_token": "oidc-fresh",
                    "refresh_token": "rt-new",
                },
            ) as mock_refresh,
            patch(
                "stardag.registry._auth.exchange_for_internal_token",
                return_value={
                    "access_token": "internal-fresh",
                    "expires_in": 600,
                },
            ) as mock_exchange,
        ):
            result = ensure_access_token(
                "reg", "ws-1", "u@t.com", registry_url="http://api.example.com"
            )

        assert result == "internal-fresh"
        mock_refresh.assert_called_once_with(
            "https://auth.example.com/token", "rt-123", "client-1"
        )
        mock_exchange.assert_called_once_with(
            "http://api.example.com", "oidc-fresh", "ws-1"
        )

        # Verify the token was cached
        cached = load_access_token_cache("reg", "ws-1", "u@t.com")
        assert cached is not None
        assert cached.get("access_token") == "internal-fresh"

    def test_refresh_token_rotation(self, temp_stardag_dir):
        """When OIDC returns a new refresh_token, it's saved to credentials."""
        _write_credentials(temp_stardag_dir, "reg", "u@t.com", refresh_token="rt-old")

        with (
            patch(
                "stardag.registry._auth.refresh_oidc_token",
                return_value={
                    "access_token": "oidc",
                    "refresh_token": "rt-rotated",
                },
            ),
            patch(
                "stardag.registry._auth.exchange_for_internal_token",
                return_value={"access_token": "internal", "expires_in": 600},
            ),
        ):
            ensure_access_token("reg", "ws-1", "u@t.com", registry_url="http://api")

        # Verify the new refresh token was persisted
        creds = load_credentials("reg", "u@t.com")
        assert creds is not None
        assert creds.get("refresh_token") == "rt-rotated"

    def test_returns_none_on_oidc_refresh_failure(self, temp_stardag_dir):
        _write_credentials(temp_stardag_dir, "reg", "u@t.com")

        with patch(
            "stardag.registry._auth.refresh_oidc_token",
            side_effect=Exception("OIDC down"),
        ):
            result = ensure_access_token(
                "reg", "ws-1", "u@t.com", registry_url="http://api"
            )

        assert result is None

    def test_returns_none_on_exchange_failure(self, temp_stardag_dir):
        _write_credentials(temp_stardag_dir, "reg", "u@t.com")

        with (
            patch(
                "stardag.registry._auth.refresh_oidc_token",
                return_value={"access_token": "oidc"},
            ),
            patch(
                "stardag.registry._auth.exchange_for_internal_token",
                side_effect=Exception("exchange failed"),
            ),
        ):
            result = ensure_access_token(
                "reg", "ws-1", "u@t.com", registry_url="http://api"
            )

        assert result is None

    def test_resolves_registry_url_from_toml_when_not_provided(self, temp_stardag_dir):
        """When registry_url is not provided, reads from ~/.stardag/config.toml."""
        _write_credentials(temp_stardag_dir, "reg", "u@t.com")

        # Write a TOML config with the registry URL
        config_path = temp_stardag_dir / "config.toml"
        config_path.write_text('[registry.reg]\nurl = "http://from-toml"\n')

        with (
            patch(
                "stardag.registry._auth.refresh_oidc_token",
                return_value={"access_token": "oidc"},
            ),
            patch(
                "stardag.registry._auth.exchange_for_internal_token",
                return_value={"access_token": "tok", "expires_in": 600},
            ) as mock_exchange,
        ):
            result = ensure_access_token("reg", "ws-1", "u@t.com")

        assert result == "tok"
        # Verify it used the URL from TOML config
        mock_exchange.assert_called_once_with("http://from-toml", "oidc", "ws-1")


# ---------------------------------------------------------------------------
# registry_key_from_url
# ---------------------------------------------------------------------------


class TestRegistryKeyFromUrl:
    def test_https_standard_port(self):
        assert registry_key_from_url("https://api.stardag.com") == "api.stardag.com"

    def test_https_explicit_443(self):
        assert registry_key_from_url("https://api.stardag.com:443") == "api.stardag.com"

    def test_http_standard_port(self):
        assert registry_key_from_url("http://api.example.com") == "api.example.com"

    def test_http_explicit_80(self):
        assert registry_key_from_url("http://api.example.com:80") == "api.example.com"

    def test_non_standard_port(self):
        assert registry_key_from_url("http://localhost:8000") == "localhost_8000"

    def test_https_non_standard_port(self):
        assert (
            registry_key_from_url("https://api.example.com:9443")
            == "api.example.com_9443"
        )


# ---------------------------------------------------------------------------
# _resolve_credential_key
# ---------------------------------------------------------------------------


class TestResolveCredentialKey:
    def test_prefers_registry_name(self):
        assert _resolve_credential_key("my-reg", "http://localhost:8000") == "my-reg"

    def test_falls_back_to_url(self):
        assert (
            _resolve_credential_key(None, "https://api.stardag.com")
            == "api.stardag.com"
        )

    def test_returns_none_without_either(self):
        assert _resolve_credential_key(None, None) is None

    def test_registry_name_only(self):
        assert _resolve_credential_key("my-reg", None) == "my-reg"


# ---------------------------------------------------------------------------
# init_registry
# ---------------------------------------------------------------------------


class TestInitRegistry:
    def test_returns_noop_when_registry_is_none(self, temp_stardag_dir, monkeypatch):
        """init_registry returns NoOpRegistry when config.registry is None."""
        from stardag.config.loader import clear_config_cache
        from stardag.registry._base import NoOpRegistry, init_registry

        monkeypatch.chdir(temp_stardag_dir.parent)
        clear_config_cache()

        registry = init_registry()
        assert isinstance(registry, NoOpRegistry)

    def test_returns_api_registry_when_registry_configured(
        self, temp_stardag_dir, monkeypatch
    ):
        """init_registry returns APIRegistry when config.registry is set."""
        from stardag.config.loader import clear_config_cache
        from stardag.registry._api_registry import APIRegistry
        from stardag.registry._base import init_registry

        monkeypatch.chdir(temp_stardag_dir.parent)
        monkeypatch.setenv("STARDAG_REGISTRY_URL", "http://localhost:8000")
        monkeypatch.setenv("STARDAG_API_KEY", "test-key")
        clear_config_cache()

        registry = init_registry()
        assert isinstance(registry, APIRegistry)


# ---------------------------------------------------------------------------
# Env-var-only config (no profiles)
# ---------------------------------------------------------------------------


class TestEnvVarOnlyConfig:
    def test_env_vars_create_full_registry_config(self, temp_stardag_dir, monkeypatch):
        """All env vars without any TOML profile creates a complete RegistryConfig."""
        from stardag.config.loader import clear_config_cache, load_config

        monkeypatch.chdir(temp_stardag_dir.parent)
        monkeypatch.setenv("STARDAG_REGISTRY_URL", "https://api.stardag.com")
        monkeypatch.setenv("STARDAG_API_KEY", "sk_test_123")
        monkeypatch.setenv("STARDAG_WORKSPACE_ID", "ws-uuid")
        monkeypatch.setenv("STARDAG_ENVIRONMENT_ID", "env-uuid")

        clear_config_cache()
        config = load_config(use_project_config=False)

        assert config.registry is not None
        assert config.registry.url == "https://api.stardag.com"
        assert config.registry.auth.api_key is not None
        assert config.registry.auth.api_key.get_secret_value() == "sk_test_123"
        assert config.registry.workspace_id == "ws-uuid"
        assert config.registry.environment_id == "env-uuid"
        # No profile context
        assert config.context.profile is None
        assert config.context.registry_name is None

    def test_token_auth_derives_cred_key_from_url(self):
        """StardagTokenAuth without registry_name derives key from URL."""
        auth = StardagTokenAuth(
            access_token="tok",
            workspace_id="ws",
            user_email="u@t.com",
            registry_url="https://api.stardag.com",
            registry_name=None,
        )
        assert auth._cred_key == "api.stardag.com"

    def test_token_auth_prefers_registry_name(self):
        """StardagTokenAuth prefers registry_name over URL-derived key."""
        auth = StardagTokenAuth(
            access_token="tok",
            workspace_id="ws",
            user_email="u@t.com",
            registry_url="https://api.stardag.com",
            registry_name="cloud",
        )
        assert auth._cred_key == "cloud"
