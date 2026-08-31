"""Tests for self-host CLI config assembly and repo detection."""

from pathlib import Path

import pytest

pytest.importorskip("modal")
pytest.importorskip("cryptography")

import typer  # noqa: E402

from stardag._cli.selfhost import (  # noqa: E402
    FROM_SOURCE_VERSION,
    LATEST_VERSION,
    MAX_ADMIN_PASSWORD_BYTES,
    MIN_ADMIN_PASSWORD_CHARS,
    _admin_password_error,
    _build_config_env,
    _generate_jwt_keypair,
    _latest_released_server_version,
    _provided_config_flags,
    _record_deployed_server_version,
    _resolve_keep_warm,
    _resolve_upgrade_server_version,
    _resolve_version_keyword,
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


def _patch_meta_dict(
    monkeypatch: pytest.MonkeyPatch, store: dict, environments: list | None = None
) -> list[str]:
    """Patch modal.Dict.from_name to return `store`; records requested names
    (and, when ``environments`` is given, the environment_name of each call)."""
    import modal

    names: list[str] = []

    def fake_from_name(
        name: str,
        *,
        create_if_missing: bool = False,
        environment_name: str | None = None,
    ):
        names.append(name)
        if environments is not None:
            environments.append(environment_name)
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

    def raise_not_found(
        name: str,
        *,
        create_if_missing: bool = False,
        environment_name: str | None = None,
    ):
        raise modal.exception.NotFoundError(f"Dict {name} not found")

    monkeypatch.setattr(modal.Dict, "from_name", raise_not_found)
    assert _resolve_upgrade_server_version("myapp", None) == DEFAULT_SERVER_VERSION


def test_upgrade_version_explicit_flag_wins(monkeypatch: pytest.MonkeyPatch):
    store: dict = {"server_version": "0.2.0"}
    _patch_meta_dict(monkeypatch, store)
    # Explicit newer version: no warning path, explicit wins
    assert _resolve_upgrade_server_version("myapp", "0.3.0") == "0.3.0"
    # An explicit version is passed through verbatim. The commands resolve
    # the `latest` keyword before calling this, so it only ever sees X.Y.Z.
    assert _resolve_upgrade_server_version("myapp", "0.2.1") == "0.2.1"


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


# --- `latest` resolution ----------------------------------------------------


def _patch_releases(monkeypatch: pytest.MonkeyPatch, payload) -> dict:
    """Patch httpx.get to answer the GitHub releases call with `payload`."""
    import httpx

    captured: dict = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return payload

    def fake_get(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return FakeResponse()

    monkeypatch.setattr(httpx, "get", fake_get)
    return captured


def test_latest_released_server_version_picks_highest_semver(
    monkeypatch: pytest.MonkeyPatch,
):
    captured = _patch_releases(
        monkeypatch,
        [
            {"tag_name": "server-v0.2.0"},
            {"tag_name": "server-v0.10.0"},
            {"tag_name": "server-v0.9.3"},
        ],
    )
    # 0.10.0 > 0.9.3 numerically, not lexically
    assert _latest_released_server_version() == "0.10.0"
    assert captured["url"].endswith("/repos/stardag-dev/stardag/releases")


def test_latest_released_server_version_ignores_other_tags(
    monkeypatch: pytest.MonkeyPatch,
):
    _patch_releases(
        monkeypatch,
        [
            {"tag_name": "v0.22.0"},  # SDK release, not the server
            {"tag_name": "server-v0.1.2"},
            {"tag_name": "server-vnonsense"},
            {"tag_name": "server-v0.2.0", "draft": True},
            {"tag_name": "server-v0.3.0", "prerelease": True},
        ],
    )
    assert _latest_released_server_version() == "0.1.2"


def test_latest_released_server_version_no_releases_exits(
    monkeypatch: pytest.MonkeyPatch,
):
    _patch_releases(monkeypatch, [{"tag_name": "v0.22.0"}])
    with pytest.raises(typer.Exit):
        _latest_released_server_version()


def test_latest_released_server_version_http_error_exits(
    monkeypatch: pytest.MonkeyPatch,
):
    import httpx

    def fake_get(url, **kwargs):
        raise httpx.ConnectError("no network")

    monkeypatch.setattr(httpx, "get", fake_get)
    with pytest.raises(typer.Exit):
        _latest_released_server_version()


def test_latest_released_server_version_sends_token_when_set(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_example")
    captured = _patch_releases(monkeypatch, [{"tag_name": "server-v0.2.0"}])
    assert _latest_released_server_version() == "0.2.0"
    assert captured["kwargs"]["headers"]["Authorization"] == "Bearer ghp_example"


def test_resolve_version_keyword_passes_through(monkeypatch: pytest.MonkeyPatch):
    # No network call for anything but the keyword
    import httpx

    def explode(*args, **kwargs):
        raise AssertionError("should not have called the releases API")

    monkeypatch.setattr(httpx, "get", explode)
    assert _resolve_version_keyword(None) is None
    assert _resolve_version_keyword("0.2.0") == "0.2.0"


def test_resolve_version_keyword_resolves_latest(monkeypatch: pytest.MonkeyPatch):
    _patch_releases(monkeypatch, [{"tag_name": "server-v0.4.1"}])
    assert _resolve_version_keyword(LATEST_VERSION) == "0.4.1"


# --- upgrade rolls forward as well as refusing to roll back -----------------


def test_upgrade_version_rolls_forward_to_sdk_default(
    monkeypatch: pytest.MonkeyPatch,
):
    """The SDK's tested pin wins when it is ahead of what is deployed.

    Returning the recorded version unconditionally froze a deployment at
    whatever it first recorded, so a plain `upgrade` could never move it.
    """
    store: dict = {"server_version": "0.0.1"}
    _patch_meta_dict(monkeypatch, store)
    assert _resolve_upgrade_server_version("myapp", None) == DEFAULT_SERVER_VERSION


def test_upgrade_version_recorded_latest_resolves_to_concrete(
    monkeypatch: pytest.MonkeyPatch,
):
    """A deployment recorded as `latest` by an older SDK converts to a pin."""
    store: dict = {"server_version": LATEST_VERSION}
    _patch_meta_dict(monkeypatch, store)
    _patch_releases(monkeypatch, [{"tag_name": "server-v0.5.0"}])
    assert _resolve_upgrade_server_version("myapp", None) == "0.5.0"


def test_upgrade_version_unparseable_recorded_version_is_kept(
    monkeypatch: pytest.MonkeyPatch,
):
    """An unrecognised recorded value is left alone rather than guessed at."""
    store: dict = {"server_version": "some-custom-tag"}
    _patch_meta_dict(monkeypatch, store)
    assert _resolve_upgrade_server_version("myapp", None) == "some-custom-tag"


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
