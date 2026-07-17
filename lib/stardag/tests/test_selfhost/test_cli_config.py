"""Tests for self-host CLI config assembly and repo detection."""

from pathlib import Path

import pytest

pytest.importorskip("modal")
pytest.importorskip("cryptography")

from stardag._cli.selfhost import (  # noqa: E402
    FROM_SOURCE_VERSION,
    MAX_ADMIN_PASSWORD_BYTES,
    MIN_ADMIN_PASSWORD_CHARS,
    _admin_password_error,
    _build_config_env,
    _generate_jwt_keypair,
    _provided_config_flags,
    _record_deployed_server_version,
    _resolve_keep_warm,
    _resolve_upgrade_server_version,
)
from stardag.selfhost._modal_app import DEFAULT_SERVER_VERSION  # noqa: E402
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


def test_provided_config_flags():
    # Nothing provided -> re-running `up` keeps the existing config silently
    assert (
        _provided_config_flags(
            auth_mode=None,
            admin_email=None,
            admin_password=None,
            enable_registration=False,
            oidc_issuer=None,
            oidc_sdk_client_id=None,
            oidc_ui_client_id=None,
            oidc_audience=None,
            oidc_jwks_url=None,
        )
        == []
    )
    # Config-affecting flags are reported so `up` can fail fast instead of
    # silently ignoring them when the config secret already exists
    assert _provided_config_flags(
        auth_mode="oidc",
        admin_email=None,
        admin_password=None,
        enable_registration=True,
        oidc_issuer="https://idp.example.com",
        oidc_sdk_client_id=None,
        oidc_ui_client_id=None,
        oidc_audience=None,
        oidc_jwks_url=None,
    ) == ["--auth-mode", "--enable-registration", "--oidc-issuer"]


def test_admin_password_validation():
    """Mirrors the server's password policy: 8-char minimum AND 72-byte
    maximum (bcrypt truncation limit) - a too-long password would otherwise
    crash-loop the API container at startup (bootstrap admin provisioning)."""
    assert _admin_password_error("short") is not None
    assert _admin_password_error("a" * MIN_ADMIN_PASSWORD_CHARS) is None
    assert _admin_password_error("a" * MAX_ADMIN_PASSWORD_BYTES) is None
    assert _admin_password_error("a" * (MAX_ADMIN_PASSWORD_BYTES + 1)) is not None
    # The limit is bytes of UTF-8, not characters ('€' is 3 bytes)
    assert _admin_password_error("€" * 24) is None  # 72 bytes
    assert _admin_password_error("€" * 25) is not None  # 75 bytes


def _patch_meta_dict(monkeypatch: pytest.MonkeyPatch, store: dict) -> list[str]:
    """Patch modal.Dict.from_name to return `store`; records requested names."""
    import modal

    names: list[str] = []

    def fake_from_name(name: str, *, create_if_missing: bool = False):
        names.append(name)
        return store

    monkeypatch.setattr(modal.Dict, "from_name", fake_from_name)
    return names


def test_resolve_keep_warm_explicit_value_wins_and_persists(
    monkeypatch: pytest.MonkeyPatch,
):
    store: dict = {}
    names = _patch_meta_dict(monkeypatch, store)
    assert _resolve_keep_warm("myapp", 2) == 2
    assert store["keep_warm"] == 2
    assert names == ["myapp-meta"]


def test_resolve_keep_warm_omitted_uses_persisted_value(
    monkeypatch: pytest.MonkeyPatch,
):
    store: dict = {"keep_warm": 1}
    _patch_meta_dict(monkeypatch, store)
    # Omitting the flag (None) must NOT reset a previously set value
    assert _resolve_keep_warm("myapp", None) == 1
    assert store["keep_warm"] == 1


def test_resolve_keep_warm_defaults_to_zero(monkeypatch: pytest.MonkeyPatch):
    store: dict = {}
    _patch_meta_dict(monkeypatch, store)
    assert _resolve_keep_warm("myapp", None) == 0
    # Explicit 0 is persisted (distinguishable from "not provided")
    assert _resolve_keep_warm("myapp", 0) == 0
    assert store["keep_warm"] == 0


def test_record_deployed_server_version(monkeypatch: pytest.MonkeyPatch):
    store: dict = {}
    names = _patch_meta_dict(monkeypatch, store)
    _record_deployed_server_version("myapp", "0.2.0")
    assert store["server_version"] == "0.2.0"
    assert names == ["myapp-meta"]
    _record_deployed_server_version("myapp", FROM_SOURCE_VERSION)
    assert store["server_version"] == FROM_SOURCE_VERSION


def test_upgrade_version_defaults_to_deployed(monkeypatch: pytest.MonkeyPatch):
    """A plain `upgrade` must never silently downgrade: the recorded
    deployed version wins over DEFAULT_SERVER_VERSION."""
    store: dict = {"server_version": "99.0.0"}
    _patch_meta_dict(monkeypatch, store)
    assert _resolve_upgrade_server_version("myapp", None) == "99.0.0"
    # The recorded value is not modified by resolution alone
    assert store["server_version"] == "99.0.0"


def test_upgrade_version_falls_back_to_default(monkeypatch: pytest.MonkeyPatch):
    # Meta dict exists but no version recorded (pre-existing deployments)
    store: dict = {"keep_warm": 1}
    _patch_meta_dict(monkeypatch, store)
    assert _resolve_upgrade_server_version("myapp", None) == DEFAULT_SERVER_VERSION


def test_upgrade_version_no_meta_dict(monkeypatch: pytest.MonkeyPatch):
    import modal
    import modal.exception

    def raise_not_found(name: str, *, create_if_missing: bool = False):
        raise modal.exception.NotFoundError(f"Dict {name} not found")

    monkeypatch.setattr(modal.Dict, "from_name", raise_not_found)
    assert _resolve_upgrade_server_version("myapp", None) == DEFAULT_SERVER_VERSION


def test_upgrade_version_explicit_flag_wins(monkeypatch: pytest.MonkeyPatch):
    store: dict = {"server_version": "0.2.0"}
    _patch_meta_dict(monkeypatch, store)
    # Explicit newer version: no warning path, explicit wins
    assert _resolve_upgrade_server_version("myapp", "0.3.0") == "0.3.0"
    # Explicit 'latest' (not comparable): explicit wins
    assert _resolve_upgrade_server_version("myapp", "latest") == "latest"


def test_upgrade_version_explicit_downgrade_warns_but_proceeds(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    store: dict = {"server_version": "0.10.0"}
    _patch_meta_dict(monkeypatch, store)
    # Explicit older version wins (semver-aware: 0.2.0 < 0.10.0), with warning
    assert _resolve_upgrade_server_version("myapp", "0.2.0") == "0.2.0"
    captured = capsys.readouterr()
    assert "Warning" in captured.out
    assert "0.10.0" in captured.out


def test_upgrade_version_from_source_deploy_uses_default(
    monkeypatch: pytest.MonkeyPatch,
):
    # Last deploy was from source: a plain prebuilt `upgrade` cannot infer a
    # version from it, so it falls back to the SDK's tested default.
    store: dict = {"server_version": FROM_SOURCE_VERSION}
    _patch_meta_dict(monkeypatch, store)
    assert _resolve_upgrade_server_version("myapp", None) == DEFAULT_SERVER_VERSION


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
