"""Tests for self-host CLI config assembly and repo detection."""

from pathlib import Path

import pytest

pytest.importorskip("modal")
pytest.importorskip("cryptography")

from stardag._cli.selfhost import _build_config_env, _generate_jwt_keypair  # noqa: E402
from stardag.selfhost._modal_app import find_repo_root  # noqa: E402


def test_find_repo_root_from_nested_dir():
    # tests/ lives inside lib/stardag inside the repo
    root = find_repo_root(Path(__file__).parent)
    assert root is not None
    assert (root / "app" / "stardag-api" / "pyproject.toml").exists()


def test_find_repo_root_not_found(tmp_path: Path):
    assert find_repo_root(tmp_path) is None


def test_build_config_env_local_mode():
    env = _build_config_env(
        pooled_url="postgresql+asyncpg://u:p@pooled/db?ssl=require",
        direct_url="postgresql+asyncpg://u:p@direct/db?ssl=require",
        pooler_compat=True,
        auth_mode="local",
        admin_email="admin@example.com",
        admin_password="s3cret-pass",
        enable_registration=False,
        oidc_issuer=None,
        oidc_sdk_client_id=None,
        oidc_ui_client_id=None,
        oidc_audience=None,
        oidc_jwks_url=None,
    )
    assert env["AUTH_MODE"] == "local"
    assert env["STARDAG_API_DATABASE_URL"].endswith("pooled/db?ssl=require")
    assert env["STARDAG_API_DATABASE_URL_DIRECT"].endswith("direct/db?ssl=require")
    assert env["STARDAG_API_DATABASE_POOLER_COMPAT"] == "true"
    assert env["AUTH_BOOTSTRAP_ADMIN_EMAIL"] == "admin@example.com"
    assert env["AUTH_BOOTSTRAP_ADMIN_PASSWORD"] == "s3cret-pass"
    assert env["AUTH_LOCAL_REGISTRATION_ENABLED"] == "false"
    assert env["EMAIL_ENABLED"] == "false"
    assert "OIDC_ISSUER_URL" not in env


def test_build_config_env_oidc_mode():
    env = _build_config_env(
        pooled_url="postgresql+asyncpg://u:p@host/db",
        direct_url="postgresql+asyncpg://u:p@host/db",
        pooler_compat=False,
        auth_mode="oidc",
        admin_email=None,
        admin_password=None,
        enable_registration=False,
        oidc_issuer="https://idp.example.com",
        oidc_sdk_client_id="client-sdk",
        oidc_ui_client_id="client-ui",
        oidc_audience="client-ui,client-sdk",
        oidc_jwks_url="https://idp.example.com/oauth2/jwks",
    )
    assert env["AUTH_MODE"] == "oidc"
    assert env["OIDC_ISSUER_URL"] == "https://idp.example.com"
    assert env["OIDC_JWKS_URL"] == "https://idp.example.com/oauth2/jwks"
    assert env["OIDC_AUDIENCE"] == "client-ui,client-sdk"
    assert env["OIDC_SDK_CLIENT_ID"] == "client-sdk"
    assert env["OIDC_UI_CLIENT_ID"] == "client-ui"
    assert "AUTH_BOOTSTRAP_ADMIN_EMAIL" not in env


def test_generate_jwt_keypair_pem():
    private_pem, public_pem = _generate_jwt_keypair()
    assert private_pem.startswith("-----BEGIN PRIVATE KEY-----")
    assert public_pem.startswith("-----BEGIN PUBLIC KEY-----")
    # Keys must be valid input for the API's InternalTokenManager contract:
    # loadable PEM (round-trip through cryptography)
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = serialization.load_pem_private_key(private_pem.encode(), password=None)
    assert isinstance(key, rsa.RSAPrivateKey)
    assert key.key_size == 2048
